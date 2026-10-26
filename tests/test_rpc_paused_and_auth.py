"""Регрессия: пауза блокирует POST /enter; сравнение токенов — constant-time.

Контракт (rpc.py):
  - `_guard_paused(rpc_state)` → причина отказа или None; `_handle_enter` отвечает 409
    при `rpc_state['paused']` (kill switch / /pause), а `_handle_close` паузой НЕ блокируется.
  - `_check_auth` сравнивает токен и `X-Emergency-Auth` через `hmac.compare_digest`.

Сервер не поднимается: обработчики вызываются на фейковом носителе wfile/send_response.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_rpc_paused_and_auth.py -q
"""
import inspect
import io
import os
import sys

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402

rpc = pytest.importorskip('bybit_ws.rpc')

TOKEN = 'a1b2c3d4-test-token-uuid'


class _FakeHandler:
    """Минимальный носитель wfile/send_response — без сокета и ThreadingHTTPServer."""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.status = None
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


def _auth_handler(headers):
    """RPCHandler без BaseHTTPRequestHandler.__init__: `_check_auth` читает только headers."""
    h = object.__new__(rpc.RPCHandler)
    h.headers = headers
    return h


@pytest.fixture(autouse=True)
def _restore_state():
    saved = dict(rpc.rpc_state)
    yield
    rpc.rpc_state.clear()
    rpc.rpc_state.update(saved)


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(rpc, '_get_auth_token', lambda: TOKEN)
    return TOKEN


# ── (а) пауза блокирует вход ────────────────────────────────
def test_guard_paused_passes_when_running():
    assert rpc._guard_paused({'paused': False}) is None
    assert rpc._guard_paused({}) is None


def test_guard_paused_returns_reason_when_paused():
    msg = rpc._guard_paused({'paused': True, 'paused_reason': 'kill_switch activated'})
    assert msg and 'paused' in msg.lower()
    assert 'kill_switch activated' in msg
    assert 'resume' in msg.lower()


def test_enter_409_when_paused(monkeypatch):
    """После kill switch /enter отбивается 409 и до биржи не доходит."""
    rpc.rpc_state.update({'paused': True, 'paused_reason': 'kill_switch activated'})
    touched = []
    monkeypatch.setattr(rpc, '_get_position', lambda s: touched.append(('position', s)))
    monkeypatch.setattr(rpc, '_api_call', lambda *a, **k: touched.append(('api', a)))

    h = _FakeHandler()
    rpc.RPCHandler._handle_enter(h, {'symbol': 'XRPUSDT', 'side': 'Buy', 'qty': 10, 'confirm': True})

    assert h.status == 409
    assert b'paused' in h.wfile.getvalue().lower()
    assert touched == []


def test_close_not_blocked_by_pause():
    """Закрывать позиции на паузе разрешено — гард стоит только во входах."""
    assert '_guard_paused' not in inspect.getsource(rpc.RPCHandler._handle_close)


# ── (б) _check_auth: поведение ──────────────────────────────
def test_auth_accepts_correct_token(token):
    assert _auth_handler({'Authorization': 'Bearer ' + token})._check_auth() is True


def test_auth_rejects_wrong_token(token):
    assert _auth_handler({'Authorization': 'Bearer wrong-token'})._check_auth() is False


def test_auth_rejects_missing_or_empty_token(token):
    assert _auth_handler({})._check_auth() is False
    assert _auth_handler({'Authorization': 'Bearer '})._check_auth() is False
    assert _auth_handler({'Authorization': 'Basic ' + token})._check_auth() is False


def test_auth_rejects_when_server_token_missing(monkeypatch):
    monkeypatch.setattr(rpc, '_get_auth_token', lambda: '')
    assert _auth_handler({'Authorization': 'Bearer ' + TOKEN})._check_auth() is False


def test_auth_uses_compare_digest():
    """Поведенческие тесты выше проходят и с `==`, поэтому фиксируем сам примитив."""
    src = inspect.getsource(rpc.RPCHandler._check_auth)
    assert 'hmac.compare_digest' in src
    assert 'provided == token' not in src


# ── (б) X-Emergency-Auth ────────────────────────────────────
def test_emergency_auth_requires_matching_header(token, monkeypatch):
    monkeypatch.setenv('EMERGENCY_TOKEN', 'emg-secret')
    hdr = {'Authorization': 'Bearer ' + token, 'X-Emergency-Auth': 'emg-secret'}
    assert _auth_handler(hdr)._check_auth(require_emergency=True) is True

    bad = dict(hdr, **{'X-Emergency-Auth': 'nope'})
    assert _auth_handler(bad)._check_auth(require_emergency=True) is False

    missing = {'Authorization': 'Bearer ' + token}
    assert _auth_handler(missing)._check_auth(require_emergency=True) is False
