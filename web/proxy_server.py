#!/usr/bin/env python3
"""Прокси: отдаёт dashboard.html и проксирует RPC к bybit-ws."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, os

RPC = 'http://127.0.0.1:8766'
WEB = os.path.dirname(os.path.abspath(__file__))

class P(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/health', '/positions', '/metrics'):
            try:
                r = urllib.request.urlopen(RPC + self.path, timeout=5)
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
