"""Kill switch: cancel-all + верификация + эскалация (аудит #3, 25.10.2026).

Контракты:
  1. emergency_close_all вызывает cancel_all_orders ДО закрытия позиций.
  2. После закрытия — повторный fetch_positions + fetch_orders.
  3. При failed>0 / positions_left>0 / orders_left>0 — add_alert('STOP', ...).
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_modules(monkeypatch):
    """test_position_idx.py трогает sys.modules['bybit_ws.api'] на импорте —
    после него атрибут bybit_ws.api в пакете может указывать на stub.
    Переимпортируем api/risk_manager и патчим ОБА места: атрибут пакета и sys.modules.
    """
    import importlib
    import bybit_ws
    # выкидываем кеш пакета и sys.modules, чтобы получить настоящие модули
    for k in list(sys.modules):
        if k.startswith('bybit_ws.') and k != 'bybit_ws':
            sys.modules.pop(k, None)
    for attr in ('api', 'risk_manager', 'alerts'):
        if attr in bybit_ws.__dict__:
            del bybit_ws.__dict__[attr]
    api_mod = importlib.import_module('bybit_ws.api')
    rm = importlib.import_module('bybit_ws.risk_manager')
    alerts_mod = importlib.import_module('bybit_ws.alerts')
    yield (api_mod, rm, alerts_mod)


# ── 1. cancel-all вызывается до закрытия ──────────────────────────────────
def test_kill_switch_cancels_orders_first(monkeypatch, _fresh_modules):
    api_mod, rm, _alerts = _fresh_modules
    calls = []

    def fake_cancel_all():
        calls.append('cancel_all')
        return True, 5

    def fake_bybit(method, path, body=None, **kw):
        calls.append(f'bybit:{path}')
        return {'retCode': 0, 'result': {}}

    def fake_fetch_positions():
        calls.append('fetch_positions')
        return {}

    def fake_fetch_orders():
        calls.append('fetch_orders')
        return {}

    monkeypatch.setattr(api_mod, 'cancel_all_orders', fake_cancel_all)
    monkeypatch.setattr(api_mod, 'fetch_positions', fake_fetch_positions)
    monkeypatch.setattr(api_mod, 'fetch_orders', fake_fetch_orders)
    monkeypatch.setattr(api_mod, 'bybit', fake_bybit)

    rm.emergency_close_all('test')

    assert calls[0] == 'cancel_all', f'cancel-all должен быть первым, а не {calls[0]}'
    assert 'fetch_positions' in calls
    assert 'fetch_orders' in calls


# ── 2. Позиции закрываются, потом верификация ──────────────────────────────
def test_kill_switch_closes_and_verifies(monkeypatch, _fresh_modules):
    api_mod, rm, _alerts = _fresh_modules
    calls = []

    def fake_cancel_all():
        calls.append('cancel_all')
        return True, 3

    def fake_bybit(method, path, body=None, **kw):
        if path == '/v5/order/create':
            calls.append(f'close:{body["symbol"]}')
        return {'retCode': 0, 'result': {}}

    pos_calls = [0]
    def fake_fetch_positions():
        pos_calls[0] += 1
        if pos_calls[0] == 1:
            return {'BTCUSDT': {'size': '0.001', 'side': 'Buy', 'positionIdx': 0}}
        return {}  # после закрытия — пусто

    def fake_fetch_orders():
        return {}

    monkeypatch.setattr(api_mod, 'cancel_all_orders', fake_cancel_all)
    monkeypatch.setattr(api_mod, 'fetch_positions', fake_fetch_positions)
    monkeypatch.setattr(api_mod, 'fetch_orders', fake_fetch_orders)
    monkeypatch.setattr(api_mod, 'bybit', fake_bybit)

    result = rm.emergency_close_all('test')

    assert result['closed'] == 1
    assert result['failed'] == 0
    assert result['orders_cancelled'] == 3
    assert result['positions_left'] == 0
    assert result['orders_left'] == 0


# ── 3. Эскалация при failed>0 ──────────────────────────────────────────────
def test_kill_switch_escalates_on_failure(monkeypatch, _fresh_modules):
    api_mod, rm, alerts_mod = _fresh_modules
    alerts = []

    def fake_cancel_all():
        return True, 0

    def fake_bybit(method, path, body=None, **kw):
        if path == '/v5/order/create':
            return {'retCode': 10001, 'retMsg': 'position idx not match'}
        return {'retCode': 0, 'result': {}}

    def fake_fetch_positions():
        return {'ETHUSDT': {'size': '1.0', 'side': 'Sell', 'positionIdx': 0}}

    def fake_fetch_orders():
        return {}

    def fake_add_alert(level, msg):
        alerts.append((level, msg))

    monkeypatch.setattr(api_mod, 'cancel_all_orders', fake_cancel_all)
    monkeypatch.setattr(api_mod, 'fetch_positions', fake_fetch_positions)
    monkeypatch.setattr(api_mod, 'fetch_orders', fake_fetch_orders)
    monkeypatch.setattr(api_mod, 'bybit', fake_bybit)
    monkeypatch.setattr(alerts_mod, 'add_alert', fake_add_alert)

    result = rm.emergency_close_all('test')

    assert result['failed'] == 1
    assert len(alerts) == 1
    assert alerts[0][0] == 'STOP'
    assert 'KILL SWITCH VERIFY FAILED' in alerts[0][1]


# ── 4. Эскалация при оставшихся ордерах ────────────────────────────────────
def test_kill_switch_escalates_on_orders_left(monkeypatch, _fresh_modules):
    api_mod, rm, alerts_mod = _fresh_modules
    alerts = []

    def fake_cancel_all():
        return True, 2

    def fake_fetch_positions():
        return {}

    def fake_fetch_orders():
        return {'XRPUSDT_123': {'symbol': 'XRPUSDT', 'kind': 'LIMIT_ENTRY'}}

    def fake_add_alert(level, msg):
        alerts.append((level, msg))

    monkeypatch.setattr(api_mod, 'cancel_all_orders', fake_cancel_all)
    monkeypatch.setattr(api_mod, 'fetch_positions', fake_fetch_positions)
    monkeypatch.setattr(api_mod, 'fetch_orders', fake_fetch_orders)
    monkeypatch.setattr(alerts_mod, 'add_alert', fake_add_alert)

    result = rm.emergency_close_all('test')

    assert result['orders_left'] == 1
    assert len(alerts) == 1
    assert 'KILL SWITCH VERIFY' in alerts[0][1]

