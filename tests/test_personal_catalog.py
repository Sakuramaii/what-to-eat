"""The production catalog uses only the user's scored entries, never the legacy fixture."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from assistant import (AppError, DEFAULTS, balanced_score, basic_parse, candidates, make_plan,
                       model_candidate, normalize, parse_needs, recommend, violations)
from catalog import CANTEEN_ROWS, PRICE_BANDS, VENUES, public_catalog
from server import ThreadingHTTPServer, create_handler
from storage import Store

# Explicit conditions for the existing regression scenarios; the app starts unset.
DEFAULTS = dict(DEFAULTS, budget=25, minutes=45, people=1, purpose="日常用餐")


def venue(name):
    return next(v for v in VENUES if v['full_name'] == name)


def plan(name, **conditions):
    v = venue(name)
    needs = normalize(dict(DEFAULTS, **conditions))
    return make_plan(v['id'], [dict(dish_id=v['dishes'][0]['id'], quantity=needs['people'])], needs), needs


class PersonalCatalogTests(unittest.TestCase):
    def test_every_scored_row_is_present_and_unscored_rows_are_absent(self):
        self.assertEqual(len(CANTEEN_ROWS), 52)
        self.assertEqual(len({r['canteen'] for r in CANTEEN_ROWS}), 11)
        self.assertEqual(len({v['id'] for v in VENUES}), 52)
        self.assertEqual(len({d['id'] for v in VENUES for d in v['dishes']}), 52)
        self.assertFalse(any('*' in r['full_name'] for r in CANTEEN_ROWS))
        self.assertNotIn('清青披萨', {r['full_name'] for r in CANTEEN_ROWS})
        self.assertEqual(venue('清芬一楼炒饭')['ratings'], dict(distance='近', price='较便宜', taste='不太好吃', portion='较少', speed='快'))
        self.assertEqual(venue('澜园三楼跷脚牛肉')['ratings'], dict(distance='适中', price='贵', taste='好吃', portion='较多', speed='慢'))
        self.assertEqual(venue('桃李地下')['dishes'][0]['name'], '地下')

    def test_prices_and_minutes_are_not_invented(self):
        self.assertFalse(public_catalog()['sample'])
        self.assertEqual(public_catalog()['source'], 'personal_descriptions')
        for v in VENUES:
            self.assertTrue(v['rated'])
            self.assertEqual(v['scene'], 'canteen')
            self.assertIsNone(v['walk'])
            self.assertIsNone(v['wait'])
            self.assertIsNone(v['dine'])
            d = v['dishes'][0]
            self.assertIsNone(d['price'])
            self.assertEqual((d['price_low'], d['price_high']), PRICE_BANDS[v['ratings']['price']])
            self.assertEqual(d['taste'], '未注明')
            self.assertFalse(d['ingredients_known'])

    def test_multi_person_range_calculation(self):
        p, needs = plan('清芬一楼炒饭', people=2, budget=15)
        self.assertEqual((p['total_low'], p['total_high']), (2000, 3000))
        self.assertEqual((p['per_person_low'], p['per_person_high']), (1000, 1500))
        self.assertEqual(p['servings'], 2)
        self.assertIsNone(p['total'])
        self.assertIsNone(p['minutes'])
        self.assertFalse(violations(p, needs))

    def test_price_band_boundaries_and_pending_options(self):
        for budget, expected, pending in [(8, 2, 2), (10, 2, 0), (15, 9, 0), (18, 27, 18), (25, 42, 0), (40, 52, 10)]:
            with self.subTest(budget=budget):
                result = recommend(dict(DEFAULTS, budget=budget), [], mode='basic')
                self.assertEqual(len(result['options']), expected)
                self.assertEqual(sum(p['needs_price_check'] for p in result['options']), pending)
                self.assertFalse(any(p['needs_price_check'] for p in result['plans']))
                self.assertTrue(all(p['budget_status'] == 'within' for p in result['plans']))
        p, _ = plan('清芬一楼烤盘饭', budget=25)
        self.assertEqual(p['budget_status'], 'over')
        self.assertIsNone(p['total_high'])

    def test_unknown_price_never_auto_selected(self):
        result = recommend(dict(DEFAULTS, budget=8), [], mode='basic')
        self.assertFalse(result['plans'])
        self.assertIsNone(result['primary_id'])
        self.assertTrue(all(not p['recommended'] and p['needs_price_check'] for p in result['options']))
        self.assertIn('价格区间', '、'.join(result['conflicts']))

    def test_explicit_actual_price_is_precise_and_budget_checked(self):
        v = venue('清芬一楼烤盘饭')
        did = v['dishes'][0]['id']
        needs = normalize(dict(DEFAULTS, people=2, budget=30))
        items = [dict(dish_id=did, quantity=2, price=1)]
        self.assertIsNone(make_plan(v['id'], items, needs)['total'])
        p = make_plan(v['id'], items, needs, actual_prices={did:28.35})
        self.assertEqual((p['total'], p['per_person']), (5670, 2835))
        self.assertFalse(violations(p, needs))
        p = make_plan(v['id'], items, needs, actual_prices={did:30.01})
        self.assertIn('超过预算', violations(p, needs))
        with self.assertRaises(AppError):
            make_plan(v['id'], items, needs, actual_prices={did:28.351})
        with self.assertRaises(AppError):
            make_plan(v['id'], items, needs, actual_prices={'fake':1})

    def test_hard_avoidance_requires_verified_ingredients(self):
        result = recommend(dict(DEFAULTS, avoid=['香菜']), [], mode='basic')
        self.assertFalse(result['options'])
        self.assertIn('配料', '、'.join(result['conflicts']))

    def test_no_fictional_outside_venues(self):
        result = recommend(dict(DEFAULTS, scene='outside'), [], mode='basic')
        self.assertFalse(result['options'])
        self.assertIn('尚未提供校外', '、'.join(result['conflicts']))

    def test_unknown_time_does_not_become_fake_minutes(self):
        p, n = plan('丁香肠粉', minutes=1)
        self.assertIsNone(p['minutes'])
        self.assertFalse(violations(p, n))
        result = recommend(dict(DEFAULTS, minutes=1), [], mode='basic')
        self.assertIn('不能保证', result['notice'])

    def test_portion_and_pace_are_goals_instead_of_universal_bonuses(self):
        p, n = plan('紫荆二楼海南鸡')
        smaller = copy.deepcopy(p)
        smaller['ratings']['portion'] = '少'
        larger = copy.deepcopy(p)
        larger['ratings']['portion'] = '多'
        self.assertEqual(balanced_score(smaller,n,[]), balanced_score(larger,n,[]))
        for target, preferred, other in [('少一些',smaller,larger), ('吃饱',larger,smaller)]:
            with self.subTest(portion=target):
                n['portion'] = target
                self.assertGreater(balanced_score(preferred,n,[]), balanced_score(other,n,[]))
        slower = copy.deepcopy(p)
        slower['ratings']['speed'] = '慢'
        faster = copy.deepcopy(p)
        faster['ratings']['speed'] = '快'
        self.assertEqual(balanced_score(slower,n,[]), balanced_score(faster,n,[]))
        for target, preferred, other in [('慢慢吃',slower,faster), ('快点吃',faster,slower)]:
            n['pace'] = target
            self.assertGreater(balanced_score(preferred,n,[]), balanced_score(other,n,[]))
        n.update(purpose='快速吃饱', pace='慢慢吃')
        self.assertGreater(balanced_score(slower,n,[]), balanced_score(faster,n,[]))

    def test_taste_distance_and_recency_affect_ranking(self):
        p, n = plan('澜园三楼韩式')
        better = copy.deepcopy(p)
        better['ratings']['taste'] = '很好吃'
        self.assertGreater(balanced_score(better,n,[]), balanced_score(p,n,[]))
        better['ratings']['distance'] = '近'
        self.assertGreater(balanced_score(better,n,[]), balanced_score(p,n,[]))
        did = p['items'][0]['dish_id']
        self.assertGreater(balanced_score(p,n,[],{did:dict(days_since=20)}), balanced_score(p,n,[],{did:dict(days_since=0)}))

    def test_explicit_small_portion_and_relaxed_pace_are_parsed(self):
        change = basic_parse('两个人，人均25元，40分钟，少吃一点，不赶时间，不吃香菜')
        self.assertEqual(change['people'], 2)
        self.assertEqual(change['budget'], 25)
        self.assertEqual(change['portion'], '少一些')
        self.assertEqual(change['pace'], '不限')
        self.assertEqual(change['purpose'], '')
        self.assertIn('香菜', change['avoid'])
        with patch('assistant.ai_json', return_value={'changes':dict(portion='吃饱',pace='快点吃')}):
            parsed = parse_needs('吃少一些，慢慢吃', DEFAULTS, 'ai')['needs']
        self.assertEqual(parsed['portion'], '少一些')
        self.assertEqual(parsed['pace'], '慢慢吃')

    def test_ai_receives_yuan_ranges_and_cannot_invent_dishes_or_amounts(self):
        p, _ = plan('清芬一楼炒饭')
        payload = model_candidate(p)
        self.assertEqual(payload['per_person_high'], 15)
        self.assertIsNone(payload['per_person'])
        self.assertEqual(p['per_person_high'], 1500)
        with patch('assistant.ai_json', return_value={'recommendations':[dict(id='invented',strategy='综合均衡',reason='好吃',tradeoff='无')]}):
            with self.assertRaises(AppError):
                recommend(DEFAULTS, [])

    def test_database_import_is_durable_and_keeps_personal_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'eat.sqlite3'
            # Simulate a database from before the catalog table existed.
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE overrides (tag TEXT PRIMARY KEY, direction INTEGER NOT NULL)')
            db.execute("INSERT INTO overrides VALUES ('米粉',1)")
            db.commit(); db.close()
            store = Store(path)
            self.assertEqual(len(store.venues()), 52)
            with store.connection() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM canteen_entries ORDER BY rowid')]
            self.assertEqual(rows, CANTEEN_ROWS)
            p, n = plan('清芬一楼炒饭')
            mid = store.save_meal(p, n, 'existing')
            store.feedback(mid, dict(taste=5))
            reopened = Store(path)
            self.assertEqual(reopened.state()['meals'], 1)
            self.assertTrue(any(m['tag']=='炒饭' for m in reopened.memories()))
            self.assertTrue(any(m['tag']=='米粉' and m['manual'] for m in reopened.memories()))
            self.assertEqual(len(reopened.venues()), 52)
            reopened.clear()
            self.assertEqual(len(reopened.venues()), 52)
            self.assertEqual(reopened.state()['meals'], 0)


class PersonalCatalogHTTPTests(unittest.TestCase):
    def test_pending_prices_cannot_be_recorded_until_explicitly_resolved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'api.sqlite3')
            server = ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
            worker = threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def call(path, body=None):
                req = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json'})
                try:
                    with opener.open(req) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                _, catalog = call('/api/catalog')
                self.assertEqual(len(catalog['venues']), 52)
                needs = dict(DEFAULTS, budget=8)
                _, result = call('/api/recommend', dict(needs=needs,mode='basic',record=True,meal_session='low'))
                self.assertFalse(result['plans'])
                self.assertEqual(store.state()['meals'], 0)
                p = result['options'][0]
                body = dict(needs=needs,venue_id=p['venue_id'],items=p['items'],request_id='test')
                code, response = call('/api/meals',body)
                self.assertEqual(code, 422)
                self.assertEqual(store.state()['meals'], 0)
                body['actual_prices'] = {p['items'][0]['dish_id']:8}
                code, response = call('/api/meals',body)
                self.assertEqual(code, 201)
                self.assertEqual(response['state']['history'][0]['plan']['total'], 800)
                _, selected = call('/api/recommend', dict(needs=DEFAULTS,mode='basic',record=True,meal_session='normal'))
                self.assertIsNone(selected['state']['history'][0]['plan']['total'])
                self.assertEqual(selected['state']['meals'], 2)
            finally:
                server.shutdown(); server.server_close(); worker.join()


if __name__ == '__main__':
    unittest.main()
