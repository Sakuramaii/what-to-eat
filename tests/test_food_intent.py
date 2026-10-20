"""Keep explicit food requests across parsing, model selection and saved meals."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from assistant import (AppError, balanced_score, candidates, conversation_needs,
                       normalize, parse_needs, recommend)
from catalog import VENUES
from server import ThreadingHTTPServer, create_handler
from storage import Store


def choose(_system,payload,**kwargs):
    if 'current' in payload:
        return dict(changes={})
    return dict(recommendations=[dict(id=p['id'],strategy=s,reason='符合本次餐品需求。',tradeoff='具体配料与耗时需核实。')
        for p,s in zip(payload['candidates'][:3],['口味与尝鲜','省钱省时','综合均衡'])])


class FoodIntentTests(unittest.TestCase):
    def test_explicit_noodles_survive_both_parsers_even_if_model_misses_them(self):
        for mode in ['basic','ai']:
            with self.subTest(mode=mode),patch('assistant.ai_json',return_value=dict(changes={})):
                needs=parse_needs('今天想吃面，可以吃什么',{},mode)['needs']
            self.assertEqual(needs['foods'],['面'])
            result=recommend(needs,[dict(tag='海南鸡',direction=1)],mode='basic')
            self.assertEqual({p['full_name'] for p in result['options']},
                {'清青牛拉面','桃李一楼拉面','观畴一楼拌面','荷园二楼粉面','丁香面'})

    def test_negation_replacement_aliases_and_relaxation(self):
        n=conversation_needs('想吃面条，不要拉面',{})
        self.assertEqual(n['foods'],['面'])
        self.assertEqual(n['avoid_foods'],['拉面'])
        self.assertTrue(all('拉面' not in p['items'][0]['name'] for p in recommend(n,[],mode='basic')['options']))
        n=conversation_needs('改吃米粉',n)
        self.assertEqual(n['foods'],['米粉'])
        n=conversation_needs('什么都可以',n)
        self.assertEqual((n['foods'],n['avoid_foods']),([],[]))
        n=conversation_needs('不吃面',dict(n,foods=['面']))
        self.assertEqual((n['foods'],n['avoid_foods']),([],['面']))

    def test_past_meal_feedback_does_not_become_current_food_request(self):
        current=normalize(dict(foods=['面']))
        self.assertEqual(conversation_needs('上次海南鸡很好吃',current)['foods'],['面'])
        self.assertEqual(conversation_needs('上一餐拉面不错',current,mode='feedback')['foods'],['面'])

    def test_missing_food_or_low_budget_is_not_silently_relaxed(self):
        for text in ['今天想吃寿司','预算1元，想吃面']:
            n=parse_needs(text,{},'basic')['needs']
            with patch('assistant.ai_json') as model:
                result=recommend(n,[],mode='ai')
            self.assertFalse(result['plans'])
            self.assertFalse(result['options'])
            model.assert_not_called()

    def test_model_cannot_return_chicken_outside_noodle_candidates(self):
        n=normalize(dict(foods=['面']))
        chicken=next(v for v in VENUES if v['full_name']=='紫荆二楼海南鸡')
        wrong_id=chicken['id']+':'+chicken['dishes'][0]['id']+'x1'
        def wrong(system,payload,**kwargs):
            result=choose(system,payload,**kwargs)
            result['recommendations'][0]['id']=wrong_id
            return result
        with patch('assistant.ai_json',side_effect=wrong),self.assertRaises(AppError) as rejected:
            recommend(n,[],mode='ai')
        self.assertEqual(rejected.exception.code,'ai_format')

    def test_real_request_text_is_forwarded_without_reapplying_relative_budget(self):
        needs=parse_needs('再便宜一点，想吃面',dict(budget=25),'basic')['needs']
        context=[dict(role='user',content='预算25元')]
        with patch('assistant.ai_json',side_effect=choose) as model:
            recommend(needs,[],mode='ai',text='再便宜一点，想吃面',messages=context)
        payload=model.call_args.args[1]
        self.assertEqual(payload['original_request'],'再便宜一点，想吃面')
        self.assertEqual(payload['conversation'],context)
        self.assertEqual(payload['needs']['budget'],20)
        self.assertEqual(payload['needs']['foods'],['面'])
        self.assertTrue(all('面' in p['items'][0]['name'] for p in payload['candidates']))

    def test_ai_first_choice_is_not_replaced_by_local_score(self):
        n=normalize(dict(foods=['面']))
        pool,_=candidates(n)
        ordered=sorted(pool,key=lambda p:balanced_score(p,n,[]))
        first,second,third=ordered[0],ordered[-1],ordered[1]
        self.assertLess(balanced_score(first,n,[]),balanced_score(second,n,[]))
        response=dict(recommendations=[dict(id=p['id'],strategy=s,reason='按本次需求排序。',tradeoff='耗时未知。')
            for p,s in zip([first,second,third],['口味与尝鲜','省钱省时','综合均衡'])])
        with patch('assistant.ai_json',return_value=response):
            result=recommend(n,[],mode='ai')
        self.assertEqual(result['primary_id'],first['id'])
        self.assertEqual([p['id'] for p in result['plans']],[first['id'],second['id'],third['id']])

    def test_meal_endpoint_saves_noodles_and_rejects_manual_constraint_bypass(self):
        with tempfile.TemporaryDirectory() as folder,patch('assistant.ai_json',side_effect=choose) as model:
            store=Store(Path(folder)/'food.sqlite3')
            server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
            worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path,data):
                request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                with opener.open(request,timeout=5) as response:
                    return json.load(response)
            try:
                text='今天想吃面，可以吃什么'
                parsed=post('/api/parse',dict(text=text,needs={},mode='ai',model='MiniMax-M3'))
                result=post('/api/recommend',dict(text=text,needs=parsed['needs'],mode='ai',record=True,meal_session='noodles',model='MiniMax-M3'))
                self.assertEqual(store.history()[0]['needs']['foods'],['面'])
                self.assertIn('面',store.history()[0]['plan']['items'][0]['name'])
                self.assertEqual(result['needs']['foods'],['面'])
                self.assertEqual(model.call_args.args[1]['original_request'],text)
                chicken=next(v for v in VENUES if v['full_name']=='紫荆二楼海南鸡')
                before=store.state()
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post('/api/meals',dict(venue_id=chicken['id'],items=[dict(dish_id=chicken['dishes'][0]['id'],quantity=1)],
                        needs=parsed['needs'],meal_id=result['meal_id']))
                self.assertEqual(rejected.exception.code,422)
                self.assertEqual(store.state(),before)
            finally:
                server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    unittest.main()
