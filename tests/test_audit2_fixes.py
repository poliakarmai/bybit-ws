"""Проверки фиксов аудита 11.2 (25.10.2026).

Контракты:
  1. check_symbol_concentration — fail-closed при недоступном/невалидном балансе.
  2. get_daily_pnl().realized_pnl — из trade_history, а не из несуществующего metrics.json['pnl_total'].
  3. api.bybit — orderLinkId генерится один раз на вызов: ретрай не создаёт второй ордер.
  4. circuit breaker — состояние сохраняется в risk_state.json (переживает рестарт).
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402

from bybit_ws import api as api_mod  # noqa: E402
from bybit_ws import risk_manager as rm  # noqa: E402


# ── 1. Концентрация fail-closed ────────────────────────────────────────────
@pytest.mark.parametrize('wallet', [None, {}, {'equity': 'n/a'}, {'equity': 0}, {'equity': -5}])
def test_concentration_fail_closed(monkeypatch, wallet):
    monkeypatch.setattr(rm, '_get_wallet_balance', lambda: wallet)
    allowed, reason, pct = rm.check_symbol_concentration('BTCUSDT', 5.0, {})
    assert allowed is False, f'wallet={wallet!r} должно блокировать вход'
    assert reason


def test_concentration_ok_with_balance(monkeypatch):
    monkeypatch.setattr(rm, '_get_wallet_balance', lambda: {'equity': 1000.0})
    allowed, _, pct = rm.check_symbol_concentration('BTCUSDT', 5.0, {})
    assert allowed is True and pct == pytest.approx(0.005)


# ── 2. realized PnL из state.db ────────────────────────────────────────────
class _Conn:
    def __init__(self, value):
        self.value = value
        self.last_sql = ''

    def execute(self, sql, params=()):
        self.last_sql = sql
        return self

    def fetchone(self):
        return (self.value,)


def test_daily_pnl_realized_from_trade_history(monkeypatch):
    conn = _Conn(-3.54)

    class _DB:
        def __init__(self):
            self.conn = conn

    monkeypatch.setattr('bybit_ws.state_db.StateDB', _DB)
    monkeypatch.setattr(rm, '_get_wallet_balance', lambda: {'equity': 100.0})
    monkeypatch.setattr(rm, '_load_json', lambda *a, **k: {}, raising=False)

    daily = rm.get_daily_pnl()
    assert daily['realized_pnl'] == pytest.approx(-3.54)
    assert 'trade_history' in conn.last_sql and 'closed_at' in conn.last_sql


# ── 3. orderLinkId один на все ретраи ──────────────────────────────────────
def test_order_link_id_stable_across_retries(monkeypatch):
    import httpx

    sent = []

    class _Resp:
        status_code = 200

        def json(self):
            return {'retCode': 0, 'result': {}}

    class _Session:
        def post(self, url, content=None, headers=None, timeout=None):
            sent.append(content.decode())
            if len(sent) == 1:
                raise httpx.TimeoutException('boom')
            return _Resp()

        def get(self, *a, **k):
            raise AssertionError('GET не ожидался')

    monkeypatch.setattr(api_mod, '_load_credentials', lambda: None)
    monkeypatch.setattr(api_mod, '_get_session', lambda: _Session())
    monkeypatch.setattr(api_mod, '_cb_check', lambda: True)
    monkeypatch.setattr(api_mod, '_cb_record', lambda ok: None)
    monkeypatch.setattr(api_mod.time, 'sleep', lambda s: None)

    r = api_mod.bybit('POST', '/v5/order/create',
                      {'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Market',
                       'qty': '0.01', 'category': 'linear'})
    assert r and r['retCode'] == 0
    assert len(sent) == 2, 'ожидался один ретрай после timeout'

    import json as _json
    ids = {_json.loads(b)['orderLinkId'] for b in sent}
    assert len(ids) == 1, f'ретрай ушёл с другим orderLinkId: {ids}'


def test_order_link_id_not_overridden(monkeypatch):
    sent = []

    class _Resp:
        status_code = 200

        def json(self):
            return {'retCode': 0}

    class _Session:
        def post(self, url, content=None, headers=None, timeout=None):
            sent.append(content.decode())
            return _Resp()

    monkeypatch.setattr(api_mod, '_load_credentials', lambda: None)
    monkeypatch.setattr(api_mod, '_get_session', lambda: _Session())
    monkeypatch.setattr(api_mod, '_cb_check', lambda: True)
    monkeypatch.setattr(api_mod, '_cb_record', lambda ok: None)

    api_mod.bybit('POST', '/v5/order/create',
                  {'symbol': 'ETHUSDT', 'orderLinkId': 'mine-123'})
    assert '"orderLinkId": "mine-123"' in sent[0]


# ── 4. Персист circuit breaker ─────────────────────────────────────────────
def test_circuit_breaker_persisted(monkeypatch):
    saved = {}
    monkeypatch.setattr(rm, '_load_risk_state', lambda: {})
    monkeypatch.setattr(rm, '_save_risk_state', lambda st: saved.update(st))
    monkeypatch.setattr(rm, 'get_daily_pnl', lambda: {'total_pnl': -100.0})
    monkeypatch.setattr(rm, '_circuit_breaker_active', False, raising=False)

    class _Cfg:
        risk = {'max_daily_loss': 50}

    monkeypatch.setattr(rm, '_cb_restored', True, raising=False)
    active, reason = rm.check_circuit_breaker(_Cfg())
    assert active is True
    assert saved.get('circuit_breaker', {}).get('active') is True
    assert reason


def test_circuit_breaker_restored(monkeypatch):
    import time as _t
    monkeypatch.setattr(rm, '_load_risk_state', lambda: {
        'circuit_breaker': {'active': True, 'ts': _t.time(), 'reason': 'daily loss'}})
    monkeypatch.setattr(rm, '_circuit_breaker_active', False, raising=False)
    monkeypatch.setattr(rm, '_cb_restored', False, raising=False)
    rm.check_circuit_breaker(type('C', (), {'risk': {'max_daily_loss': 50}})())
    assert rm._circuit_breaker_active is True
