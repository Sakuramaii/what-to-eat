"""Natural-language conditions are applied before selection, with editable estimates."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from assistant import (DEFAULTS, chat_reply, conversation_needs, normalize, parse_needs,
                       recommend)
from server import ThreadingHTTPServer, create_handler
from storage import Store


class ConditionUnderstandingTests(unittest.TestCase):
    def test_exact_written_numbers_and_units(self):
        for text, expected in [
            ('人均二十元，半小时，我们俩，吃少一点',(20,30,2)),
            ('人均25元，我有一个半小时，和两个同学一起',(25,90,3)),
            ('两个人，总预算50元，四十分钟',(25,40,2)),
            ('人均十五元，半个小时，独自去吃',(15,30,1))]:
            with self.subTest(text=text):
                n = parse_needs(text,{},'basic')['needs']
                self.assertEqual((n['budget'],n['minutes'],n['people']),expected)
                self.assertFalse(n['inferred'])

    def test_qualitative_requirements_are_estimated_and_used(self):
        n = conversation_needs('便宜一点，有点赶，和室友一起，没什么胃口',{})
        self.assertEqual((n['budget'],n['minutes'],n['people'],n['portion']), (15,20,2,'少一些'))
        for field in ['budget','minutes','people','portion']:
            self.assertEqual(n['inferred'][field]['value'],n[field])
            self.assertIn('根据',n['inferred'][field]['reason'])
        result = recommend(n,[],mode='basic')
        self.assertEqual(len(result['options']),9)
        self.assertTrue(all(p['per_person_high']<=1500 and p['servings']==2 for p in result['plans']))
        self.assertTrue(all(p['minutes'] is None for p in result['plans']))

    def test_numeric_ranges_are_marked_instead_of_parsed_as_partial_numbers(self):
        for text in ['预算二三十块，十几分钟，两三个人','预算二三十，十几分钟，两三位']:
            with self.subTest(text=text):
                n = parse_needs(text,{},'basic')['needs']
                self.assertEqual((n['budget'],n['minutes'],n['people']),(25,15,3))
                self.assertEqual(set(n['inferred']),{'budget','minutes','people'})

    def test_llm_can_understand_nonliteral_semantics(self):
        with patch('assistant.ai_json',return_value=dict(changes=dict(budget=18,minutes=25,people=4))):
            n = parse_needs('预算不多，别耗太久，我们几个同学一起吃',{},'ai')['needs']
        self.assertEqual((n['budget'],n['minutes'],n['people']),(18,25,4))
        self.assertEqual(set(n['inferred']),{'budget','minutes','people'})

    def test_explicit_values_win_over_fuzzy_and_model_values(self):
        with patch('assistant.ai_json',return_value=dict(changes=dict(budget=15,minutes=20,people=2))):
            n = parse_needs('便宜一点，人均25元；有点赶，但有40分钟，三个人',{},'ai')['needs']
        self.assertEqual((n['budget'],n['minutes'],n['people']),(25,40,3))
        self.assertFalse(any(field in n['inferred'] for field in ['budget','minutes','people']))

    def test_estimates_persist_until_corrected_or_cleared(self):
        first = conversation_needs('便宜点，和室友一起，没胃口',{})
        followup = conversation_needs('想换个口味',first)
        self.assertEqual(followup['inferred'],first['inferred'])
        corrected = conversation_needs('预算18元，三个人',followup)
        self.assertEqual((corrected['budget'],corrected['people']),(18,3))
        self.assertNotIn('budget',corrected['inferred'])
        self.assertNotIn('people',corrected['inferred'])
        self.assertIn('portion',corrected['inferred'])
        cleared = conversation_needs('预算不限，人数未定',corrected)
        self.assertIsNone(cleared['budget'])
        self.assertIsNone(cleared['people'])

    def test_relative_budget_is_not_applied_twice_in_a_chat_turn(self):
        current = normalize(dict(DEFAULTS,budget=25))
        with patch('assistant.ai_text',return_value='可以看看这些经济选择。') as model:
            result = chat_reply('再便宜一点',[],[],[],needs=current)
        self.assertEqual(model.call_args.args[1]['current_needs']['budget'],20)
        self.assertEqual(result['needs']['budget'],20)
        self.assertIn('budget',result['needs']['inferred'])

    def test_total_budget_can_use_estimated_party_size_with_provenance(self):
        n = parse_needs('和室友一起，总预算50元',{},'basic')['needs']
        self.assertEqual((n['people'],n['budget']),(2,25))
        self.assertIn('people',n['inferred'])
        self.assertIn('budget',n['inferred'])
        self.assertIsNone(parse_needs('总预算50元',{},'basic')['needs']['budget'])

    def test_feedback_does_not_become_next_meals_conditions(self):
        current = normalize(dict(DEFAULTS,budget=30,minutes=60,people=2))
        same = conversation_needs('这餐花20元、等了40分钟，没胃口',current,'feedback',dict(budget=20,minutes=40))
        self.assertEqual(same,current)
        next_meal = conversation_needs('下一餐便宜一点，和室友一起',current,'feedback')
        self.assertEqual(next_meal['budget'],24)
        self.assertIn('budget',next_meal['inferred'])

    def test_negation_and_unstated_fields_remain_unset(self):
        n = conversation_needs('不赶时间，不着急，吃少一些',{})
        self.assertIsNone(n['budget'])
        self.assertIsNone(n['minutes'])
        self.assertIsNone(n['people'])
        self.assertFalse(n['inferred'])
        n = conversation_needs('不赶时间',conversation_needs('有点赶',{}))
        self.assertIsNone(n['minutes'])
        self.assertNotIn('minutes',n['inferred'])
        with patch('assistant.ai_json',return_value=dict(changes=dict(budget=25,minutes=45,people=1,avoid=['鸡蛋']))):
            n = parse_needs('推荐几道常吃的餐品',{},'ai')['needs']
        self.assertEqual(n,normalize({}))


class ConditionHTTPTests(unittest.TestCase):
    def test_chat_and_meal_routes_share_interpretation_and_filtered_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'conditions.sqlite3')
            server = ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
            worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path,body):
                request = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                with opener.open(request,timeout=5) as response:
                    return json.load(response)
            try:
                text = '便宜一点，有点赶，和室友一起，没什么胃口'
                with patch('assistant.ai_text',return_value='可以看看这些餐品。') as model:
                    response = post('/api/chat',dict(text=text,needs={},messages=[],mode='chat'))
                n = response['needs']
                self.assertEqual((n['budget'],n['minutes'],n['people']),(15,20,2))
                self.assertEqual(len(response['recommendations']['options']),9)
                self.assertEqual(model.call_args.args[1]['current_needs']['budget'],15)
                self.assertEqual(store.state()['meals'],0)
                with patch('assistant.ai_json',return_value=dict(changes={})):
                    parsed = post('/api/parse',dict(text=text,needs={},mode='ai'))
                selected = post('/api/recommend',dict(needs=parsed['needs'],mode='basic',record=True,meal_session='inferred'))
                saved = selected['state']['history'][0]
                self.assertEqual(saved['needs']['inferred'],parsed['needs']['inferred'])
                self.assertEqual(saved['plan']['servings'],2)
                self.assertFalse(store.memories())
            finally:
                server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    unittest.main()
