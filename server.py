#!/usr/bin/env python3
"""Run with python3 server.py. No package installation required."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from assistant import (AppError, DEFAULTS, chat_reply, chat_recommendations, conversation_needs, load_key,
                       make_plan, model_catalog, normalize, parse_needs, recommend, resolve_model, violations)
from catalog import public_catalog
from menus import recognize_menu, recommend_import, validate_menu, vision_model
from storage import Store
from food_intent import food_changes

ROOT = Path(__file__).resolve().parent


def create_handler(store):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

        def log_message(self, fmt, *args):
            # Avoid user input, credential paths and upstream exception payloads in logs.
            pass

        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            host = self.headers.get('Host', '')
            allowed = {'localhost', '127.0.0.1', '[::1]'}
            if host.split(':')[0] not in allowed and not host.startswith('[::1]:'):
                raise AppError('请通过 localhost 或 127.0.0.1 访问本地助手', 'origin', 403)
            origin = self.headers.get('Origin')
            if (origin and origin not in {'http://' + host, 'https://' + host}) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise AppError('不接受来自其他网页的请求', 'origin', 403)

        def do_GET(self):
            try:
                self.local_request()
                path = urlsplit(self.path).path
                if path == '/api/catalog':
                    catalog = public_catalog()
                    catalog['venues'] = store.venues()
                    return self.send_json(catalog)
                if path == '/api/state':
                    return self.send_json(store.state())
                if path == '/api/models':
                    return self.send_json(model_catalog(refresh=urlsplit(self.path).query == 'refresh=1'))
                if path == '/api/status':
                    try:
                        load_key()
                        configured = True
                    except AppError:
                        configured = False
                    return self.send_json(dict(configured=configured, model=resolve_model(), vision_model=vision_model()))
                if path.startswith('/api/'):
                    raise AppError('接口不存在', 'not_found', 404)
                # Only public application assets; never serve Python, SQLite or credentials.
                if path not in ['/', '/index.html', '/styles.css', '/app.js', '/favicon.svg', '/food.svg']:
                    raise AppError('页面不存在', 'not_found', 404)
                return super().do_GET()
            except AppError as exc:
                self.send_json(dict(error=exc.message, code=exc.code), exc.status)

        def do_POST(self):
            try:
                self.local_request()
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise AppError('请发送 JSON 请求')
                try:
                    path = urlsplit(self.path).path
                    size = int(self.headers.get('Content-Length', '0'))
                    limit = 8_500_000 if path == '/api/menus/import' else 262144 if path == '/api/menus/update' else 65536
                    if size < 1 or size > limit:
                        raise ValueError()
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise AppError('请求数据无效或过大')
                venues = store.venues()
                if path == '/api/new-chat':
                    # Conversation messages live in the browser and are sent with
                    # each request. Keep the completed meal as durable history.
                    completed = store.complete_meal(data.get('active_meal_id'))
                    needs = normalize(DEFAULTS, venues)
                    return self.send_json(dict(state=store.state(), needs=needs, completed_meal_id=completed,
                        recommendations=recommend(needs, store.memories(), store.dish_recency(), mode='basic', venues=venues)))
                if path in ['/api/menus/import', '/api/menus/update']:
                    if path.endswith('/import'):
                        menu, changes, used_model, selection = recognize_menu(data)
                    else:
                        old = next((v for v in venues if v.get('imported') and v['id'] == data.get('menu_id')), None)
                        if not old:
                            raise AppError('找不到这份菜单', 'not_found', 404)
                        menu = validate_menu(data.get('menu'), old['id'])
                        changes, used_model, selection = {}, None, None
                    updated_venues = [v for v in venues if v['id'] != menu['id']] + [menu]
                    raw_needs = dict(data.get('needs') or DEFAULTS, scene='menu', venue_id=menu['id'])
                    recognition_notice = None
                    try:
                        needs = conversation_needs(data.get('requirements', ''), raw_needs, changes=changes, venues=updated_venues)
                    except AppError:
                        needs = conversation_needs(data.get('requirements', ''), raw_needs, venues=updated_venues)
                        recognition_notice = '部分需求未能识别为有效条件，已保留原条件并提取明确的预算、人数等限制。'
                    store.save_menu(menu)
                    result = recommend_import(menu, needs, store.memories(), store.dish_recency(), updated_venues, selection, used_model)
                    return self.send_json(dict(menu=menu, needs=needs, recommendations=result,
                                               recognition_model=used_model, recognition_notice=recognition_notice, venues=store.venues()), 201)
                if path == '/api/menus/delete':
                    store.delete_menu(data.get('menu_id'))
                    return self.send_json(dict(venues=store.venues()))
                if path == '/api/chat':
                    needs = normalize(data.get('needs', DEFAULTS), venues)
                    model = resolve_model(data.get('model'))
                    history = store.history()
                    active = next((m['id'] for m in history if m['id'] == data.get('active_meal_id') and not m['feedback'] and not m['completed']), None)
                    recent = store.dish_recency(active)
                    try:
                        result = chat_reply(data.get('text'), data.get('messages', []), store.memories(), history,
                                            data.get('mode', 'chat'), data.get('meal_id'), model, needs, recent, venues)
                    except AppError as exc:
                        if exc.code not in ['upstream', 'timeout', 'connection', 'ai_format', 'configuration']:
                            raise
                        fallback_needs = conversation_needs(data['text'], needs, data.get('mode', 'chat'), venues=venues)
                        fallback = recommend(fallback_needs, store.memories(), recent, mode='basic', venues=venues)
                        return self.send_json(dict(error=exc.message, code=exc.code, needs=fallback_needs,
                                                   recommendations=fallback), exc.status)
                    if result['mode'] == 'feedback':
                        result.update(store.apply_chat_feedback(result))
                        result['state'] = store.state()
                        if result['saved_feedback']:
                            recent = store.dish_recency()
                    result['recommendations'] = chat_recommendations(result, store.memories(), recent, model, venues)
                    return self.send_json(result)
                if path in ['/api/parse', '/api/recommend']:
                    mode = data.get('mode', 'ai')
                    if mode not in ['ai', 'basic']:
                        raise AppError('推荐模式无效')
                    if path == '/api/parse':
                        return self.send_json(parse_needs(data.get('text'), data.get('needs'), mode, resolve_model(data.get('model')), venues))
                    exclude = data.get('exclude', [])
                    if not isinstance(exclude, list) or len(exclude) > 200 or any(not isinstance(i, str) or len(i) > 1000 for i in exclude):
                        raise AppError('换一批参数无效')
                    record = data.get('record', False)
                    if not isinstance(record, bool):
                        raise AppError('记录参数无效')
                    current_meal = store.session_meal(data.get('meal_session')) if record else None
                    needs = normalize(data.get('needs'), venues)
                    text = data.get('text','')
                    if not isinstance(text,str) or len(text)>2000:
                        raise AppError('推荐原始需求格式不正确')
                    if text:
                        needs = normalize(dict(needs,**food_changes(text,needs)),venues)
                    result = recommend(needs, store.memories(), store.dish_recency(current_meal), exclude, mode, resolve_model(data.get('model')), venues,
                                       text=text, messages=data.get('messages'))
                    result['needs'] = needs
                    if record and result['plans']:
                        chosen = next(p for p in result['plans'] if p['id'] == result['primary_id'])
                        result['meal_id'] = store.assume_meal(chosen, needs, data['meal_session'])
                        result['state'] = store.state()
                    return self.send_json(result)
                if path in ['/api/quote', '/api/meals']:
                    needs = normalize(data.get('needs'), venues)
                    plan = make_plan(data.get('venue_id'), data.get('items'), needs, venues, data.get('actual_prices'))
                    issues = violations(plan, needs, venues)
                    if path == '/api/quote':
                        return self.send_json(dict(plan=plan, violations=issues))
                    if issues:
                        raise AppError('当前组合无法确认：' + '、'.join(issues) + '。请调整菜品或条件。', 'constraint', 422)
                    if data.get('meal_id'):
                        mid = store.update_meal(data['meal_id'], plan, needs)
                    elif data.get('meal_session'):
                        mid = store.assume_meal(plan, needs, data['meal_session'])
                        store.update_meal(mid, plan, needs)
                    else:
                        mid = store.save_meal(plan, needs, data.get('request_id'))
                    return self.send_json(dict(id=mid, state=store.state()), 201)
                if path == '/api/meals/cancel':
                    store.cancel_meal(data.get('meal_id'), data.get('cancelled', True))
                elif path == '/api/feedback':
                    store.feedback(data.get('meal_id'), data.get('feedback'))
                elif path == '/api/learning':
                    store.toggle_learning(data.get('meal_id'), data.get('enabled'))
                elif path == '/api/preferences':
                    if data.get('action') == 'reset':
                        store.reset_preference(data.get('tag'))
                    else:
                        store.preference(data.get('tag'), data.get('direction'))
                elif path == '/api/reset':
                    if data.get('confirm') != '清空本地记录':
                        raise AppError('请输入确认文字后再清空')
                    store.clear()
                else:
                    raise AppError('接口不存在', 'not_found', 404)
                self.send_json(store.state())
            except AppError as exc:
                self.send_json(dict(error=exc.message, code=exc.code), exc.status)
            except (TypeError, ValueError, KeyError):
                self.send_json(dict(error='请求格式不正确，请检查输入后重试。', code='validation'), 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                # Never return a traceback or upstream payload containing authentication data.
                self.send_json(dict(error='本地服务暂时无法处理请求，请重试。', code='internal'), 500)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--db', default=str(ROOT / 'data/eat.sqlite3'))
    args = parser.parse_args()
    store = Store(args.db)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), create_handler(store))
    except OSError:
        print('无法启动端口，请使用 --port 8001 选择其他端口。', file=sys.stderr)
        return 1
    print(f'今天吃什么 · http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止本地助手。')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
