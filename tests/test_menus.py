import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from assistant import AppError, DEFAULTS, candidates, make_plan, normalize, recommend, violations
from menus import menu_price, recognize_menu, recommend_import, validate_images, validate_menu
from server import ThreadingHTTPServer, create_handler
from storage import Store

# Explicit conditions for the existing regression scenarios; the app starts unset.
DEFAULTS = dict(DEFAULTS, budget=25, minutes=45, people=1, purpose="日常用餐")
from catalog import VENUES


def source_menu(known=False):
    return dict(name='校园小馆',wait=10,dine=20,dishes=[
        dict(name='青椒肉丝',price=22,kind='dish',taste='香辣',ingredients=['猪肉','辣椒'],ingredients_known=known),
        dict(name='清炒时蔬',price=12,kind='dish',taste='清淡',ingredients=['青菜'],ingredients_known=known),
        dict(name='番茄炒蛋',price=16,kind='dish',taste='清淡',ingredients=['鸡蛋','番茄'],ingredients_known=known),
        dict(name='米饭',price=2,kind='staple',ingredients=['大米'],ingredients_known=known),
        dict(name='时令鲜鱼',price=None,kind='dish',ingredients=[],ingredients_known=False)])


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.menu=validate_menu(source_menu())
        self.venues=[self.menu]
        self.needs=normalize(dict(DEFAULTS,scene='menu',venue_id=self.menu['id'],people=2,budget=25),self.venues)

    def test_prices_are_exact_and_unknown_is_not_zero(self):
        self.assertEqual(menu_price('18.50'),1850)
        self.assertEqual(menu_price(0),0)
        self.assertIsNone(menu_price(None))
        for price in [True,-1,'时价','18~25',18.005,float('nan')]:
            with self.subTest(price=price),self.assertRaises(AppError):
                menu_price(price)

    def test_images_must_be_bounded_inline_images(self):
        for images in [['https://example.com/menu.png'],['data:text/html;base64,AAAA'],['data:image/png;base64,AAAA'],['x']*4]:
            with self.subTest(images=images),self.assertRaises(AppError):
                validate_images(images)

    def test_recognition_never_marks_ingredients_as_verified(self):
        raw=source_menu(True)
        with patch('menus.ai_json',return_value=raw):
            menu,_,model,_=recognize_menu(dict(text='青椒肉丝22元，清炒时蔬12元',model='GLM-4-Flash'))
        self.assertTrue(all(not d['ingredients_known'] for d in menu['dishes']))
        self.assertEqual(model,'GLM-4-Flash')

    def test_menu_prices_do_not_become_new_budget_or_party_size(self):
        raw=source_menu()
        raw['changes']=dict(budget=500,people=12,minutes=240)
        with patch('menus.ai_json',return_value=raw):
            _,changes,_,_=recognize_menu(dict(text='菜单价格22元、12元',requirements='搭配荤素',needs=DEFAULTS))
        self.assertEqual(changes,{})

    def test_shared_dishes_include_rice_and_obey_budget(self):
        pool,_=candidates(self.needs,self.venues)
        self.assertTrue(pool)
        rice=next(d['id'] for d in self.menu['dishes'] if d['name']=='米饭')
        missing=next(d['id'] for d in self.menu['dishes'] if d['price'] is None)
        for plan in pool:
            self.assertEqual(violations(plan,self.needs,self.venues),[])
            self.assertLessEqual(plan['total'],5000)
            self.assertIn(dict(dish_id=rice,quantity=2),[dict(dish_id=i['dish_id'],quantity=i['quantity']) for i in plan['items']])
            self.assertNotIn(missing,[i['dish_id'] for i in plan['items']])

    def test_unknown_ingredients_cannot_silently_satisfy_avoidance(self):
        needs=normalize(dict(self.needs,avoid=['香菜']),self.venues)
        result=recommend(needs,[],mode='basic',venues=self.venues)
        self.assertEqual(result['options'],[])
        self.assertTrue(any('配料未核实' in issue for issue in result['conflicts']))
        menu=validate_menu(source_menu(True),self.menu['id'])
        self.assertTrue(recommend(needs,[],mode='basic',venues=[menu])['options'])

    def test_ai_selection_is_repriced_and_cannot_create_dishes_or_exceed_budget(self):
        selection=[dict(name='青椒肉丝',quantity=1,total=1),dict(name='清炒时蔬',quantity=1),dict(name='米饭',quantity=2)]
        result=recommend_import(self.menu,self.needs,[],{},self.venues,selection,'GLM-4-Flash')
        self.assertEqual(result['mode'],'ai')
        self.assertEqual(result['options'][0]['total'],3800)
        self.assertEqual(result['options'][0]['id'],result['primary_id'])
        for invalid in [[dict(name='虚构菜',quantity=1)],[dict(name='青椒肉丝',quantity=24)]]:
            result=recommend_import(self.menu,self.needs,[],{},self.venues,invalid,'GLM-4-Flash')
            self.assertEqual(result['mode'],'basic')

    def test_menu_state_is_durable_without_mutating_sample_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'menu.sqlite3')
            store.save_menu(self.menu)
            reopened=Store(store.path)
            self.assertEqual(reopened.menus(),[self.menu])
            self.assertEqual(len(reopened.venues()),len(VENUES)+1)
            self.assertEqual(len(candidates(normalize(DEFAULTS))[0]),42)
            reopened.clear()
            self.assertEqual(reopened.menus(),[])


class UploadedMenuHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.store=Store(Path(cls.tmp.name)/'http.sqlite3')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(cls.store))
        cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.worker.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.worker.join();cls.tmp.cleanup()

    def setUp(self):
        self.store.clear()

    def post(self,path,body):
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with self.opener.open(request,timeout=5) as response:
            return json.load(response)

    def import_menu(self):
        raw=source_menu()
        raw['changes']=dict(people=2,budget=25)
        raw['selection']=[dict(name='青椒肉丝',quantity=1),dict(name='清炒时蔬',quantity=1),dict(name='米饭',quantity=2)]
        with patch('menus.ai_json',return_value=raw):
            return self.post('/api/menus/import',dict(text='校园小馆菜单',requirements='两个人，人均25元',needs=DEFAULTS))

    def test_import_chat_quote_selection_and_feedback_are_one_workflow(self):
        result=self.import_menu()
        self.assertEqual(result['recommendations']['mode'],'ai')
        self.assertEqual(self.store.state()['meals'],0)
        needs=result['needs']
        with patch('assistant.ai_text',return_value='可以从这份菜单换个搭配。'):
            chat=self.post('/api/chat',dict(text='换个搭配',needs=needs,mode='chat',messages=[]))
        self.assertTrue(all(p['venue_id']==result['menu']['id'] for p in chat['recommendations']['options']))
        plan=result['recommendations']['options'][0]
        quote=self.post('/api/quote',dict(venue_id=plan['venue_id'],items=plan['items'],needs=needs))
        self.assertEqual(quote['plan']['total'],3800)
        saved=self.post('/api/meals',dict(venue_id=plan['venue_id'],items=plan['items'],needs=needs,request_id='uploaded-meal'))
        self.post('/api/feedback',dict(meal_id=saved['id'],feedback=dict(taste=5)))
        self.assertTrue(self.store.memories())
        self.assertEqual(self.store.state()['meals'],1)

    def test_price_edit_updates_quote_without_adding_a_meal(self):
        result=self.import_menu()
        raw=source_menu(True)
        raw['dishes'][0]['price']=23.5
        edited=self.post('/api/menus/update',dict(menu_id=result['menu']['id'],menu=raw,needs=result['needs']))
        self.assertEqual(edited['menu']['dishes'][0]['price'],2350)
        self.assertEqual(self.store.state()['meals'],0)
        self.assertEqual(len(self.store.menus()),1)

    def test_unsupported_ai_preference_label_does_not_discard_a_valid_menu(self):
        raw=source_menu()
        raw['changes']=dict(tastes=['荤素搭配'],purpose='健康饮食')
        with patch('menus.ai_json',return_value=raw):
            result=self.post('/api/menus/import',dict(text='校园小馆菜单',requirements='两个人，人均25元，搭配荤素',needs=DEFAULTS))
        self.assertTrue(result['recognition_notice'])
        self.assertEqual((result['needs']['people'],result['needs']['budget']),(2,25))
        self.assertTrue(result['recommendations']['options'])

    def test_forged_menu_ids_and_invalid_uploads_are_rejected_before_model_calls(self):
        with patch('menus.ai_json') as model:
            with self.assertRaises(urllib.error.HTTPError):
                self.post('/api/menus/import',dict(images=['https://example.com/menu.png'],needs=DEFAULTS))
            model.assert_not_called()
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.post('/api/menus/update',dict(menu_id='forged',menu=source_menu(),needs=DEFAULTS))
        self.assertEqual(context.exception.code,404)

    def test_delete_menu_keeps_meal_snapshot(self):
        result=self.import_menu()
        plan=result['recommendations']['options'][0]
        saved=self.post('/api/meals',dict(venue_id=plan['venue_id'],items=plan['items'],needs=result['needs'],request_id='snapshot'))
        self.post('/api/menus/delete',dict(menu_id=result['menu']['id']))
        self.assertEqual(self.store.menus(),[])
        self.assertEqual(self.store.history()[0]['id'],saved['id'])
        self.assertEqual(self.store.history()[0]['plan']['total'],3800)


if __name__=='__main__':
    unittest.main()
