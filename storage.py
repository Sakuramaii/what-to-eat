"""Single-user durable records and reversible, evidence-based soft preferences."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from assistant import AppError, number
from catalog import MEMORY_TAGS, CANTEEN_ROWS, CATALOG_VERSION, rated_venue
from descriptions import METRIC_TERMS, metric_description, feedback_description, feedback_rank, describe_plan


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meals (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, plan TEXT NOT NULL,
                    needs TEXT NOT NULL, feedback TEXT, learn_enabled INTEGER NOT NULL DEFAULT 1,
                    request_id TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS overrides (tag TEXT PRIMARY KEY, direction INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS menus (id TEXT PRIMARY KEY, created TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS canteen_entries (
                    id TEXT PRIMARY KEY, full_name TEXT NOT NULL UNIQUE, canteen TEXT NOT NULL,
                    floor TEXT NOT NULL, food TEXT NOT NULL, distance TEXT NOT NULL,
                    price TEXT NOT NULL, taste TEXT NOT NULL, portion TEXT NOT NULL, speed TEXT NOT NULL
                );
            ''')
            db.execute('BEGIN IMMEDIATE')
            entries = [dict(row) for row in db.execute('SELECT * FROM canteen_entries ORDER BY rowid')]
            types = {row['name']:row['type'].upper() for row in db.execute('PRAGMA table_info(canteen_entries)')}
            if any(types[field]!='TEXT' for field in METRIC_TERMS):
                db.execute('''CREATE TABLE canteen_entries_text (
                    id TEXT PRIMARY KEY, full_name TEXT NOT NULL UNIQUE, canteen TEXT NOT NULL,
                    floor TEXT NOT NULL, food TEXT NOT NULL, distance TEXT NOT NULL,
                    price TEXT NOT NULL, taste TEXT NOT NULL, portion TEXT NOT NULL, speed TEXT NOT NULL
                )''')
                for row in entries:
                    row.update({field:metric_description(field,row[field]) for field in METRIC_TERMS})
                db.executemany('INSERT INTO canteen_entries_text VALUES (:id,:full_name,:canteen,:floor,:food,:distance,:price,:taste,:portion,:speed)',entries)
                db.execute('DROP TABLE canteen_entries')
                db.execute('ALTER TABLE canteen_entries_text RENAME TO canteen_entries')
            version = db.execute("SELECT value FROM catalog_meta WHERE key='version'").fetchone()
            if not version or version['value'] != CATALOG_VERSION:
                if not entries:
                    db.executemany('INSERT INTO canteen_entries VALUES (:id,:full_name,:canteen,:floor,:food,:distance,:price,:taste,:portion,:speed)', CANTEEN_ROWS)
                else:
                    for row in entries:
                        db.execute('UPDATE canteen_entries SET distance=?,price=?,taste=?,portion=?,speed=? WHERE id=?',
                            tuple(metric_description(field,row[field]) for field in METRIC_TERMS)+(row['id'],))
                db.execute("INSERT INTO catalog_meta VALUES ('version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CATALOG_VERSION,))
            columns = {r['name'] for r in db.execute('PRAGMA table_info(meals)')}
            for name in ['assumed', 'cancelled', 'completed']:
                if name not in columns:
                    db.execute(f'ALTER TABLE meals ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            for row in db.execute('SELECT id,plan,feedback FROM meals').fetchall():
                original = json.loads(row['plan'])
                plan = describe_plan(original)
                if plan != original:
                    db.execute('UPDATE meals SET plan=? WHERE id=?',(json.dumps(plan,ensure_ascii=False),row['id']))
                if row['feedback']:
                    old = json.loads(row['feedback'])
                    feedback = dict(old,**{field:feedback_description(field,old[field]) for field in ['taste','portion','value'] if field in old})
                    if feedback != old:
                        db.execute('UPDATE meals SET feedback=? WHERE id=?',(json.dumps(feedback,ensure_ascii=False),row['id']))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def history(self):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM meals WHERE cancelled=0 ORDER BY created DESC, rowid DESC').fetchall()
        return [dict(id=r['id'], created=r['created'], plan=json.loads(r['plan']), needs=json.loads(r['needs']),
                     feedback=json.loads(r['feedback']) if r['feedback'] else None,
                     learn_enabled=bool(r['learn_enabled']), assumed=bool(r['assumed']), completed=bool(r['completed'])) for r in rows]

    def menus(self):
        with self.connection() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM menus ORDER BY created DESC')]

    def venues(self):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM canteen_entries ORDER BY rowid').fetchall()
        return [rated_venue(dict(row)) for row in rows] + self.menus()

    def save_menu(self, menu):
        with self.connection() as db:
            db.execute('INSERT INTO menus VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (menu['id'], datetime.now(timezone.utc).isoformat(), json.dumps(menu, ensure_ascii=False)))

    def delete_menu(self, mid):
        with self.connection() as db:
            if not db.execute('DELETE FROM menus WHERE id=?', (mid,)).rowcount:
                raise AppError('找不到这份菜单', 'not_found', 404)

    def save_meal(self, plan, needs, request_id):
        plan = describe_plan(plan)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise AppError('缺少选择标识，请刷新后重试')
        with self.connection() as db:
            old = db.execute('SELECT id FROM meals WHERE request_id=?', (request_id,)).fetchone()
            if old:
                return old['id']
            mid = uuid4().hex
            db.execute('INSERT OR IGNORE INTO meals (id,created,plan,needs,request_id) VALUES (?,?,?,?,?)',
                       (mid, datetime.now(timezone.utc).isoformat(), json.dumps(plan, ensure_ascii=False),
                        json.dumps(needs, ensure_ascii=False), request_id))
            mid = db.execute('SELECT id FROM meals WHERE request_id=?', (request_id,)).fetchone()['id']
        return mid

    def session_meal(self, session):
        if not isinstance(session, str) or not 1 <= len(session) <= 100:
            raise AppError('本次用餐标识无效')
        with self.connection() as db:
            row = db.execute('SELECT id,completed FROM meals WHERE request_id=? AND cancelled=0', ('auto:' + session,)).fetchone()
        if row and row['completed']:
            raise AppError('上一餐已完成，请为新一餐重新推荐。', 'meal_closed', 409)
        return row['id'] if row else None

    def complete_meal(self, mid=None):
        """End the previous meal without changing its choice, feedback or learning."""
        if mid is not None and (not isinstance(mid, str) or not 1 <= len(mid) <= 100):
            raise AppError('用餐记录标识无效')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if mid is None:
                row = db.execute('SELECT id,completed FROM meals WHERE cancelled=0 ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
            else:
                row = db.execute('SELECT id,completed FROM meals WHERE id=? AND cancelled=0', (mid,)).fetchone()
            if not row or row['completed']:
                return None
            db.execute('UPDATE meals SET completed=1 WHERE id=?', (row['id'],))
            return row['id']

    def assume_meal(self, plan, needs, session):
        plan = describe_plan(plan)
        self.session_meal(session)
        mid = uuid4().hex
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO meals (id,created,plan,needs,request_id,assumed) VALUES (?,?,?,?,?,1)',
                       (mid, datetime.now(timezone.utc).isoformat(), json.dumps(plan, ensure_ascii=False),
                        json.dumps(needs, ensure_ascii=False), 'auto:' + session))
            row = db.execute('SELECT * FROM meals WHERE request_id=?', ('auto:' + session,)).fetchone()
            if row['cancelled']:
                raise AppError('这餐已撤销，请开始新的一餐。', 'meal_closed', 409)
            if row['completed']:
                raise AppError('上一餐已完成，请为新一餐重新推荐。', 'meal_closed', 409)
            old = json.loads(row['plan'])
            if row['feedback'] and old['id'] != plan['id']:
                raise AppError('这餐已有反馈，请开始新的一餐。', 'meal_closed', 409)
            db.execute('UPDATE meals SET plan=?,needs=? WHERE id=?',
                       (json.dumps(plan, ensure_ascii=False), json.dumps(needs, ensure_ascii=False), row['id']))
            return row['id']

    def update_meal(self, mid, plan, needs):
        plan = describe_plan(plan)
        with self.connection() as db:
            row = db.execute('SELECT * FROM meals WHERE id=? AND cancelled=0', (mid,)).fetchone()
            if not row:
                raise AppError('找不到这条用餐记录', 'not_found', 404)
            if row['completed']:
                raise AppError('上一餐已完成，请为新一餐重新推荐。', 'meal_closed', 409)
            changed = json.loads(row['plan'])['id'] != plan['id']
            updated = db.execute('UPDATE meals SET plan=?,needs=?,assumed=0,feedback=?,learn_enabled=? WHERE id=? AND completed=0 AND cancelled=0',
                       (json.dumps(plan, ensure_ascii=False), json.dumps(needs, ensure_ascii=False),
                        None if changed else row['feedback'], 1 if changed else row['learn_enabled'], mid))
            if not updated.rowcount:
                raise AppError('这餐已结束，请为新一餐重新推荐。', 'meal_closed', 409)
        return mid

    def cancel_meal(self, mid, cancelled=True):
        if not isinstance(cancelled, bool):
            raise AppError('记录状态无效')
        with self.connection() as db:
            cursor = db.execute('UPDATE meals SET cancelled=? WHERE id=?', (int(cancelled), mid))
            if not cursor.rowcount:
                raise AppError('找不到这条用餐记录', 'not_found', 404)

    @staticmethod
    def feedback_patch(data):
        if not isinstance(data, dict):
            raise AppError('反馈格式不正确')
        try:
            feedback = {k:feedback_description(k,data[k]) for k in ['taste','portion','value'] if data.get(k) is not None}
        except ValueError:
            raise AppError('请填写有效的口味、分量或性价比感受')
        if data.get('wait') is not None:
            feedback['wait'] = number(data['wait'], '实际等待时间', 0, 240, True)
        if data.get('note') is not None:
            if not isinstance(data['note'], str) or len(data['note']) > 500:
                raise AppError('补充反馈最多 500 字')
            feedback['note'] = data['note'].strip()
        if not feedback or all(value == '' for value in feedback.values()):
            raise AppError('请至少提供一项餐后感受')
        return feedback

    @staticmethod
    def _feedback(db, mid, patch):
        row = db.execute('SELECT feedback FROM meals WHERE id=? AND cancelled=0', (mid,)).fetchone()
        if not row:
            raise AppError('找不到这条用餐记录', 'not_found', 404)
        merged = dict(json.loads(row['feedback']) if row['feedback'] else {}, **patch)
        merged.update({field:feedback_description(field,merged[field]) for field in ['taste','portion','value'] if field in merged})
        db.execute('UPDATE meals SET feedback=? WHERE id=?', (json.dumps(merged, ensure_ascii=False), mid))

    def feedback(self, mid, data):
        feedback = self.feedback_patch(data)
        with self.connection() as db:
            self._feedback(db, mid, feedback)

    def apply_chat_feedback(self, result):
        draft = result.get('feedback_draft')
        patch = self.feedback_patch(draft) if draft else None
        proposal = result.get('memory_proposal')
        if proposal and (proposal.get('tag') not in MEMORY_TAGS or type(proposal.get('direction')) is not int
                         or proposal['direction'] not in [-1, 0, 1]):
            raise AppError('偏好格式不正确')
        with self.connection() as db:
            if patch:
                self._feedback(db, result.get('meal_id'), patch)
            if proposal:
                db.execute('INSERT INTO overrides VALUES (?,?) ON CONFLICT(tag) DO UPDATE SET direction=excluded.direction',
                           (proposal['tag'], proposal['direction']))
        return dict(saved_feedback=bool(patch), saved_memory=bool(proposal))

    def toggle_learning(self, mid, enabled):
        if not isinstance(enabled, bool):
            raise AppError('学习状态格式不正确')
        with self.connection() as db:
            cursor = db.execute('UPDATE meals SET learn_enabled=? WHERE id=?', (int(enabled), mid))
            if not cursor.rowcount:
                raise AppError('找不到这条用餐记录', 'not_found', 404)

    def memories(self):
        evidence = {}
        for meal in self.history():
            feedback = meal['feedback']
            if not feedback or not meal['learn_enabled']:
                continue
            taste = feedback_rank('taste',feedback.get('taste','一般'))
            direction = 1 if taste >= 4 else -1 if taste <= 2 else 0
            if not direction:
                continue
            # One meal is one piece of evidence, independent of portions or repeated edits.
            tags = ({i['taste'] for i in meal['plan']['items']} | {i['category'] for i in meal['plan']['items']}) & set(MEMORY_TAGS)
            for tag in tags:
                evidence.setdefault(tag, []).append(dict(meal_id=meal['id'], venue=meal['plan']['venue_name'],
                                                        created=meal['created'], direction=direction))
        with self.connection() as db:
            overrides = {r['tag']: r['direction'] for r in db.execute('SELECT * FROM overrides')}
        result = []
        for tag in sorted(set(evidence) | set(overrides)):
            sources = evidence.get(tag, [])
            positives = sum(s['direction'] == 1 for s in sources)
            negatives = sum(s['direction'] == -1 for s in sources)
            direction = (1 if positives > negatives else -1 if negatives > positives else 0)
            manual = tag in overrides
            direction = overrides.get(tag, direction)
            if direction:
                result.append(dict(tag=tag, direction=direction, manual=manual, sources=sources,
                                   label=('喜欢' if direction == 1 else '较少推荐') + tag))
        return result

    def preference(self, tag, direction):
        if tag not in MEMORY_TAGS or direction not in [-1, 0, 1] or isinstance(direction, bool):
            raise AppError('请选择有效的偏好与方向')
        with self.connection() as db:
            db.execute('INSERT INTO overrides VALUES (?,?) ON CONFLICT(tag) DO UPDATE SET direction=excluded.direction', (tag, direction))

    def reset_preference(self, tag):
        if tag not in MEMORY_TAGS:
            raise AppError('请选择有效的偏好')
        with self.connection() as db:
            db.execute('DELETE FROM overrides WHERE tag=?', (tag,))

    def recent_dishes(self):
        return list({i['dish_id'] for m in self.history()[:7] for i in m['plan']['items']})

    def dish_recency(self, exclude_meal_id=None):
        now = datetime.now(timezone.utc)
        result = {}
        for meal in self.history():
            if meal['id'] == exclude_meal_id:
                continue
            when = datetime.fromisoformat(meal['created'])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            for item in meal['plan']['items']:
                key = item['dish_id']
                if key not in result:
                    result[key] = dict(last_eaten=meal['created'], days_since=max(0, (now - when).total_seconds() / 86400), times=0)
                result[key]['times'] += 1
        return result

    def clear(self, keep_menus=False):
        with self.connection() as db:
            db.execute('DELETE FROM meals')
            db.execute('DELETE FROM overrides')
            if not keep_menus:
                db.execute('DELETE FROM menus')

    def state(self):
        history = self.history()
        return dict(history=history, memories=self.memories(),
                    meals=len(history), feedback_count=sum(bool(m['feedback']) for m in history))
