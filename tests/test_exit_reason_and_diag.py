"""exit_reason по механизму выхода + диагностика входа по стороне ВХОДА.

Два дефекта наблюдаемости:
1. exit_reason выводился из знака PnL ('TP' на убытке) — здесь проверяем, что
   метка идёт от уровня SL/TP, который мы держали, а без уровней — 'Unknown'.
2. trade_history/closed-pnl живут стороной ЗАКРЫТИЯ, а entry_diag — стороной
   ВХОДА: LONG (закрытие 'Sell') обязан находить diag, записанный как 'Buy'.
"""
import pytest

import bybit_ws.main_async as ma
from bybit_ws.state_db import StateDB


# ── (а) механизм выхода по цене против SL/TP ───────────────────────────

def test_exit_at_tp_level_is_tp():
    assert ma.classify_exit(105.0, sl=95.0, tp=105.2, hold_hours=3) == 'TP'


def test_exit_at_sl_level_is_sl():
    assert ma.classify_exit(95.1, sl=95.0, tp=110.0, hold_hours=3) == 'SL'


def test_exit_far_from_levels_is_unknown():
    """Ни SL, ни TP: 'Unknown', даже если сделка в плюсе (раньше был 'TP')."""
    assert ma.classify_exit(102.0, sl=95.0, tp=110.0, hold_hours=3) == 'Unknown'


def test_exit_without_levels_is_unknown_not_pnl():
    """SL/TP не нашли — честный 'Unknown' при любом PnL."""
    assert ma.classify_exit(120.0, sl=None, tp=None, hold_hours=1) == 'Unknown'
    assert ma.classify_exit(80.0, sl=None, tp=None, hold_hours=1) == 'Unknown'


def test_long_hold_without_levels_is_time():
    assert ma.classify_exit(102.0, sl=None, tp=None, hold_hours=49) == 'Time'


def test_level_beats_time():
    """Уровень важнее длительности: выход по SL через 60ч — это SL, а не Time."""
    assert ma.classify_exit(95.05, sl=95.0, tp=110.0, hold_hours=60) == 'SL'


def test_tolerance_is_relative_and_bounded():
    """0.3% — допуск на проскальзывание; 0.5% уже не «наш уровень»."""
    assert ma.classify_exit(105.2 * 1.002, sl=None, tp=105.2, hold_hours=1) == 'TP'
    assert ma.classify_exit(105.2 * 1.005, sl=None, tp=105.2, hold_hours=1) == 'Unknown'


# ── SL/TP «на память»: переживают закрытие позиции ─────────────────────

def test_sltp_memory_survives_position_close(tmp_path):
    db = StateDB(path=str(tmp_path / 'sltp.db'))
    db.save_positions({'BTCUSDT': {'side': 'Buy', 'entry': 100.0, 'mark': 101.0,
                                   'size': 1.0, 'stopLoss': 95.0, 'takeProfit': 110.0}})
    got = db.get_position_sltp('BTCUSDT', 'Buy', entry_price=100.0)
    assert got['sl'] == 95.0
    assert got['tp'] == 110.0
    db.close()


def test_sltp_memory_of_other_position_is_not_used(tmp_path):
    """Ре-вход по другой цене — это другая позиция, чужие уровни не берём."""
    db = StateDB(path=str(tmp_path / 'sltp2.db'))
    db.save_positions({'BTCUSDT': {'side': 'Buy', 'entry': 100.0,
                                   'stopLoss': 95.0, 'takeProfit': 110.0}})
    assert db.get_position_sltp('BTCUSDT', 'Buy', entry_price=200.0) is None
    db.close()


def test_free_form_tp_not_erased_by_snapshot(tmp_path):
    """Ручной TP — обычный limit-ордер: в снепшоте позиции его нет, память не трогаем."""
    db = StateDB(path=str(tmp_path / 'sltp3.db'))
    db.save_position_sltp('BTCUSDT', 'Buy', 95.0, 110.0, entry=100.0)
    db.save_positions({'BTCUSDT': {'side': 'Buy', 'entry': 100.0,
                                   'stopLoss': 96.0, 'takeProfit': None}})
    got = db.get_position_sltp('BTCUSDT', 'Buy', entry_price=100.0)
    assert (got['sl'], got['tp']) == (96.0, 110.0)
    db.close()


# ── (б) сопоставление стороны при закрытии ─────────────────────────────

def test_long_close_finds_buy_entry_diag(tmp_path):
    """LONG: вход 'Buy', закрытие приходит как 'Sell'."""
    db = StateDB(path=str(tmp_path / 'diag.db'))
    db.save_entry_diagnostic('BTCUSDT', 'Buy', bb_pct=12.5, rsi=28.0,
                             entry_reason='score=4/8', score=4, min_score=8,
                             regime='TRENDING_UP', leverage=5, margin=20.0)
    diag = db.get_entry_diagnostic_for_close('BTCUSDT', 'Sell')
    assert diag['bb_pct'] == 12.5
    assert diag['rsi'] == 28.0
    assert diag['entry_reason'] == 'score=4/8'
    assert diag['score'] == 4 and diag['min_score'] == 8
    assert diag['regime'] == 'TRENDING_UP'
    assert diag['leverage'] == 5 and diag['margin'] == 20.0
    db.close()


def test_short_close_finds_sell_entry_diag(tmp_path):
    """SHORT: вход 'Sell', закрытие приходит как 'Buy'."""
    db = StateDB(path=str(tmp_path / 'diag2.db'))
    db.save_entry_diagnostic('QUSDT', 'Sell', bb_pct=91.0, entry_reason='weekly_junk')
    assert db.get_entry_diagnostic_for_close('QUSDT', 'Buy')['bb_pct'] == 91.0
    db.close()


# ── сквозной прогон импорта closed-pnl (без сети, БД в tmp) ────────────

def _closed_trade(sym, entry, exit_price, pnl, hold_h, side='Sell'):
    closed_ms = 1_700_000_000_000
    return {
        'symbol': sym, 'side': side, 'qty': '10',
        'avgEntryPrice': str(entry), 'avgExitPrice': str(exit_price),
        'closedPnl': str(pnl), 'openFee': '0.01', 'closeFee': '0.01',
        'leverage': '5', 'orderId': f'ord-{sym}-{int(exit_price * 100)}',
        'createdTime': str(closed_ms - int(hold_h * 3600 * 1000)),
        'updatedTime': str(closed_ms),
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    db = StateDB(path=str(tmp_path / 'state.db'))
    monkeypatch.setattr(ma, 'sync_db', db)
    monkeypatch.setattr(ma, 'record_alert', lambda *a, **kw: None)

    import bybit_ws.manual_positions as mp
    monkeypatch.setattr(mp, 'is_manual_position', lambda sym: False)

    import bybit_ws.journal.self_learn as sl
    for name in ('record_exit', 'record_trade_result', 'match_canary_entry',
                 'record_canary_result', 'on_trade_closed'):
        monkeypatch.setattr(sl, name, lambda *a, **kw: None, raising=False)
    return db, tmp_path


def _run_import(monkeypatch, data_dir, trades):
    import bybit_ws.api as api_mod
    monkeypatch.setattr(api_mod, 'bybit',
                        lambda *a, **kw: {'retCode': 0, 'result': {'list': trades}})
    return ma._import_bybit_trades(data_dir)


def test_import_long_exit_at_tp_gets_tp_and_entry_diag(harness, monkeypatch):
    db, tmp = harness
    db.save_positions({'BTCUSDT': {'side': 'Buy', 'entry': 100.0,
                                   'stopLoss': 95.0, 'takeProfit': 110.0}})
    db.save_entry_diagnostic('BTCUSDT', 'Buy', bb_pct=11.0, rsi=25.0,
                             entry_reason='score=5/8')
    assert _run_import(monkeypatch, tmp, [_closed_trade('BTCUSDT', 100.0, 110.0, 9.0, 2.0)]) == 1

    row = db.conn.execute(
        'SELECT side, exit_reason, rsi, bb_pct, entry_reason FROM trade_history').fetchone()
    assert row[0] == 'Sell', 'сторона закрытия LONG'
    assert row[1] == 'TP'
    assert (row[2], row[3], row[4]) == (25.0, 11.0, 'score=5/8')
    db.close()


def test_import_long_loss_not_at_sl_is_unknown(harness, monkeypatch):
    """Убыток ≠ SL: цена выхода далеко от нашего уровня → 'Unknown', не 'SL'."""
    db, tmp = harness
    db.save_positions({'ETHUSDT': {'side': 'Buy', 'entry': 100.0,
                                   'stopLoss': 90.0, 'takeProfit': 120.0}})
    assert _run_import(monkeypatch, tmp, [_closed_trade('ETHUSDT', 100.0, 97.0, -3.0, 1.0)]) == 1

    row = db.conn.execute('SELECT exit_reason FROM trade_history').fetchone()
    assert row[0] == 'Unknown'
    db.close()


def test_import_without_levels_and_with_profit_is_unknown(harness, monkeypatch):
    """Профит без известных SL/TP — 'Unknown' (PnL-фолбэка больше нет)."""
    db, tmp = harness
    assert _run_import(monkeypatch, tmp, [_closed_trade('SOLUSDT', 100.0, 104.0, 4.0, 2.0)]) == 1

    row = db.conn.execute('SELECT exit_reason FROM trade_history').fetchone()
    assert row[0] == 'Unknown'
    db.close()
