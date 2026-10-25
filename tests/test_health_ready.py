"""#6: /health отдаёт trading_ready = not paused and not circuit_breaker.

Хендлер вызывается напрямую на фейковом handler'е (без сокета) — ответ
перехватывается по wfile.write.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_health_ready.py -q
"""
import json
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


class _WFile:
    def __init__(self):
        self.body = b''

    def write(self, b):
        self.body += b


class _FakeHandler:
    """Минимум для _handle_health: заголовки и тело ответа."""

    def __init__(self):
        self.wfile = _WFile()
        self.code = None

    def send_response(self, code):
        self.code = code

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.body.decode())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    rpc = pytest.importorskip('bybit_ws.rpc')
    rm = pytest.importorskip('bybit_ws.risk_manager')
    # Свой DATA_DIR: health.txt из боевого каталога не должен влиять на alive.
    monkeypatch.setattr(rpc, 'DATA_DIR', tmp_path)
    monkeypatch.setitem(rpc.rpc_state, 'paused', False)
    monkeypatch.setattr(rm, 'is_circuit_breaker_active', lambda: False)
    return rpc, rm


def _health(rpc):
    h = _FakeHandler()
    rpc.RPCHandler._handle_health(h)
    assert h.code == 200
    return h.payload()


def test_trading_ready_when_running(env):
    rpc, _rm = env
    body = _health(rpc)
    assert body['trading_ready'] is True
    assert body['paused'] is False
    assert body['circuit_breaker_active'] is False


def test_not_ready_when_paused(env, monkeypatch):
    rpc, _rm = env
    monkeypatch.setitem(rpc.rpc_state, 'paused', True)
    body = _health(rpc)
    assert body['paused'] is True
    assert body['trading_ready'] is False


def test_not_ready_when_circuit_breaker_active(env, monkeypatch):
    rpc, rm = env
    monkeypatch.setattr(rm, 'is_circuit_breaker_active', lambda: True)
    body = _health(rpc)
    assert body['circuit_breaker_active'] is True
    assert body['trading_ready'] is False


def test_stale_but_ready_is_possible(env):
    """alive (свежий цикл) и trading_ready — разные признаки, не путать."""
    rpc, _rm = env
    body = _health(rpc)
    assert body['alive'] is False  # health.txt в tmp_path нет
    assert body['trading_ready'] is True
