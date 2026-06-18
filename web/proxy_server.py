#!/usr/bin/env python3
"""Прокси: отдаёт dashboard.html и проксирует RPC к bybit-ws."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, os, json

RPC = 'http://127.0.0.1:8766'
WEB = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')

# Эндпоинты для проксирования (с авторизацией)
PROXY_PATHS = {
    '/health', '/positions', '/metrics', '/risk', '/orders',
    '/signals', '/rpc/all', '/rpc/alerts', '/rpc/trades',
}

# Глобальный кеш токена
_AUTH_TOKEN = ''


def _get_token():
    """Достать RPC-токен из state.db."""
    import sqlite3
    try:
        db = sqlite3.connect(os.path.join(DATA_DIR, 'state.db'))
        row = db.execute("SELECT value FROM kv_store WHERE key='rpc_auth_token'").fetchone()
        db.close()
        return row[0] if row else ''
    except Exception:
        return ''


def _proxy_rpc(handler, path):
    """Проксировать запрос к RPC с авторизацией."""
    global _AUTH_TOKEN
    if not _AUTH_TOKEN:
        _AUTH_TOKEN = _get_token()

    req = urllib.request.Request(RPC + path)
    if _AUTH_TOKEN:
        req.add_header('Authorization', 'Bearer ' + _AUTH_TOKEN)
    r = urllib.request.urlopen(req, timeout=5)
    data = r.read()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    handler.wfile.write(data)


class P(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.rstrip('/') or '/'

        # Статика
        if path in ('/', '/dashboard.html'):
            with open(os.path.join(WEB, 'dashboard.html'), 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
            return

        # Проксирование RPC
        if path in PROXY_PATHS or path.startswith('/rpc/'):
            try:
                _proxy_rpc(self, path)
            except Exception as e:
                self.send_response(502)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'404')

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 9999), P).serve_forever()
