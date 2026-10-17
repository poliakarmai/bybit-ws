"""Гейты входа в backtest-движке: ширина канала BB и «энергия» (пройдено % ATR).

Оба гейта добавлены как гипотезы из материала Герчика/Носкова и оба по умолчанию
выключены (None → поведение не меняется). Свечи берём из test_vol_gate — тот же
генератор даёт реальные входы, второй генератор разъехался бы с первым.
"""

import pytest

from bybit_ws.experiments.engine import simulate
from bybit_ws.experiments.test_vol_gate import make_klines

BALANCE = 10_000.0
SYMBOL = "TESTUSDT"


def _trades(params: dict) -> int:
    trades, _ = simulate(make_klines(80, spike_start=65), SYMBOL, BALANCE, params)
    return len(trades)


# ── BB-width gate ────────────────────────────────────────────────────────────

def test_baseline_produces_trades():
    """Без гейтов движок должен торговать — иначе тесты ниже ничего не проверяют."""
    assert _trades({}) > 0


def test_impossible_width_band_blocks_everything():
    assert _trades({"bb_width_min": 1e6}) == 0


def test_width_absent_params_change_nothing():
    baseline = _trades({})
    assert _trades({"bb_width_min": None, "bb_width_max": None}) == baseline


def test_wide_width_band_keeps_all_trades():
    assert _trades({"bb_width_min": 0.0, "bb_width_max": 1e6}) == _trades({})


# ── Energy gate (растяжение хода против сделки) ───────────────────────────────

def _trend_then_drop(n_rise: int = 66, n_drop: int = 16) -> list[dict]:
    """Дневные свечи: рост на 1.0/бар, затем падение на 2.0/бар.

    Входы у такой серии — покупки на просадке, то есть цена уже растянута вниз
    сильнее ATR: именно этот случай гейт обязан отсекать.
    """
    step, ts, price = 86_400_000, 1_700_000_000_000, 100.0
    ks = []
    for i in range(n_rise):
        ks.append({"open_time": ts + i * step, "open": price, "high": price + 0.6,
                   "low": price - 0.4, "close": price + 1.0,
                   "volume": 100_000, "turnover": 100_000 * price})
        price += 1.0
    for j in range(n_drop):
        ks.append({"open_time": ts + (n_rise + j) * step, "open": price, "high": price + 0.5,
                   "low": price - 2.0, "close": price - 2.0,
                   "volume": 100_000, "turnover": 100_000 * price})
        price -= 2.0
    return ks


def _trend_trades(params: dict) -> int:
    trades, _ = simulate(_trend_then_drop(), SYMBOL, BALANCE, params)
    return len(trades)


def test_energy_gate_blocks_stretched_entries():
    """Растяжение > 0.5 ATR против сделки → входов нет вовсе."""
    assert _trend_trades({}) > 0, "серия должна давать входы"
    assert _trend_trades({"atr_travel_max": 0.5}) == 0


def test_energy_wide_threshold_keeps_trades():
    """Порог 1e6 ATR не отсекает ничего — гейт не мешает, когда выключен порогом."""
    assert _trend_trades({"atr_travel_max": 1e6}) == _trend_trades({})


def test_energy_absent_param_changes_nothing():
    assert _trend_trades({"atr_travel_max": None}) == _trend_trades({})


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
