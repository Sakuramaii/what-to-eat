"""Chat UI checks with a temporary database and mocked models; no paid calls."""
from pathlib import Path
import json
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright,expect
from assistant import AppError,DEFAULTS,basic_parse,basic_recommend,candidates
from server import ThreadingHTTPServer,create_handler
from storage import Store


def model(_system,payload,**kwargs):
    if 'candidates' in payload:
        plans=basic_recommend(payload['candidates'],payload['needs'],payload['memories'],payload.get('meal_recency'))
        return {'recommendations':[dict(id=p['id'],strategy=p['strategy'],reason='符合本次条件，并照顾久未吃过的口味。',tradeoff='价格、时间与口味各有取舍。') for p in plans]}
    if 'current' in payload:
        return {'changes':basic_parse(payload['text'],payload['current'])}
    if 'allowed_memory_tags' in payload:
        if '记住' in payload['text']:
            return {'reply':'你对清淡口味的偏好很明确。','memory_proposal':{'tag':'清淡','direction':1}}
        if '只等了' in payload['text']:
            return {'reply':'这次排队时间记下了。','feedback_draft':{'wait':8}}
        return {'reply':'这次味道和分量都合心意。','feedback_draft':{'taste':5,'portion':4,'value':4,'wait':10,'note':'味道不错，分量很足。'}}
    return {'reply':'可以从口味和主食换起。'}


def chat_model(_system,payload,**kwargs):
    if '测试超时' in payload['text']:
        raise AppError('测试：AI 响应超时', 'timeout', 504)
    needs=payload['current_needs']
    pool,_=candidates(needs)
    plans=basic_recommend(pool,needs,[])
    return json.dumps(dict(reply='可以先从口味和主食换起，试试久未吃过的搭配。',changes={},
        recommendations=[dict(id=p['id'],strategy=p['strategy'],reason='适合这次的用餐条件。',tradeoff='价格与口味各有取舍。') for p in plans]))


def main():
    output=Path(__file__).resolve().parents[1]/'artifacts'
    output.mkdir(exist_ok=True)
    errors=[]
    with tempfile.TemporaryDirectory() as folder,patch('assistant.ai_json',side_effect=model),patch('assistant.ai_text',side_effect=chat_model),patch('server.model_catalog',return_value={'models':['GLM-4-Flash','Qwen3.5-27B'],'default_model':'GLM-4-Flash','source':'platform'}):
        store=Store(Path(folder)/'browser.sqlite3')
        server=ThreadingHTTPServer(('127.0.0.1',0),create_handler(store))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto('http://127.0.0.1:'+str(server.server_port),wait_until='networkidle')
                expect(page.locator('.recommendation-card')).to_have_count(52)
                for field in ['budget','minutes','people','purpose']:
                    expect(page.locator('#'+field)).to_have_value('')
                flags=page.locator('.recommendation-card').evaluate_all('(els)=>els.map(el=>el.dataset.recommended)')
                assert flags[:3]==['true']*3 and all(flag=='false' for flag in flags[3:])
                assert store.state()['meals']==0
                assert page.locator('#recent-meals,#memory-preview-content,#meal-count,#feedback-count').count()==0
                assert page.locator('.app-shell').evaluate('(el)=>getComputedStyle(el).gridTemplateColumns')=='360px 720px 360px'
                expect(page.locator('#recommendations-footer')).to_contain_text('52 项')
                page.locator('#option-search').fill('清芬一楼炒饭')
                expect(page.locator('.recommendation-card')).to_have_count(1)
                expect(page.locator('.recommendation-card')).to_contain_text('¥10–15')
                expect(page.locator('.recommendation-card [data-metric]')).to_have_count(5)
                for field,value in dict(distance='近',price='较便宜',taste='不太好吃',portion='较少',speed='快').items():
                    expect(page.locator('[data-metric="'+field+'"] .metric-value')).to_have_text(value)
                expect(page.locator('.recommendation-card')).not_to_contain_text('/5')
                page.locator('[data-detail]').click()
                expect(page.locator('#detail-title')).to_have_text('清芬一楼炒饭')
                expect(page.locator('#detail-content')).to_contain_text('饭量 较少')
                expect(page.locator('#quote-total')).to_contain_text('¥10–15')
                expect(page.locator('#confirm-meal')).to_be_enabled()
                page.locator('[data-close="detail-dialog"]').click()
                page.locator('#option-search').fill('')
                page.screenshot(path=str(output/'personal-canteens-desktop.png'))
                print('PASS: 52 real entries, blank conditions, ratings/ranges, exact-name search, read-only preview',flush=True)

                page.locator('#edit-conditions').click()
                page.locator('#budget-slider').focus()
                page.locator('#budget-slider').press('ArrowRight')
                expect(page.locator('#budget')).to_have_value('1')
                page.locator('#budget').fill('28.50')
                expect(page.locator('#budget-slider')).to_have_value('28.5')
                page.locator('#budget-slider').press('Home')
                expect(page.locator('#budget')).to_have_value('')
                expect(page.locator('#budget-slider-value')).to_have_text('未设置')
                page.locator('#minutes').fill('180')
                expect(page.locator('#minutes-slider')).to_have_value('180')
                rect=page.locator('#minutes-slider').bounding_box()
                page.mouse.click(rect['x']+rect['width']*.5,rect['y']+rect['height']/2)
                assert 70<int(page.locator('#minutes').input_value())<110
                page.locator('#minutes').fill('')
                page.locator('#people-slider').focus()
                page.locator('#people-slider').press('End')
                expect(page.locator('#people')).to_have_value('12')
                page.locator('#people').fill('2')
                expect(page.locator('#people-slider')).to_have_value('2')
                page.screenshot(path=str(output/'conditions-sliders-desktop.png'))
                print('PASS: slider dragging, keyboard control, precise direct input and clearing limits',flush=True)

                def send(text,mode='chat'):
                    page.locator('#chat-mode').select_option(mode)
                    page.locator('#chat-input').fill(text)
                    page.locator('#send-button').click()
                    expect(page.locator('#send-button')).to_be_enabled()

                send('两个人，人均25元，今天吃少一点，慢慢吃')
                expect(page.locator('#people')).to_have_value('2')
                expect(page.locator('#portion-goal')).to_have_value('少一些')
                expect(page.locator('#pace-goal')).to_have_value('慢慢吃')
                expect(page.locator('#result-mode')).to_contain_text('AI 推荐置顶')
                assert store.state()['meals']==0
                page.locator('#model-button').click()
                page.locator('#model-search').fill('qwen')
                expect(page.locator('.model-option')).to_have_count(1)
                page.locator('[data-model="Qwen3.5-27B"]').click()
                expect(page.locator('#model-label')).to_have_text('Qwen3.5-27B')
                send('两个人，人均25元，帮我选一餐','meal')
                expect(page.locator('.chosen-label')).to_have_count(1)
                assert store.state()['meals']==1
                saved=store.history()[0]
                assert saved['plan']['total'] is None and saved['plan']['total_high']<=5000
                assert saved['needs']['portion']=='少一些' and saved['needs']['pace']=='慢慢吃'
                page.locator('[data-choose]').first.click()
                expect(page.locator('.chosen-label')).to_have_count(1)
                assert store.state()['meals']==1 and store.history()[0]['id']==saved['id']
                send('这次味道很好，口味5分','feedback')
                assert store.state()['feedback_count']==1 and store.memories()
                expect(page.locator('#result-mode')).to_contain_text('规则推荐置顶')
                page.locator('#personal-data').click()
                expect(page.locator('.history-item')).to_have_count(1)
                expect(page.locator('#drawer-content')).not_to_contain_text('¥0')
                page.locator('[data-feedback]').first.click()
                expect(page.locator('#rating-fields')).to_contain_text('很好吃')
                expect(page.locator('#rating-fields')).to_contain_text('很满意')
                page.locator('input[name="taste"][value="好吃"]').check()
                page.locator('#save-feedback').click()
                expect(page.locator('#feedback-dialog')).not_to_be_visible()
                assert store.history()[0]['feedback']['taste']=='好吃'
                page.locator('[data-close="drawer"]').click()
                print('PASS: all modes update options, small/slow goals, model selection, automatic meal and direct feedback',flush=True)

                send('一个人，人均30元，不赶时间')
                expect(page.locator('.recommendation-card')).to_have_count(52)
                pending=page.locator('.recommendation-card').filter(has=page.locator('.choose-option',has_text='核对价格')).first
                pending.locator('[data-choose]').click()
                expect(page.locator('#quote-error')).to_contain_text('实际单价')
                expect(page.locator('#confirm-meal')).to_be_disabled()
                page.locator('#rated-actual-price').fill('35')
                expect(page.locator('#quote-error')).to_contain_text('超过预算')
                expect(page.locator('#confirm-meal')).to_be_disabled()
                page.locator('#rated-actual-price').fill('28.35')
                expect(page.locator('#quote-total')).to_contain_text('¥28.35')
                expect(page.locator('#confirm-meal')).to_be_enabled()
                page.screenshot(path=str(output/'personal-canteens-price.png'))
                page.locator('#confirm-meal').click()
                expect(page.locator('#detail-dialog')).not_to_be_visible()
                assert store.state()['meals']==2 and store.history()[0]['plan']['total']==2835
                send('测试超时，人均15元')
                expect(page.locator('#error-box')).to_be_visible()
                expect(page.locator('#chat-input')).to_have_value('测试超时，人均15元')
                expect(page.locator('.recommendation-card')).to_have_count(9)
                assert store.state()['meals']==2
                send('不吃香菜')
                expect(page.locator('.empty-state')).to_contain_text('配料')
                assert page.locator('.recommendation-card').count()==0
                assert store.state()['meals']==2
                print('PASS: pending price guard, exact current-meal amount, over-budget rejection, timeout and unknown ingredients',flush=True)

                page.locator('#edit-conditions').click()
                page.locator('#avoid').fill('')
                page.locator('#budget').fill('25')
                send('一个人，人均25元')
                page.locator('#needs-settings').evaluate('(el)=>el.open=false')
                for width in [1440,1024,768,390,320]:
                    page.set_viewport_size({'width':width,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    if width<=700:
                        page.locator('[data-panel="right"]').click()
                        expect(page.locator('#right-sidebar')).to_have_class('sidebar right-sidebar panel-open')
                        assert page.locator('#right-sidebar').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                        assert page.locator('.card-metrics').first.evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                        assert page.locator('.card-metric').count()==page.locator('.recommendation-card').count()*5
                        page.screenshot(path=str(output/('personal-canteens-mobile-'+str(width)+'.png')))
                        page.locator('#right-sidebar [data-panel-close]').click()
                    if width<=999:
                        page.locator('[data-panel="left"]').click()
                        page.locator('#needs-settings').evaluate('(el)=>el.open=true')
                        assert page.locator('#left-sidebar').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                        if width==390:
                            page.screenshot(path=str(output/'conditions-sliders-mobile.png'))
                        page.locator('#left-sidebar [data-panel-close]').click()
                page.reload(wait_until='networkidle')
                assert store.state()['meals']==2 and len(store.venues())==52
                page.set_viewport_size({'width':1440,'height':1000})
                old_messages=page.locator('.message').count()
                before_new_chat=store.state()
                preserved_needs=page.evaluate('readNeeds()')
                preserved_active=page.evaluate("sessionStorage.getItem('eat-active-meal')")
                preserved_options=page.locator('.recommendation-card').evaluate_all('(els)=>els.map(el=>el.dataset.planId)')
                preserved_mode=page.locator('#chat-mode').input_value()
                page.route('**/api/new-chat',lambda route:route.fulfill(status=503,content_type='application/json',body=json.dumps(dict(error='测试：暂时无法清空'))))
                page.locator('#new-chat').click()
                expect(page.locator('#toast')).to_contain_text('原有内容已保留')
                assert page.locator('.message').count()==old_messages and store.state()['meals']==2
                assert page.evaluate('readNeeds()')==preserved_needs
                assert page.evaluate("sessionStorage.getItem('eat-active-meal')")==preserved_active
                assert page.locator('.recommendation-card').evaluate_all('(els)=>els.map(el=>el.dataset.planId)')==preserved_options
                page.unroute('**/api/new-chat')
                page.locator('#new-chat').click()
                expect(page.locator('#welcome')).to_be_visible()
                expect(page.locator('#welcome-description')).to_contain_text('上一餐已完成')
                expect(page.locator('.message')).to_have_count(0)
                assert page.evaluate('readNeeds()')==DEFAULTS
                expect(page.locator('.recommendation-card')).to_have_count(52)
                expect(page.locator('.chosen-label')).to_have_count(0)
                for field in ['budget','minutes','people']:
                    expect(page.locator('#'+field+'-slider')).to_have_value('0')
                expect(page.locator('#chat-mode')).to_have_value(preserved_mode)
                expect(page.locator('#model-label')).to_have_text('Qwen3.5-27B')
                completed_id=json.loads(preserved_active)['mealId'] if preserved_active and json.loads(preserved_active) else before_new_chat['history'][0]['id']
                next(meal for meal in before_new_chat['history'] if meal['id']==completed_id)['completed']=True
                assert store.state()==before_new_chat
                assert json.loads(page.evaluate("sessionStorage.getItem('eat-chat-v2')"))==[]
                assert json.loads(page.evaluate("sessionStorage.getItem('eat-active-meal')")) is None
                page.locator('#personal-data').click()
                expect(page.locator('.history-item')).to_have_count(before_new_chat['meals'])
                page.locator('[data-drawer-tab="memories"]').click()
                for memory in before_new_chat['memories']:
                    expect(page.locator('#drawer-content')).to_contain_text(memory['label'])
                page.locator('[data-close="drawer"]').click()
                # Simulate another old tab restoring a stale meal session.
                page.evaluate('(value)=>sessionStorage.setItem("eat-active-meal",value)',preserved_active)
                page.reload(wait_until='networkidle')
                expect(page.locator('#welcome')).to_be_visible()
                assert Store(store.path).state()==before_new_chat
                assert json.loads(page.evaluate("sessionStorage.getItem('eat-active-meal')")) is None
                request_needs=page.evaluate('readNeeds()')
                assert request_needs==DEFAULTS
                page.locator('#chat-input').fill('你好，重新开始')
                with page.expect_response(lambda response:response.url.endswith('/api/chat')) as response:
                    page.locator('#send-button').click()
                assert response.value.request.post_data_json['messages']==[]
                assert response.value.request.post_data_json['needs']==request_needs
                expect(page.locator('#send-button')).to_be_enabled()
                page.locator('#new-chat').click()
                expect(page.locator('#welcome')).to_be_visible()
                expect(page.locator('#welcome-description')).to_contain_text('开始新一餐')
                send('十几块，有点赶，和室友一起，没什么胃口')
                expect(page.locator('#budget')).to_have_value('15')
                expect(page.locator('#minutes')).to_have_value('20')
                expect(page.locator('#people')).to_have_value('2')
                expect(page.locator('#portion-goal')).to_have_value('少一些')
                expect(page.locator('#needs-summary .inference-badge')).to_have_count(3)
                expect(page.locator('.recommendation-card')).to_have_count(9)
                page.locator('#inferred-conditions summary').click()
                expect(page.locator('#inferred-reasons')).to_contain_text('和室友一起')
                page.screenshot(path=str(output/'inferred-conditions-desktop.png'))
                page.locator('#edit-conditions').click()
                page.locator('#budget').fill('25')
                expect(page.locator('#budget')).not_to_have_class('inferred-value')
                expect(page.locator('#needs-summary .inference-badge')).to_have_count(2)
                send('按现在的条件选就行')
                expect(page.locator('.recommendation-card')).to_have_count(42)
                expect(page.locator('#budget')).to_have_value('25')
                expect(page.locator('#needs-summary .inference-badge')).to_have_count(2)
                purpose_inference=page.evaluate('readNeeds().inferred.purpose')
                send('人均25元，40分钟，三个人，正常饭量，正常速度')
                expect(page.locator('#minutes')).to_have_value('40')
                expect(page.locator('#people')).to_have_value('3')
                expect(page.locator('#portion-goal')).to_have_value('适中')
                expect(page.locator('#needs-summary .inference-badge')).to_have_count(0)
                assert page.evaluate('readNeeds().inferred.purpose')==purpose_inference
                send('下一餐便宜一点，和同学一起，没胃口','feedback')
                expect(page.locator('#budget')).to_have_value('20')
                expect(page.locator('#people')).to_have_value('2')
                expect(page.locator('#portion-goal')).to_have_value('少一些')
                assert store.state()['meals']==before_new_chat['meals']
                send('和室友一起，十几块就好，赶时间，没胃口','meal')
                expect(page.locator('.chosen-label')).to_have_count(1)
                assert store.history()[0]['needs']['inferred']['budget']['value']==15
                for width in [390,320]:
                    page.set_viewport_size({'width':width,'height':844})
                    page.locator('[data-panel="left"]').click()
                    assert page.locator('#left-sidebar').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
                    page.screenshot(path=str(output/('inferred-conditions-mobile-'+str(width)+'.png')))
                    page.locator('#left-sidebar [data-panel-close]').click()
                page.set_viewport_size({'width':1440,'height':1000})
                final_state=store.state()
                page.locator('#new-chat').click()
                expect(page.locator('#welcome')).to_be_visible()
                expect(page.locator('#welcome-description')).to_contain_text('上一餐已完成')
                assert page.evaluate('readNeeds()')==DEFAULTS
                assert json.loads(page.evaluate("sessionStorage.getItem('eat-active-meal')")) is None
                expect(page.locator('#inferred-conditions')).to_be_hidden()
                final_state['history'][0]['completed']=True
                assert store.state()==final_state
                page.screenshot(path=str(output/'new-chat-preserved-memory-desktop.png'))
                send('按现在的条件选就行','meal')
                assert store.state()['meals']==final_state['meals']+1
                assert store.history()[0]['id'] not in {meal['id'] for meal in final_state['history']}
                assert store.history()[1:]==final_state['history']
                page.locator('#new-chat').click()
                expect(page.locator('#welcome')).to_be_visible()
                with page.expect_response(lambda response:response.url.endswith('/api/recommend')) as noodle_response:
                    send('今天想吃面，可以吃什么','meal')
                noodle_request=noodle_response.value.request.post_data_json
                assert noodle_request['text']=='今天想吃面，可以吃什么'
                assert noodle_request['messages']==[]
                expect(page.locator('#foods')).to_have_value('面')
                expect(page.locator('#condition-tags')).to_contain_text('想吃：面')
                expect(page.locator('.recommendation-card')).to_have_count(5)
                assert all('面' in text for text in page.locator('.card-dishes').all_text_contents())
                assert '面' in store.history()[0]['plan']['items'][0]['name']
                page.screenshot(path=str(output/'noodles-desktop.png'))
                page.locator('#edit-conditions').click()
                page.locator('#foods').fill('米粉')
                send('按当前条件推荐')
                expect(page.locator('.recommendation-card')).to_have_count(4)
                assert all('米粉' in text for text in page.locator('.card-dishes').all_text_contents())
                print('PASS: noodle requests reach the recommendation call, all options match, and food conditions remain editable',flush=True)
                print('PASS: all modes apply fuzzy intent before selection, editable estimate labels, explicit corrections and mobile layout',flush=True)
                assert not errors,errors
                print('PASS: new chat clears model context and conditions, preserves long-term memory after reload, and prevents overwriting previous meals',flush=True)
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=='__main__':
    main()
