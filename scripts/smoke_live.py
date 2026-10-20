"""Explicit live smoke check: six generation calls, temporary SQLite only."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant import DEFAULTS
from server import ThreadingHTTPServer, create_handler
from storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'live.sqlite3')
        server = ThreadingHTTPServer(('127.0.0.1', 0), create_handler(store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = 'http://127.0.0.1:' + str(server.server_port)

        def post(path, data):
            request = urllib.request.Request(base + path, data=json.dumps(data).encode(),
                                             headers={'Content-Type': 'application/json'})
            try:
                with opener.open(request, timeout=45) as response:
                    return json.load(response)
            except Exception as exc:
                if hasattr(exc, 'read'):
                    try:
                        detail = json.loads(exc.read())
                        raise RuntimeError(detail.get('error', '接口验证失败')) from None
                    except ValueError:
                        pass
                raise RuntimeError('接口验证失败，错误详情已隐藏') from None
        try:
            chat = post('/api/chat',dict(text='两个人，人均25元，40分钟，今天吃少一点，不赶时间，推荐几个常吃食堂选项？',
                                         needs=DEFAULTS,mode='chat',messages=[]))
            assert chat['reply'] and chat['recommendations']['options']
            assert chat['needs']['budget']==25 and chat['needs']['people']==2
            assert chat['needs']['portion']=='少一些' and chat['needs']['pace']=='不限'
            assert all(p['rated'] and p['total'] is None and p['minutes'] is None for p in chat['recommendations']['options'])
            assert store.state()['meals']==0
            print('PASS: real chat answers and updates recommendations without recording a meal ('+chat['recommendations']['mode']+')',flush=True)
            parsed = post('/api/parse',dict(text='两个人，人均25元，40分钟内吃完，不吃香菜', needs=DEFAULTS, mode='ai'))
            restricted = parsed['needs']
            assert restricted['people'] == 2 and restricted['budget'] == 25 and restricted['minutes'] == 40 and '香菜' in restricted['avoid']
            blocked = post('/api/recommend',dict(needs=restricted,mode='basic'))
            assert not blocked['options'] and '配料' in '、'.join(blocked['conflicts'])
            needs = chat['needs']
            print('PASS: real AI extracted people, budget, time and avoidance', flush=True)
            session = uuid4().hex
            result = post('/api/recommend',dict(needs=needs, mode='ai', record=True, meal_session=session))
            assert result['mode'] == 'ai' and len(result['plans']) == 3 and len(result['options']) > 3
            assert result['options'][0]['id'] == result['primary_id']
            assert all(p['recommended'] for p in result['options'][:3])
            assert not any(p['recommended'] for p in result['options'][3:])
            assert result['state']['meals'] == 1 and result['state']['feedback_count'] == 0
            assert len(result['options']) == 42
            assert all(p['per_person_high']<=2500 and p['total'] is None for p in result['plans'])
            print('PASS: real AI recommendations first, all eligible options returned, meal recorded automatically', flush=True)
            plan = next(p for p in result['options'] if not p['recommended'])
            meal = post('/api/meals',dict(venue_id=plan['venue_id'], items=plan['items'], needs=needs,
                                          meal_id=result['meal_id'], meal_session=session))
            assert meal['id'] == result['meal_id'] and meal['state']['meals'] == 1
            print('PASS: an alternative replaces the same meal without adding a record', flush=True)
            feedback = post('/api/chat',dict(text='这餐很好吃，分量满意，划算，实际等了4分钟。',
                                             mode='feedback', meal_id=meal['id'], messages=[]))
            assert feedback['saved_feedback'] and feedback['state']['meals'] == 1
            assert feedback['state']['feedback_count'] == 1 and feedback['state']['memories']
            assert feedback['recommendations']['options'] and feedback['needs']['minutes']==DEFAULTS['minutes']
            assert Store(store.path).history()[0]['feedback']['taste'] == '很好吃'
            assert Store(store.path).history()[0]['feedback']['wait'] == 4
            print('PASS: real AI feedback saved immediately, preference learned, reopened SQLite persists records', flush=True)
            preserved_state = store.state()
            fresh = post('/api/new-chat',{})
            assert fresh['completed_meal_id']==meal['id']
            preserved_state['history'][0]['completed']=True
            assert fresh['state']==preserved_state
            assert all(fresh['needs'][key] is None for key in ['budget','minutes','people'])
            assert len(fresh['recommendations']['options'])==52
            fresh_chat = post('/api/chat',dict(text='今天想换换口味，先列出几个食堂选项，人数预算时间暂时没定。',
                needs=fresh['needs'],mode='chat',messages=[]))
            assert fresh_chat['reply']
            assert all(fresh_chat['needs'][key] is None for key in ['budget','minutes','people'])
            assert len(fresh_chat['recommendations']['options'])==52
            assert Store(store.path).state()==preserved_state
            print('PASS: real AI after new chat preserves long-term memory, omits old conversation and returns 52 options ('+fresh_chat['model']+')',flush=True)
            fuzzy = post('/api/chat',dict(text='想花十几块，和室友一起吃，有点赶时间，没什么胃口，帮我挑几份。',
                needs=fresh_chat['needs'],mode='chat',messages=[]))
            inferred = fuzzy['needs']
            assert inferred['budget']==15 and inferred['people']==2 and inferred['minutes']==20
            assert inferred['portion']=='少一些'
            assert all(key in inferred['inferred'] for key in ['budget','people','minutes','portion'])
            assert len(fuzzy['recommendations']['options'])==9
            assert all(p['per_person_high']<=1500 and p['servings']==2 for p in fuzzy['recommendations']['plans'])
            assert Store(store.path).state()==preserved_state
            print('PASS: real AI interprets fuzzy budget/time/party size/portion, labels estimates and filters all recommendations',flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()
