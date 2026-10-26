"""Проводка дневных метрик в async-движке (metrics.json).

Регрессия, которую ловим: `record_alert()` вызывался только из легаси
`main.py`, поэтому после перехода на `main_async` счётчики TP/SL/ENTRY
замерли на дате последнего запуска синхронного движка.
"""
import pytest

import bybit_ws.main_async as ma


def _fake_trade(sym, pnl, hold_h):
    """Закрытая сделка в формате Bybit /v5/position/closed-pnl."""
    closed_ms = 1_700_000_000_000
    return {
        'symbol': sym,
        'side': 'Buy',
        'qty': '10',
        'avgEntryPrice': '100',
        'avgExitPrice': '100',
        'closedPnl': str(pnl),
        'openFee': '0.01',
        'closeFee': '0.01',
        'leverage': '5',
        'orderId': f'ord-{sym}',
        'createdTime': str(closed_ms - int(hold_h * 3600 * 1000)),
        'updatedTime': str(closed_ms),
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    alerts = []
    monkeypatch.setattr(ma, 'record_alert', lambda level, is_false=False, symbol=None: alerts.append((level, symbol)))
    monkeypatch.setattr(ma.sync_db, 'add_trade', lambda **kw: None)
    monkeypatch.setattr(ma.sync_db, 'get_entry_diagnostic_for_close', lambda *a, **kw: {})
    # exit_reason теперь идёт от SL/TP позиции (не от знака PnL) — уровни, которые
    # «держал» бот для фейковых сделок ниже.
    _levels = {
        'BTCUSDT': {'sl': 95.0, 'tp': 100.0},    # выход 100 → TP
        'ETHUSDT': {'sl': 100.0, 'tp': 110.0},   # выход 100 → SL
    }
    monkeypatch.setattr(ma.sync_db, 'get_position_sltp',
                        lambda sym, side, entry_price=None: _levels.get(sym))
    import bybit_ws.journal.self_learn as sl
    for name in ('record_exit', 'record_trade_result', 'match_canary_entry',
                 'record_canary_result', 'on_trade_closed'):
        monkeypatch.setattr(sl, name, lambda *a, **kw: None, raising=False)
    return alerts, tmp_path


def _run(monkeypatch, data_dir, trades):
    monkeypatch.setattr(ma, 'bybit', None, raising=False)
    import bybit_ws.api as api_mod
    monkeypatch.setattr(api_mod, 'bybit', lambda *a, **kw: {'retCode': 0, 'result': {'list': trades}})
    return ma._import_bybit_trades(data_dir)


def test_tp_sl_counts_written(monkeypatch, harness):
    alerts, tmp = harness
    trades = [
        _fake_trade('BTCUSDT', 5.0, 1.0),    # TP
        _fake_trade('ETHUSDT', -3.0, 2.0),   # SL
        _fake_trade('OLDUSDT', -1.0, 60.0),  # Time — не TP/SL
    ]
    assert _run(monkeypatch, tmp, trades) == 3
    assert ('TP', 'BTCUSDT') in alerts
    assert ('SL', 'ETHUSDT') in alerts
    assert all(sym != 'OLDUSDT' for _, sym in alerts), 'Time-закрытия не считаются ни TP, ни SL'


def test_dedup_second_run_adds_nothing(monkeypatch, harness):
    alerts, tmp = harness
    trades = [_fake_trade('BTCUSDT', 5.0, 1.0)]
    assert _run(monkeypatch, tmp, trades) == 1
    assert _run(monkeypatch, tmp, trades) == 0
    assert alerts == [('TP', 'BTCUSDT')], 'повторный импорт не должен накручивать счётчик'
