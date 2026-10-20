"""Explicit live menu check: recognition + chat, temporary SQLite only."""
import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from assistant import DEFAULTS
from server import ThreadingHTTPServer,create_handler
from storage import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--image',type=Path,help='Use a JPG/PNG/WebP menu instead of the built-in text fixture')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        store=Store(Path(folder)/'live-menu.sqlite3')
        server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base='http://127.0.0.1:'+str(server.server_port)

        def post(path,data):
            request=urllib.request.Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
            try:
                with opener.open(request,timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    message=json.loads(exc.read()).get('error','接口返回错误')
                except ValueError:
                    message='接口返回格式错误'
                raise RuntimeError(message) from None

        try:
            data=dict(needs=DEFAULTS,requirements='两个人，人均30元，想搭配荤素',wait=10,dine=20)
            if args.image:
                mime={'.png':'png','.jpg':'jpeg','.jpeg':'jpeg','.webp':'webp'}[args.image.suffix.lower()]
                data['images']=['data:image/'+mime+';base64,'+base64.b64encode(args.image.read_bytes()).decode()]
            else:
                data['text']='校园小馆菜单\n青椒肉丝 22元\n清炒时蔬 12元\n番茄炒蛋 16元\n米饭 2元/碗\n红烧牛肉面 20元/份'
            result=post('/api/menus/import',data)
            assert result['menu']['dishes'] and result['recommendations']['options']
            assert result['needs']['people']==2 and result['needs']['budget']==30
            assert store.state()['meals']==0
            print('PASS: real menu recognition using '+result['recognition_model']+', automatic recommendations, no meal recorded',flush=True)
            chat=post('/api/chat',dict(text='想更清淡一些，还是从这份菜单里选。',needs=result['needs'],mode='chat',messages=[]))
            assert chat['reply'] and chat['recommendations']['options']
            assert all(p['venue_id']==result['menu']['id'] for p in chat['recommendations']['options'])
            print('PASS: real follow-up chat stays on the uploaded menu and updates combinations',flush=True)
            plan=chat['recommendations']['options'][0]
            quoted=post('/api/quote',dict(venue_id=plan['venue_id'],items=plan['items'],needs=chat['needs']))
            assert not quoted['violations'] and quoted['plan']['total']==sum(i['price']*i['quantity'] for i in plan['items'])
            meal=post('/api/meals',dict(venue_id=plan['venue_id'],items=plan['items'],needs=chat['needs'],request_id=uuid4().hex))
            post('/api/feedback',dict(meal_id=meal['id'],feedback=dict(taste=5)))
            reopened=Store(store.path)
            assert len(reopened.menus())==1 and reopened.state()['meals']==1 and reopened.state()['feedback_count']==1
            print('PASS: authoritative quote, explicit selection, feedback and reopened SQLite persistence',flush=True)
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    main()
