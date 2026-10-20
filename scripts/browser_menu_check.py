"""Menu upload, editing, chat, selection and responsive QA; no paid model calls."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright,expect
from assistant import AppError,DEFAULTS,basic_parse
from server import ThreadingHTTPServer,create_handler
from storage import Store


def menu_model(_system,payload,**kwargs):
    if 'FAIL' in payload['menu_text']:
        raise AppError('测试：菜单识别失败，请重试。','upstream',502)
    dishes=[dict(name='青椒肉丝',price=22,kind='dish',taste='香辣',ingredients=['猪肉','辣椒']),
            dict(name='清炒时蔬',price=12,kind='dish',taste='清淡',ingredients=['青菜']),
            dict(name='番茄炒蛋',price=16,kind='dish',taste='清淡',ingredients=['番茄','鸡蛋']),
            dict(name='米饭',price=2,kind='staple',ingredients=['大米']),
            dict(name='时令鱼',price=None,kind='dish',ingredients=[])]
    name='校园小馆'
    selection=[dict(name='青椒肉丝',quantity=1),dict(name='清炒时蔬',quantity=1),dict(name='米饭',quantity=2)]
    if '文字单人餐' in payload['menu_text']:
        name='文字单人餐'
        dishes=[dict(name='鸡腿饭',price=18,kind='meal',taste='鲜香',ingredients=[]),
                dict(name='菌菇面',price=16,kind='meal',taste='清淡',ingredients=[])]
        selection=[dict(name='鸡腿饭',quantity=1)]
    return dict(name=name,dishes=dishes,changes=basic_parse(payload['requirements'],payload.get('current_needs')),selection=selection)


def feedback_model(_system,payload,**kwargs):
    return dict(reply='这次的感受记下了。',feedback_draft=dict(taste=5,wait=8))


def main():
    artifacts=Path(__file__).resolve().parents[1]/'artifacts'
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder,patch('menus.ai_json',side_effect=menu_model) as recognition,patch('assistant.ai_text',return_value='可以在你发送的菜单里继续调整搭配。'),patch('assistant.ai_json',side_effect=feedback_model):
        store=Store(Path(folder)/'menu.sqlite3')
        server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.set_content('<html lang="zh"><meta charset="utf-8"><style>body{font:28px sans-serif;padding:32px;background:#fff7ed}p{margin:24px}</style><h1>校园小馆菜单</h1><p>青椒肉丝 22元</p><p>清炒时蔬 12元</p><p>番茄炒蛋 16元</p><p>米饭 2元</p></html>')
                fixture=Path(folder)/'menu.png';page.screenshot(path=str(fixture))
                base='http://127.0.0.1:'+str(server.server_port)
                page.goto(base,wait_until='networkidle')
                page.locator('#upload-menu').click()
                page.locator('#menu-import-submit').click()
                expect(page.locator('#menu-import-error')).to_contain_text('选择菜单图片')
                recognition.assert_not_called()
                page.locator('#menu-files').set_input_files([str(fixture)]*4)
                expect(page.locator('#menu-import-error')).to_contain_text('最多发送 3 张')
                page.locator('#menu-files').set_input_files(str(fixture))
                expect(page.locator('.menu-image')).to_have_count(1)
                page.locator('#menu-requirements').fill('两个人，人均25元，搭配荤素')
                page.screenshot(path=str(artifacts/'menu-upload-desktop.png'))
                page.locator('#menu-import-submit').click()
                expect(page.locator('#menu-dialog')).not_to_be_visible()
                expect(page.locator('#current-menu')).to_contain_text('校园小馆')
                expect(page.locator('#people')).to_have_value('2')
                expect(page.locator('.recommendation-card').first).to_contain_text('青椒肉丝')
                expect(page.locator('.recommendation-card').first).to_contain_text('清炒时蔬')
                assert store.state()['meals']==0 and len(store.menus())==1
                assert recognition.call_args.kwargs['images'][0].startswith('data:image/jpeg;base64,')
                assert recognition.call_args.kwargs['model']=='GLM-4V-Plus-0111'
                first_menu=store.menus()[0]['id']
                page.screenshot(path=str(artifacts/'menu-recommendations.png'))
                print('PASS: photo upload, preview, size/count validation, AI selection, authoritative totals and no auto record',flush=True)

                page.locator('#current-menu').click()
                expect(page.locator('[data-menu-row]')).to_have_count(5)
                page.locator('.edit-dish-price').first.fill('23.50')
                for checkbox in page.locator('.edit-dish-known').all():
                    checkbox.check()
                page.locator('#menu-edit-save').click()
                expect(page.locator('#menu-dialog')).not_to_be_visible()
                assert store.menus()[0]['dishes'][0]['price']==2350
                assert store.state()['meals']==0
                page.locator('#chat-input').fill('人均20元，换个搭配')
                page.locator('#send-button').click()
                expect(page.locator('#send-button')).to_be_enabled()
                expect(page.locator('#budget')).to_have_value('20')
                assert page.locator('.recommendation-card').count()>0
                assert all(first_menu in pid for pid in page.locator('.recommendation-card').evaluate_all('(els)=>els.map(el=>el.dataset.planId)'))
                page.locator('[data-choose]').first.click()
                expect(page.locator('.chosen-label')).to_have_count(1)
                assert store.state()['meals']==1
                page.locator('#chat-mode').select_option('feedback')
                page.locator('#chat-input').fill('口味5分，等了8分钟')
                page.locator('#send-button').click()
                expect(page.locator('#send-button')).to_be_enabled()
                assert store.state()['feedback_count']==1 and store.memories()
                page.reload(wait_until='networkidle')
                expect(page.locator('#current-menu')).to_contain_text('校园小馆')
                assert store.state()['meals']==1
                print('PASS: edit prices/ingredients, chat stays on uploaded menu, explicit selection, feedback, reload persistence',flush=True)

                page.locator('#upload-menu').click()
                page.locator('#menu-text').fill('文字单人餐：鸡腿饭18元，菌菇面16元')
                page.locator('#menu-requirements').fill('一个人，人均20元')
                page.locator('#menu-import-submit').click()
                expect(page.locator('#menu-dialog')).not_to_be_visible()
                expect(page.locator('#current-menu')).to_contain_text('文字单人餐')
                expect(page.locator('.recommendation-card').first).to_contain_text('鸡腿饭')
                assert len(store.menus())==2 and store.state()['meals']==1
                page.locator('#upload-menu').click()
                page.locator('#menu-text').fill('FAIL')
                page.locator('#menu-import-submit').click()
                expect(page.locator('#menu-import-error')).to_contain_text('菜单识别失败')
                expect(page.locator('#menu-text')).to_have_value('FAIL')
                expect(page.locator('#current-menu')).to_contain_text('文字单人餐')
                assert len(store.menus())==2
                page.locator('#menu-text').fill('')
                for width in [390,320]:
                    page.set_viewport_size({'width':width,'height':844})
                    assert page.locator('#menu-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                    page.screenshot(path=str(artifacts/('menu-upload-mobile-'+str(width)+'.png')))
                page.set_viewport_size({'width':1440,'height':1000})
                page.locator('[data-use-menu="'+first_menu+'"]').click()
                expect(page.locator('#menu-dialog')).not_to_be_visible()
                expect(page.locator('#current-menu')).to_contain_text('校园小馆')
                page.locator('#current-menu').click()
                page.locator('#menu-delete').click()
                expect(page.locator('#menu-dialog')).not_to_be_visible()
                expect(page.locator('#menu-context')).to_be_hidden()
                assert len(store.menus())==1 and store.state()['meals']==1
                page.locator('#upload-menu').click()
                page.locator('[data-use-menu]').first.click()
                expect(page.locator('#current-menu')).to_contain_text('文字单人餐')
                preserved_state=store.state()
                page.locator('#new-chat').click()
                expect(page.locator('#welcome')).to_be_visible()
                expect(page.locator('#welcome-description')).to_contain_text('上一餐已完成')
                expect(page.locator('#menu-context')).to_be_hidden()
                assert page.evaluate('readNeeds()')==DEFAULTS
                preserved_state['history'][0]['completed']=True
                assert len(store.menus())==1 and store.state()==preserved_state
                page.reload(wait_until='networkidle')
                expect(page.locator('#welcome')).to_be_visible()
                expect(page.locator('#menu-context')).to_be_hidden()
                assert page.evaluate('readNeeds()')==DEFAULTS
                assert store.state()==preserved_state
                page.locator('#upload-menu').click()
                expect(page.locator('#saved-menus')).to_contain_text('文字单人餐')
                page.locator('[data-close="menu-dialog"]').click()
                assert not errors,errors
                print('PASS: text menus, retry preserves input, mobile layouts, reuse/delete menus without deleting meal history',flush=True)
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    main()
