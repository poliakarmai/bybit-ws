"""Test volatility gate in backtest engine.

Synthetic klines with a crash + ATR spike pattern that triggers real entries.
Verifies that atr_ratio_threshold reduces trade count.
"""

import pytest
from bybit_ws.experiments.engine import simulate


def make_klines(n_bars: int = 80, spike_start: int = 65) -> list[dict]:
    """Build synthetic klines: stable → crash → volatile recovery.

    - Bars 0..64: stable at ~100 (low ATR ~0.8).
    - Bar 65: crash to 80 (big range, triggers BUY signal via low bb_pos).
    - Bars 66..69: gradual recovery with elevated ATR.
    - Bars 70+: gap back up with large range bars (high ATR, ~3-5x baseline).
    """
    base_time = 1_700_000_000_000
    klines: list[dict] = []

    for i in range(n_bars):
        ts = base_time + i * 60_000

        if i < spike_start:
            # Stable regime
            p = 100.0
            klines.append({
                "open_time": ts, "open": p, "high": p + 0.5,
                "low": p - 0.3, "close": p,
                "volume": 100_000, "turnover": 100_000 * p,
            })
        elif i == spike_start:
            # Crash bar
            klines.append({
                "open_time": ts, "open": 100.0, "high": 100.5,
                "low": 78.0, "close": 80.0,
                "volume": 200_000, "turnover": 200_000 * 90.0,
            })
        elif i < spike_start + 5:
            # Recovery bars, still low
            base = 80.0 + (i - spike_start) * 1.0
            klines.append({
                "open_time": ts, "open": base, "high": base + 2.0,
                "low": base - 1.0, "close": base + 1.0,
                "volume": 200_000, "turnover": 200_000 * base,
            })
        else:
            # Gap up + big range bars (high ATR regime)
            base = 100.0 + (i - spike_start - 5) * 0.5
            klines.append({
                "open_time": ts, "open": base, "high": base + 8.0,
                "low": base - 6.0, "close": base + 1.0,
                "volume": 200_000, "turnover": 200_000 * base,
            })

    return klines


def test_vol_gate_reduces_trades():
    """Volatility gate should skip entries during ATR spikes → fewer trades."""
    klines = make_klines(80, spike_start=65)
    balance = 10_000.0
    symbol = "TESTUSDT"

    # Without threshold (baseline)
    trades_no_gate, equity_no_gate = simulate(klines, symbol, balance, {})
    n_no_gate = len(trades_no_gate)

    # With strict threshold (block if ATR > 0.5× baseline)
    trades_gate, equity_gate = simulate(
        klines, symbol, balance, {"atr_ratio_threshold": 0.5}
    )
    n_gate = len(trades_gate)

    # Baseline must produce trades for this to be meaningful
    assert n_no_gate > 0, f"Baseline should produce trades, got {n_no_gate}"

    # Gate must reduce trade count
    assert n_gate < n_no_gate, (
        f"Gate should reduce trades: {n_gate} >= {n_no_gate}"
    )


def test_vol_gate_backward_compat():
    """Without threshold param, simulate() behavior unchanged (2-tuple return)."""
    klines = make_klines(80, spike_start=65)
    balance = 10_000.0
    symbol = "TESTUSDT"

    result = simulate(klines, symbol, balance, {})
    assert isinstance(result, tuple)
    assert len(result) == 2
    trades, equity_curve = result
    assert isinstance(trades, list)
    assert isinstance(equity_curve, list)
    assert len(equity_curve) >= 1


def test_vol_gate_threshold_none_disabled():
    """Explicit None threshold disables gate (same as missing key)."""
    klines = make_klines(80, spike_start=65)
    balance = 10_000.0
    symbol = "TESTUSDT"

    trades_missing, _ = simulate(klines, symbol, balance, {})
    trades_none, _ = simulate(klines, symbol, balance, {"atr_ratio_threshold": None})

    assert len(trades_missing) == len(trades_none), (
        "Explicit None should behave like missing key"
    )
