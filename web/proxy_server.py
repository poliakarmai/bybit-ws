#!/usr/bin/env python3
"""Прокси: отдаёт dashboard.html и проксирует RPC к bybit-ws."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, os, subprocess

RPC = 'http://127.0.0.1:8766'
WEB = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')

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

class P(BaseHTTPRequestHandler):
    TOKEN = ''  # кеш токена

    def do_GET(self):
        if self.path in ('/health', '/positions', '/metrics'):
            if not self.TOKEN:
                self.TOKEN = _get_token()
            try:
                req = urllib.request.Request(RPC + self.path)
                if self.TOKEN:
                    req.add_header('Authorization', f'Bearer {self.TOKEN}')
                r = urllib.request.urlopen(req, timeout=5)
                data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(f'{{"error":"{e}"}}'.encode())
        elif self.path in ('/', '/dashboard.html'):
            with open(os.path.join(WEB, 'dashboard.html'), 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'404')

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 9997), P).serve_forever()
