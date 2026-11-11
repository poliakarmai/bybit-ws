"""Регрессы аудита 10.11.2026 (xpiki-аудит bybit-ws).

Каждый тест падает на коде ДО фикса — это и есть пруф.
"""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

import bybit_ws as bw
from bybit_ws import (
    auto_tp,
    file_utils,
    unified_sl,
)


def test_safe_json_append_keeps_history(tmp_path):
    """locked_open('a') через tempfile+replace уничтожал файл: 3 записи → 1 строка."""
    p = tmp_path / "t.jsonl"
    for i in range(3):
        file_utils.safe_json_append(str(p), {"i": i})
    assert [json.loads(line)["i"] for line in p.read_text().splitlines()] == [0, 1, 2]


def test_locked_open_append_appends(tmp_path):
    p = tmp_path / "t.log"
    p.write_text("old\n")
    with file_utils.locked_open(str(p), "a") as f:
        f.write("new\n")
    assert p.read_text() == "old\nnew\n"


def test_position_idx_is_per_symbol(monkeypatch):
    """Аккаунт смешанный: GRAM idx=0 (one-way), LTC idx=1 (hedge)."""
    monkeypatch.setattr(bw, "_HEDGE_SYMS", {"LTCUSDT"})
    assert bw.position_idx("LTCUSDT", "Buy") == 1
    assert bw.position_idx("LTCUSDT", "Sell") == 2
    assert bw.position_idx("GRAMUSDT", "Sell") == 0


def test_learn_position_idx_marks_hedge(monkeypatch):
    monkeypatch.setattr(bw, "_HEDGE_SYMS", set())
    bw.learn_position_idx("XRPUSDT", 2)
    assert bw.position_idx("XRPUSDT", "Sell") == 2
    bw.learn_position_idx("XRPUSDT", 0)
    assert bw.position_idx("XRPUSDT", "Sell") == 0


def test_qty_by_step_skips_unaffordable(monkeypatch):
    """BTC: $5 маржи × 3 при цене 74 385 = 0.0002 BTC < minOrderQty 0.001 → None."""
    from bybit_ws import auto_entry

    monkeypatch.setattr(auto_tp, "_get_qty_limits",
                        lambda s: (0.001, 0.001) if s == "BTCUSDT" else (0.1, 0.1))
    assert auto_entry._qty_by_step("BTCUSDT", 5 * 3 / 74385) is None
    assert auto_entry._qty_by_step("SOLUSDT", 5 * 3 / 84.69) == 0.1


def test_qty_by_step_never_overshoots(monkeypatch):
    from bybit_ws import auto_entry

    monkeypatch.setattr(auto_tp, "_get_qty_limits", lambda s: (0.001, 0.001))
    qty = auto_entry._qty_by_step("BTCUSDT", 0.0019)
    assert qty == 0.001  # floor, а не ceil → 0.002


def test_default_sl_inside_liquidation(monkeypatch):
    """При 25x ликвидация ≈ −3.7%; кламп 10% отправлял стоп за ликвидацию."""
    monkeypatch.setattr(auto_tp, "_get_atr_value", lambda sym: 10_000.0)  # ATR 10% цены → кламп 10%
    price = 100_000.0
    target, _ = unified_sl._calc_default_sl(
        "BTCUSDT", {"stopLoss": None}, True, price, price, 25)
    dist = 1 - target / price
    assert dist <= 0.8 / 25 + 1e-9, f"SL {dist:.3%} за ликвидацией"


def test_default_sl_without_leverage_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(auto_tp, "_get_atr_value", lambda sym: 0.0)
    target, reason = unified_sl._calc_default_sl(
        "BTCUSDT", {"stopLoss": None}, True, 100.0, 100.0, 0)
    assert target == pytest.approx(90.0)
    assert "10" in reason


def test_check_pumps_passes_full_state(monkeypatch):
    """_place_pump_short получал prev (dict одного символа) вместо state."""
    from bybit_ws import pump_detect as pd

    monkeypatch.setattr(pd, "_load_state", lambda: {"OTHERUSDT": {"peak_price": 1}})
    monkeypatch.setattr(pd, "_save_state", lambda s: None)
    monkeypatch.setattr(pd, "_cleanup_state", lambda s, now: s)
    monkeypatch.setattr(pd, "_confirm_volume_spike", lambda sym: (True, "vol x5"))
    monkeypatch.setattr(pd, "margin_for_strategy", lambda *a, **k: 10.0)
    monkeypatch.setattr(pd, "bybit", lambda *a, **k: {"retCode": 0, "result": {"list": [{
        "symbol": "FOOUSDT", "price24hPcnt": "0.95",
        "turnover24h": "5000000", "lastPrice": "1.0",
    }]}})
    seen = {}

    def fake_place(sym, price, label, state, peak):
        seen["state"] = state
        return True

    monkeypatch.setattr(pd, "_place_pump_short", fake_place)
    pd.check_pumps({})
    assert "state" in seen, "pump-short не дошёл до _place_pump_short"
    assert "OTHERUSDT" in seen["state"], "передан prev вместо state"


def test_auto_short_has_no_hardcoded_position_idx():
    """trading-stop с 'positionIdx': 0 в hedge-режиме отбивался биржей."""
    src = (ROOT / "auto_short.py").read_text()
    assert "'positionIdx': 0," not in src


def test_partial_tp_keeps_old_tp_when_new_fails(monkeypatch):
    """Сбой при выставлении новых TP не должен оставлять позицию без TP."""
    from bybit_ws import partial_tp as ptp

    monkeypatch.setattr(ptp, "_load_state", dict)
    monkeypatch.setattr(ptp, "_save_state", lambda s: None)
    monkeypatch.setattr(ptp, "get_bb_data", lambda sym: {"sma": 110.0, "upper": 120.0, "bb_pct": 60})
    monkeypatch.setattr(ptp, "calculate_split", lambda *a, **k: (0.9, 0.1))
    monkeypatch.setattr(ptp, "fetch_funding_total", lambda *a, **k: 0.0)
    monkeypatch.setattr(ptp, "fetch_orders", lambda: {
        "o1": {"symbol": "XRPUSDT", "kind": "TP", "orderId": "o1"},
        "o2": {"symbol": "XRPUSDT", "kind": "TP", "orderId": "o2"},
    })

    calls = []
    results = iter([{"retCode": 0}, {"retCode": 10001, "retMsg": "boom"}])

    def fake_place(*a, **k):
        calls.append("place")
        return next(results)

    monkeypatch.setattr(ptp, "place_order", fake_place)
    monkeypatch.setattr(ptp, "cancel_order", lambda *a, **k: calls.append("cancel"))

    ptp.check_partial_tp({"XRPUSDT": {
        "size": 10.0, "side": "Buy", "entry": 100.0, "mark": 105.0,
        "openTime": 0, "cumRealisedPnl": 0.0, "positionIdx": 0,
    }})
    assert calls == ["place", "place"], f"отмена до выставления новых TP: {calls}"


def test_qty_by_step_tiny_lot_decimal(monkeypatch):
    """str(1e-05) без точки ломал округление: 3 шага по 0.00001 → 0.0."""
    from bybit_ws import auto_entry

    monkeypatch.setattr(auto_tp, "_get_qty_limits", lambda s: (0.00001, 0.00001))
    assert auto_entry._qty_by_step("TINYUSDT", 0.00003) == pytest.approx(0.00003)
    assert auto_entry._qty_by_step("TINYUSDT", 0.00003) < 0.00004  # floor, не ceil


def test_partial_tp_cleans_partial_new_orders(monkeypatch):
    """Частичный успех: уже поставленный новый TP снимается, старые остаются."""
    from bybit_ws import partial_tp as ptp

    monkeypatch.setattr(ptp, "_load_state", dict)
    monkeypatch.setattr(ptp, "_save_state", lambda s: None)
    monkeypatch.setattr(ptp, "get_bb_data", lambda sym: {"sma": 110.0, "upper": 120.0, "bb_pct": 60})
    monkeypatch.setattr(ptp, "calculate_split", lambda *a, **k: (0.9, 0.1))
    monkeypatch.setattr(ptp, "fetch_funding_total", lambda *a, **k: 0.0)
    monkeypatch.setattr(ptp, "fetch_orders", lambda: {
        "o1": {"symbol": "XRPUSDT", "kind": "TP", "orderId": "old1"},
    })
    results = iter([{"retCode": 0, "result": {"orderId": "new1"}},
                    {"retCode": 10001, "retMsg": "boom"}])
    monkeypatch.setattr(ptp, "place_order", lambda *a, **k: next(results))
    cancelled = []
    monkeypatch.setattr(ptp, "cancel_order", lambda sym, oid: cancelled.append(oid))

    ptp.check_partial_tp({"XRPUSDT": {
        "size": 10.0, "side": "Buy", "entry": 100.0, "mark": 105.0,
        "openTime": 0, "cumRealisedPnl": 0.0, "positionIdx": 0,
    }})
    assert cancelled == ["new1"], f"уборка/старые TP: {cancelled}"


def test_auto_short_state_does_not_claim_sl_on_reject():
    """no_sl должен остаться True, если биржа отклонила trading-stop."""
    src = (ROOT / "auto_short.py").read_text()
    assert "state_entry['no_sl'] = not ts_ok" in src
    assert src.count("state_entry['no_sl'] = not ts_ok") == 2


def test_empty_positions_snapshot_clears_db(monkeypatch):
    """Закрытая позиция должна исчезать из state.db: пустой снимок — тоже факт."""
    import asyncio

    from bybit_ws import main_async as ma

    saved = []

    class FakeDB:
        def save_positions(self, positions):
            saved.append(positions)

    async def fake_fetch():
        return {}, {}

    monkeypatch.setattr(ma, "fetch_positions_and_orders", fake_fetch)
    monkeypatch.setattr(ma, "sync_db", FakeDB())
    monkeypatch.setattr(ma, "save_json", lambda path, data: None)

    # не asyncio.run: он закрывает loop и ломает тесты, живущие на get_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(ma.async_positions_snapshot())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
    assert saved == [{}], f"пустой снимок не дошёл до БД (фантомные позиции): {saved}"
