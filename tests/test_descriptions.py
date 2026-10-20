"""Text is persisted and exposed; legacy numbers migrate without changing meaning."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from assistant import AppError, DEFAULTS, balanced_score, make_plan, model_candidate, normalize
from catalog import CANTEEN_ROWS, VENUES
from descriptions import (METRIC_TERMS, FEEDBACK_TERMS, metric_rank, metric_description,
                          feedback_description)
from menus import validate_menu
from storage import Store


class DescriptionTests(unittest.TestCase):
    def test_source_and_catalog_are_all_text_and_keep_original_ordering(self):
        self.assertEqual(len(CANTEEN_ROWS),52)
        for row in CANTEEN_ROWS:
            for field,terms in METRIC_TERMS.items():
                self.assertIsInstance(row[field],str)
                self.assertIn(row[field],terms)
        for field,terms in METRIC_TERMS.items():
            self.assertEqual([metric_description(field,n) for n in range(1,6)],terms)
            self.assertEqual([metric_rank(field,value) for value in terms],[1,2,3,4,5])

    def test_legacy_numeric_and_new_text_candidates_rank_identically(self):
        needs = normalize(dict(DEFAULTS,budget=25,people=1,portion='少一些',pace='慢慢吃'))
        for venue in VENUES:
            text = make_plan(venue['id'],[dict(dish_id=venue['dishes'][0]['id'],quantity=1)],needs)
            legacy = copy.deepcopy(text)
            legacy['ratings'] = {field:metric_rank(field,value) for field,value in text['ratings'].items()}
            self.assertEqual(balanced_score(text,needs,[]),balanced_score(legacy,needs,[]))
            exposed = model_candidate(text)
            self.assertTrue(all(isinstance(value,str) for value in exposed['ratings'].values()))

    def test_legacy_database_columns_snapshots_feedback_and_sources_migrate_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'old.sqlite3'
            needs = normalize(dict(DEFAULTS,budget=25,people=1))
            v = VENUES[0]
            plan = make_plan(v['id'],[dict(dish_id=v['dishes'][0]['id'],quantity=1)],needs)
            plan['ratings'] = {field:metric_rank(field,value) for field,value in plan['ratings'].items()}
            plan['variety_score'] = .7
            menu = validate_menu(dict(name='保留的菜单',dishes=[dict(name='我的套餐',price=18,kind='meal')]))
            legacy_rows = [dict(row,**{field:metric_rank(field,row[field]) for field in METRIC_TERMS}) for row in CANTEEN_ROWS]
            with sqlite3.connect(path) as db:
                db.executescript('''
                    CREATE TABLE canteen_entries (id TEXT PRIMARY KEY,full_name TEXT UNIQUE,canteen TEXT,floor TEXT,food TEXT,
                        distance INTEGER,price INTEGER,taste INTEGER,portion INTEGER,speed INTEGER);
                    CREATE TABLE catalog_meta (key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE meals (id TEXT PRIMARY KEY,created TEXT,plan TEXT,needs TEXT,feedback TEXT,
                        learn_enabled INTEGER DEFAULT 1,request_id TEXT UNIQUE,assumed INTEGER DEFAULT 0,cancelled INTEGER DEFAULT 0);
                    CREATE TABLE overrides (tag TEXT PRIMARY KEY,direction INTEGER);
                    CREATE TABLE menus (id TEXT PRIMARY KEY,created TEXT,data TEXT);
                ''')
                db.executemany('INSERT INTO canteen_entries VALUES (:id,:full_name,:canteen,:floor,:food,:distance,:price,:taste,:portion,:speed)',legacy_rows)
                db.execute("INSERT INTO catalog_meta VALUES ('version','personal-canteens-2026-10-17-v1')")
                db.execute('INSERT INTO meals VALUES (?,?,?,?,?,1,?,1,0)',('old','2026-10-01T10:00:00+00:00',
                    json.dumps(plan),json.dumps(needs),json.dumps(dict(taste=5,portion=2,value=4,wait=12,note='原话保留')),'auto:old'))
                db.execute("INSERT INTO overrides VALUES ('米粉',-1)")
                db.execute('INSERT INTO menus VALUES (?,?,?)',(menu['id'],'2026-10-01',json.dumps(menu)))
            store = Store(path)
            with store.connection() as db:
                types = {r['name']:r['type'] for r in db.execute('PRAGMA table_info(canteen_entries)')}
                self.assertTrue(all(types[field]=='TEXT' for field in METRIC_TERMS))
                rows = [dict(r) for r in db.execute('SELECT * FROM canteen_entries ORDER BY rowid')]
                self.assertEqual(rows,CANTEEN_ROWS)
                self.assertTrue(all(r[0]=='text' for r in db.execute('SELECT typeof(speed) FROM canteen_entries')))
            meal = store.history()[0]
            self.assertEqual(meal['id'],'old')
            self.assertEqual(meal['plan']['ratings'],VENUES[0]['ratings'])
            self.assertEqual(meal['feedback'],dict(taste='很好吃',portion='不太满意',value='划算',wait=12,note='原话保留'))
            self.assertNotIn('variety_score',meal['plan'])
            self.assertTrue(meal['assumed'] and meal['learn_enabled'])
            self.assertEqual(store.menus(),[menu])
            self.assertTrue(any(m['tag']=='炒饭' and m['direction']==1 for m in store.memories()))
            self.assertTrue(any(m['tag']=='米粉' and m['manual'] and m['direction']==-1 for m in store.memories()))
            self.assertEqual(Store(path).state(),store.state())
            self.assertEqual(len(Store(path).venues()),53)

    def test_feedback_writes_only_text_and_learning_remains_reversible(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'text.sqlite3')
            needs = normalize(dict(DEFAULTS,people=1))
            v = VENUES[0]
            p = make_plan(v['id'],[dict(dish_id=v['dishes'][0]['id'],quantity=1)],needs)
            mid = store.save_meal(p,needs,'text')
            store.feedback(mid,dict(taste='很好吃',portion='满意',value='划算'))
            self.assertTrue(any(m['tag']=='炒饭' and m['direction']==1 for m in store.memories()))
            store.feedback(mid,dict(taste=2,wait=8))  # A legacy client is normalized on write.
            self.assertEqual(store.history()[0]['feedback'],dict(taste='不太好吃',portion='满意',value='划算',wait=8))
            self.assertTrue(any(m['tag']=='炒饭' and m['direction']==-1 for m in store.memories()))
            store.toggle_learning(mid,False)
            self.assertFalse(store.memories())
            for value in [True,0,6,2.5,'很多','出餐很快']:
                with self.subTest(value=value),self.assertRaises(AppError):
                    store.feedback(mid,dict(taste=value))
            with store.connection() as db:
                raw = json.loads(db.execute('SELECT feedback FROM meals').fetchone()[0])
            self.assertTrue(all(isinstance(raw[field],str) for field in FEEDBACK_TERMS))


if __name__=='__main__':
    unittest.main()
