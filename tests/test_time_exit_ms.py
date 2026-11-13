"""Tests for time-exit ms/s normalization (Task A — P0 fix).

Bybit returns openTime in milliseconds. time_exit and auto_short were
computing age as (now - ts)/3600 where now is seconds and ts is ms,
producing age_hours ≈ -496_000 → time-exit never triggered.

These tests fail on the old code and pass after normalize_open_ts().
"""
import os
import sys
import time
from pathlib import Path

os.environ["BYBIT_LOG_SILENT"] = "1"
# Корень репо — от __file__, НЕ "/home/openclaw": там bybit_ws резолвится
# в живой ~/bybit-ws (симлинк), и тест проверял бы не тот код.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── normalize_open_ts ──────────────────────────────────────────

def test_normalize_ms():
    from bybit_ws.time_exit import normalize_open_ts
    # 1793346684737 ms → 1793346684.737 s
    assert abs(normalize_open_ts(1793346684737) - 1793346684.737) < 0.01


def test_normalize_seconds_unchanged():
    from bybit_ws.time_exit import normalize_open_ts
    ts = 1700000000.0  # year 2023 in seconds
    assert normalize_open_ts(ts) == ts


def test_normalize_invalid():
    from bybit_ws.time_exit import normalize_open_ts
    assert normalize_open_ts(None) == 0.0
    assert normalize_open_ts("") == 0.0
    assert normalize_open_ts("abc") == 0.0


# ── check_time_exit ────────────────────────────────────────────

def _make_pos(open_time, pnl_pct=-1.0, side="Buy"):
    """Build a position dict mimicking api.fetch_positions output."""
    entry = 100.0
    # For Buy (LONG): pnl_pct = (mark - entry)/entry * 100
    # For Sell (SHORT): pnl_pct = (entry - mark)/entry * 100
    if side == "Buy":
        mark = entry * (1 + pnl_pct / 100)
    else:
        mark = entry * (1 - pnl_pct / 100)
    return {
        "openTime": open_time,
        "entry": entry,
        "mark": mark,
        "size": 1.0,
        "side": side,
        "positionIdx": 0,
    }


def test_time_exit_ms_8h_loss():
    """openTime in ms, position 8h old, in loss → should trigger exit."""
    from bybit_ws.time_exit import check_time_exit
    open_ms = int((time.time() - 8 * 3600) * 1000)
    positions = {"TESTUSDT": _make_pos(open_ms, pnl_pct=-2.0)}
    exits = check_time_exit(positions)
    assert len(exits) == 1
    assert "TESTUSDT" in exits[0][0]


def test_time_exit_ms_30h_max_holding():
    """openTime in ms, position 30h old → triggers max-holding regardless of PnL."""
    from bybit_ws.time_exit import check_time_exit
    open_ms = int((time.time() - 30 * 3600) * 1000)
    # Even in profit, >24h forces exit
    positions = {"MAXUSDT": _make_pos(open_ms, pnl_pct=+5.0)}
    exits = check_time_exit(positions)
    assert len(exits) == 1
    assert "max holding" in exits[0][1].lower()


def test_time_exit_seconds_no_double_divide():
    """openTime already in seconds (old cache) → should NOT divide again."""
    from bybit_ws.time_exit import check_time_exit
    open_s = time.time() - 8 * 3600  # 8h ago in seconds
    positions = {"OLDUSDT": _make_pos(open_s, pnl_pct=-2.0)}
    exits = check_time_exit(positions)
    assert len(exits) == 1


def test_time_exit_profit_no_exit():
    """Position in profit and <24h → no exit."""
    from bybit_ws.time_exit import check_time_exit
    open_ms = int((time.time() - 8 * 3600) * 1000)
    positions = {"WINUSDT": _make_pos(open_ms, pnl_pct=+5.0)}
    exits = check_time_exit(positions)
    assert len(exits) == 0


# ── check_short_time_sl ────────────────────────────────────────

def test_short_time_sl_ms(monkeypatch):
    """check_short_time_sl with openTime in ms, SHORT in loss >6h → triggers."""
    from bybit_ws import alerts, api
    from bybit_ws.auto_short import check_short_time_sl

    open_ms = int((time.time() - 7 * 3600) * 1000)
    positions = {
        "SHRTUSDT": {
            "side": "Sell",
            "unrealisedPnl": -5.0,
            "openTime": open_ms,
            "size": 1.0,
            "positionIdx": 0,
        }
    }

    closed = []
    # place_order и log_event импортируются ВНУТРИ функции → патчим модули-источники.
    monkeypatch.setattr(api, "place_order",
                        lambda sym, side, type_, qty, **kw: closed.append(sym) or {"retCode": 0})
    monkeypatch.setattr(alerts, "log_event", lambda *a, **kw: None)
    monkeypatch.setattr(alerts, "add_alert", lambda *a, **kw: None)

    actions = check_short_time_sl(positions)
    assert "SHRTUSDT" in actions
    assert closed == ["SHRTUSDT"]


def test_apply_time_exits_routes_through_place_order(monkeypatch):
    """apply_time_exits обязан звать api.place_order с reduceOnly/IOC/positionIdx/te_-link."""
    import pytest

    from bybit_ws import alerts, api
    from bybit_ws.time_exit import apply_time_exits

    monkeypatch.setattr(alerts, "log_event", lambda *a, **kw: None)
    captured = []

    def fake_bybit(method, path, body=None, **kw):
        captured.append((method, path, body))
        return {"retCode": 0}

    monkeypatch.setattr(api, "bybit", fake_bybit)

    res = apply_time_exits([("LTCUSDT", "TIME EXIT LTCUSDT: 8h", 2.5, 2, "Sell")])
    assert res == {"closed": 1, "failed": 0}
    assert len(captured) == 1
    method, path, body = captured[0]
    assert (method, path) == ("POST", "/v5/order/create")
    assert body["symbol"] == "LTCUSDT"
    assert body["side"] == "Buy"
    assert body["orderType"] == "Market"
    assert body["qty"] == "2.5"
    assert body["reduceOnly"] is True
    assert body["timeInForce"] == "IOC"
    assert body["positionIdx"] == 2
    assert body["orderLinkId"].startswith("te_ltcusdt_")

    with pytest.raises(ValueError, match="position_idx"):
        api.place_order("LTCUSDT", "Buy", "Market", 1.0)
