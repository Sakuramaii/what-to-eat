"""Blank initial conditions and new conversations that preserve durable memory."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from assistant import AppError, DEFAULTS, candidates, conversation_needs, normalize, parse_needs, recommend
from menus import validate_menu
from server import ThreadingHTTPServer, create_handler
from storage import Store


class BlankConditionTests(unittest.TestCase):
    def test_unstated_numbers_and_purpose_stay_unset(self):
        needs = normalize({})
        for field in ['budget','minutes','people']:
            self.assertIsNone(needs[field])
        self.assertEqual(needs['purpose'], '')
        result = recommend(needs, [], mode='basic')
        self.assertEqual(len(result['options']), 52)
        self.assertTrue(all(p['people'] is None and p['price_basis']=='serving' for p in result['options']))
        self.assertFalse(any(p['needs_price_check'] for p in result['options']))

    def test_llm_cannot_add_unstated_numeric_or_soft_defaults(self):
        defaults = dict(budget=25, minutes=45, people=1, purpose='快速吃饱', portion='吃饱', pace='快点吃')
        with patch('assistant.ai_json',return_value=dict(changes=defaults)):
            result = parse_needs('推荐几道常吃的餐品', {}, 'ai')['needs']
        self.assertEqual(result, normalize({}))
        self.assertEqual(conversation_needs('推荐几道常吃的餐品',{},changes=defaults), normalize({}))

    def test_one_stated_condition_does_not_populate_other_fields(self):
        with patch('assistant.ai_json',return_value=dict(changes=dict(budget=25,minutes=45,people=1))):
            n = parse_needs('人均25元', {}, 'ai')['needs']
        self.assertEqual(n['budget'], 25)
        self.assertIsNone(n['minutes'])
        self.assertIsNone(n['people'])
        result = recommend(n, [], mode='basic')
        self.assertEqual(len(result['options']), 42)

    def test_clear_one_constraint_keeps_other_explicit_conditions(self):
        current = dict(DEFAULTS, budget=20, minutes=40, people=2)
        n = parse_needs('预算不限', current, 'basic')['needs']
        self.assertIsNone(n['budget'])
        self.assertEqual((n['minutes'],n['people']), (40,2))
        self.assertIsNone(parse_needs('总预算50元',{},'basic')['needs']['budget'])

    def test_unknown_time_does_not_filter_uploaded_menus(self):
        menu = validate_menu(dict(name='自己的菜单',wait=60,dine=60,
            dishes=[dict(name='面',price=28,kind='meal')]))
        n = normalize(dict(DEFAULTS,scene='menu',venue_id=menu['id']),[menu])
        pool, _ = candidates(n,[menu])
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]['total'], 2800)
        self.assertIsNone(pool[0]['people'])
        n['minutes'] = 40
        self.assertFalse(candidates(n,[menu])[0])


class NewChatHTTPTests(unittest.TestCase):
    def test_new_chat_preserves_long_term_memory_and_sends_no_old_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'reset.sqlite3')
            menu = validate_menu(dict(name='保留的菜单',dishes=[dict(name='饭',price=18,kind='meal')]))
            store.save_menu(menu)
            needs = normalize(dict(DEFAULTS,people=2,budget=25,minutes=40,tastes=['清淡']))
            plan = recommend(needs,[],mode='basic')['plans'][0]
            mid = store.assume_meal(plan,needs,'old')
            store.feedback(mid,dict(taste=5))
            store.preference('米粉',1)
            cancelled = store.save_meal(plan,needs,'cancelled')
            store.cancel_meal(cancelled)
            self.assertTrue(store.memories())
            before = store.state()
            server = ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
            worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path,data):
                request = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                with opener.open(request,timeout=5) as response:
                    return json.load(response)
            try:
                with patch.object(store,'clear',side_effect=AssertionError('New chat must not delete memory')):
                    result = post('/api/new-chat',{})
                self.assertEqual(result['completed_meal_id'],mid)
                before['history'][0]['completed'] = True
                self.assertEqual(result['state'],before)
                self.assertEqual(result['needs'],normalize({}))
                self.assertEqual(result['recommendations']['mode'],'basic')
                self.assertEqual(len(result['recommendations']['options']),52)
                self.assertTrue(any(any(days is not None for days in p.get('last_eaten_days',{}).values())
                                    for p in result['recommendations']['options']))
                reopened = Store(store.path)
                self.assertEqual(reopened.state(),before)
                self.assertEqual(reopened.menus(),[menu])
                self.assertTrue(reopened.dish_recency())
                with reopened.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM meals').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM overrides').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM canteen_entries').fetchone()[0],52)
                with patch('assistant.ai_text',return_value='重新开始。') as model:
                    post('/api/chat',dict(text='你好',messages=[],needs=result['needs'],mode='chat'))
                payload = model.call_args.args[1]
                self.assertEqual(payload['conversation'],[])
                self.assertEqual(payload['memories'],[dict(tag=m['tag'],direction=m['direction']) for m in before['memories']])
                self.assertEqual(payload['recent_meals'],[dict(venue=plan['venue_name'],dishes=[i['name'] for i in plan['items']])])
                self.assertEqual(payload['current_needs'],normalize({}))
                repeated = post('/api/new-chat',{})
                self.assertIsNone(repeated['completed_meal_id'])
                self.assertEqual(repeated['state'],before)
                # An old page must not be able to overwrite the completed meal.
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post('/api/recommend',dict(needs=needs,mode='basic',record=True,meal_session='old'))
                self.assertEqual(rejected.exception.code,409)
                self.assertEqual(json.load(rejected.exception)['code'],'meal_closed')
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post('/api/meals',dict(venue_id=plan['venue_id'],items=plan['items'],needs=needs,meal_id=mid))
                self.assertEqual(rejected.exception.code,409)
                self.assertEqual(json.load(rejected.exception)['code'],'meal_closed')
                self.assertEqual(store.state(),before)
                # A new session makes a separate meal, whose alternatives only
                # replace that new meal.
                new = post('/api/recommend',dict(needs=needs,mode='basic',record=True,meal_session='new'))
                self.assertNotEqual(new['meal_id'],mid)
                self.assertEqual(new['state']['meals'],2)
                alternative = next(p for p in new['options'] if not p['needs_price_check'] and p['id']!=plan['id'])
                changed = post('/api/meals',dict(venue_id=alternative['venue_id'],items=alternative['items'],needs=needs,meal_id=new['meal_id']))
                self.assertEqual(changed['id'],new['meal_id'])
                self.assertEqual(changed['state']['meals'],2)
                self.assertEqual(next(m for m in store.history() if m['id']==mid),before['history'][0])
            finally:
                server.shutdown(); server.server_close(); worker.join()


class MealCompletionTests(unittest.TestCase):
    def test_completion_survives_restart_blocks_old_writes_and_allows_optional_feedback(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'completed.sqlite3')
            needs = normalize({})
            plans = recommend(needs,[],mode='basic')['plans']
            mid = store.assume_meal(plans[0],needs,'previous')
            self.assertEqual(store.complete_meal(mid),mid)
            original = store.history()[0]
            reopened = Store(store.path)
            for change in [lambda:reopened.session_meal('previous'),
                           lambda:reopened.assume_meal(plans[1],needs,'previous'),
                           lambda:reopened.update_meal(mid,plans[1],needs)]:
                with self.assertRaises(AppError) as rejected:
                    change()
                self.assertEqual(rejected.exception.code,'meal_closed')
            self.assertEqual(reopened.history(),[original])
            new = reopened.assume_meal(plans[1],needs,'next')
            self.assertNotEqual(new,mid)
            self.assertEqual(reopened.history()[1],original)
            reopened.feedback(mid,dict(taste='很好吃'))
            self.assertTrue(reopened.history()[1]['completed'])
            self.assertTrue(reopened.memories())

    def test_repeated_new_chat_does_not_finish_older_meals(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'completed.sqlite3')
            needs = normalize({})
            plan = recommend(needs,[],mode='basic')['plans'][0]
            older = store.assume_meal(plan,needs,'older')
            newer = store.assume_meal(plan,needs,'newer')
            self.assertEqual(store.complete_meal(),newer)
            self.assertIsNone(store.complete_meal())
            self.assertFalse(next(m for m in store.history() if m['id']==older)['completed'])
            self.assertEqual(store.complete_meal(older),older)


if __name__=='__main__':
    unittest.main()
