"""Тесты чистых функций SMA200-гейта (split-half walk-forward эксперимент).

Проверяется ТОЛЬКО логика sma/classify_regime/bucket/gate из
bybit_ws.experiments.sma200_walkforward. Сети и engine.simulate здесь нет —
сам эксперимент запускается отдельно:
    python3 -m bybit_ws.experiments.sma200_walkforward

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_sma200_walkforward.py -q
    cd /home/openclaw/bybit-ws && python3 tests/test_sma200_walkforward.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bybit_ws.experiments.sma200_walkforward import (  # noqa: E402
    bucket, classify_regime, gate, sma,
)


def test_sma_length_and_warmup():
    values = [float(v) for v in range(1, 11)]
    out = sma(values, period=3)
    assert len(out) == len(values)
    assert out[:2] == [None, None]
    # Первое значение — на индексе period-1 (правило «None где j + 1 < period»),
    # т.е. окно ЗАКАНЧИВАЕТСЯ на j: out[2] = (1+2+3)/3. Лаг в один бар
    # (out[3] = 2.0) этому правилу противоречит — подробности в отчёте.
    assert out[2] == 2.0
    assert out[3] == 3.0  # (2+3+4)/3
    assert out[-1] == 9.0  # (8+9+10)/3


def test_sma_constant_series_is_exact():
    out = sma([1.0] * 300, period=200)
    assert out[198] is None
    assert out[199] == 1.0
    assert all(v == 1.0 for v in out[199:])


def test_classify_regime():
    assert classify_regime(101.0, 100.0) == "BULL"
    assert classify_regime(100.0, 100.0) == "BEAR"  # равенство — не BULL
    assert classify_regime(99.0, 100.0) == "BEAR"
    assert classify_regime(101.0, None) == "UNKNOWN"


def test_bucket_groups_by_side_and_regime():
    trades = [
        {"side": "Buy", "entry_time": 1, "pnl": 10.0},
        {"side": "Buy", "entry_time": 2, "pnl": -4.0},
        {"side": "Sell", "entry_time": 3, "pnl": 6.0},
        {"side": "Sell", "entry_time": 4, "pnl": -2.0},
    ]
    regime_at = {1: "BULL", 2: "BEAR", 3: "BULL", 4: "BEAR"}
    b = bucket(trades, regime_at)

    assert set(b) == {("Buy", "BULL"), ("Buy", "BEAR"), ("Sell", "BULL"), ("Sell", "BEAR")}
    assert sum(v["n"] for v in b.values()) == 4
    assert b[("Buy", "BULL")] == {"n": 1, "pnl": 10.0, "wins": 1,
                                  "gross_win": 10.0, "gross_loss": 0.0}
    # убыток положителен по модулю
    assert b[("Buy", "BEAR")]["gross_loss"] == 4.0
    assert b[("Buy", "BEAR")]["gross_win"] == 0.0
    assert b[("Buy", "BEAR")]["pnl"] == -4.0


def test_bucket_unknown_entry_time_lands_in_unknown():
    trades = [{"side": "Buy", "entry_time": 99, "pnl": -1.0},
              {"side": "Buy", "pnl": -1.0}]  # entry_time отсутствует
    b = bucket(trades, {})
    assert b[("Buy", "UNKNOWN")]["n"] == 2
    assert b[("Buy", "UNKNOWN")]["gross_loss"] == 2.0


def test_gate_keeps_only_allowed_side_regime():
    trades = [
        {"side": "Buy", "entry_time": 1, "pnl": 1.0},    # LONG + BULL → pass
        {"side": "Buy", "entry_time": 2, "pnl": 1.0},    # LONG + BEAR → drop
        {"side": "Sell", "entry_time": 3, "pnl": 1.0},   # SHORT + BEAR → pass
        {"side": "Sell", "entry_time": 4, "pnl": 1.0},   # SHORT + BULL → drop
        {"side": "Buy", "entry_time": 5, "pnl": 1.0},    # режим неизвестен → drop
        {"side": "Buy", "pnl": 1.0},                     # нет entry_time → drop
    ]
    regime_at = {1: "BULL", 2: "BEAR", 3: "BEAR", 4: "BULL"}
    allowed = {"Buy": {"BULL"}, "Sell": {"BEAR"}}
    kept = gate(trades, regime_at, allowed)
    assert [t["entry_time"] for t in kept] == [1, 3]


if __name__ == "__main__":
    for f in (test_sma_length_and_warmup, test_sma_constant_series_is_exact,
              test_classify_regime, test_bucket_groups_by_side_and_regime,
              test_bucket_unknown_entry_time_lands_in_unknown,
              test_gate_keeps_only_allowed_side_regime):
        f()
    print("OK: 6/6")
