"""Two real generation calls in a temporary DB; capture request bodies, never headers."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import ThreadingHTTPServer,create_handler
from storage import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--model',default='MiniMax-M3')
    args=parser.parse_args()
    text='今天想吃面，可以吃什么'
    calls=[]
    original_builder=urllib.request.build_opener
    local=original_builder(urllib.request.ProxyHandler({}))
    def capture_builder(*handlers):
        opener=original_builder(*handlers)
        class CapturedOpener:
            def open(self,request,*pos,**kwargs):
                if request.full_url=='https://llmapi.paratera.com/v1/chat/completions':
                    calls.append(json.loads(request.data))
                return opener.open(request,*pos,**kwargs)
        return CapturedOpener()
    report={'description':'修复后真实请求，两个调用均来自临时空数据库；只记录JSON请求正文，不含密钥或HTTP请求头。',
            'model':args.model,'input':text,'calls':calls}
    output=Path(__file__).resolve().parents[1]/'artifacts/noodles-minimax-real-request.json'
    output.parent.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as folder,patch('assistant.urllib.request.build_opener',side_effect=capture_builder):
            store=Store(Path(folder)/'food.sqlite3')
            server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            def post(path,data):
                request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=json.dumps(data,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
                try:
                    with local.open(request,timeout=48) as response:
                        return json.load(response)
                except urllib.error.HTTPError as exc:
                    detail=json.loads(exc.read())
                    raise RuntimeError(detail.get('error','接口验证失败')) from None
            try:
                parsed=post('/api/parse',dict(text=text,needs={},mode='ai',model=args.model))
                assert parsed['needs']['foods']==['面']
                print('PASS: real parser preserves 想吃面 ('+args.model+')',flush=True)
                result=post('/api/recommend',dict(text=text,messages=[],needs=parsed['needs'],mode='ai',model=args.model,
                    record=True,meal_session='noodles'))
                assert result['mode']=='ai' and len(result['options'])==5
                assert all('面' in p['items'][0]['name'] for p in result['options'])
                assert result['primary_id']==result['plans'][0]['id']
                assert store.state()['meals']==1 and '面' in store.history()[0]['plan']['items'][0]['name']
                assert len(calls)==2
                assert json.loads(calls[1]['messages'][1]['content'])['original_request']==text
                report['result']={'primary':result['plans'][0]['full_name'],
                    'recommendations':[p['full_name'] for p in result['plans']],
                    'options':[p['full_name'] for p in result['options']],
                    'reason':result['plans'][0]['reason'],'model':result['model'],'mode':result['mode']}
                print(json.dumps(report['result'],ensure_ascii=False),flush=True)
                print('PASS: all candidates are noodles, raw request reaches model, AI first choice is saved; live database untouched.',flush=True)
            finally:
                server.shutdown();server.server_close();worker.join()
    finally:
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    main()
