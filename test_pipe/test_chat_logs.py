"""Разовая проверка: лог обмена с сервером списания попадает в ответ pipe."""
import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location(
    'pipe', r'C:\ПРОЕКТЫ\Сервер списания кредитов\openwebui_pipe_credits.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        body = {
            "choices": [{"message": {"content": "Тестовый ответ модели на ваш вопрос."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


srv = HTTPServer(('127.0.0.1', 4099), FakeModel)
threading.Thread(target=srv.serve_forever, daemon=True).start()

p = m.Pipe()
p.valves.API_BASE_URL = 'http://127.0.0.1:4099'
p.valves.MODEL_NAME = 'test-model'
p.valves.API_KEY = 'sk-fake-for-test'
p.valves.CREDITS_SERVER_URL = 'http://127.0.0.1:4010'
p.valves.CREDITS_API_KEY = 'devbim2026'

body = {"messages": [{"role": "user", "content": "привет"}]}
user = {"email": "test@dev-bim.com", "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}

out = asyncio.run(p.pipe(body, user))
print('=== ОТВЕТ, КАК ЕГО УВИДИТ ПОЛЬЗОВАТЕЛЬ В ЧАТЕ ===')
print(out)
print('=== КОНЕЦ ===')

# и ветка ошибки обмена: сервер списания недоступен
p.valves.CREDITS_SERVER_URL = 'http://127.0.0.1:4999'  # никто не слушает
out2 = asyncio.run(p.pipe({"messages": [{"role": "user", "content": "ещё раз"}]}, user))
print('\n=== ВЕТКА ОШИБКИ (сервер списания недоступен) ===')
print(out2[-400:])
