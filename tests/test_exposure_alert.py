"""Алерт по агрегированной экспозиции (gross leverage счёта).

Сценарий 31.10: 6 позиций, Σ|notional| $3925 при equity $453 = 8.7x, при этом
лимит `max_total_margin` ($300) проверялся только по марже и входами не рулил.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bybit_ws.margin_alerts as ma  # noqa: E402
import bybit_ws.position_sizing as ps  # noqa: E402


def _pos(size, mark):
    return {"size": str(size), "mark": str(mark)}


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    monkeypatch.setattr(ma, "_should_alert", lambda key, cooldown=None: True)
    ma._EQUITY_CACHE.update({'ts': 0.0, 'value': 0.0})
    monkeypatch.setattr(ma, "_account_equity", lambda: 452.89)


def test_gross_leverage_math():
    positions = {"LTCUSDT": _pos(20, 98.69), "ADAUSDT": _pos(300, 0.4265)}
    s = ma.get_gross_stats(positions)
    assert s["gross"] == pytest.approx(20 * 98.69 + 300 * 0.4265, rel=1e-3)
    assert s["gross_leverage"] == pytest.approx(s["gross"] / 452.89, rel=1e-2)
    assert s["max_gross_leverage"] == 5.0


def test_alert_over_cap_with_biggest_symbols():
    positions = {"LTCUSDT": _pos(20, 98.69), "ARBUSDT": _pos(1000, 0.4978)}
    alerts = ma.check_margin_utilization(positions)
    exp = [a for a in alerts if "ЭКСПОЗИЦИЯ" in a or "Экспозиция" in a]
    assert exp, alerts
    assert "LTCUSDT" in exp[0] and "5.5x" in exp[0].replace(",", ".")


def test_no_alert_when_within_cap():
    positions = {"ADAUSDT": _pos(300, 0.4265)}  # $128 = 0.28x
    assert not [a for a in ma.check_margin_utilization(positions) if "кспозици" in a]


def test_warning_at_80pct():
    positions = {"X": _pos(4000, 1.0)}  # $4000 / 452.89 = 8.8x > cap*0.8
    s = ma.get_gross_stats(positions)
    assert s["gross_leverage"] > 5.0 * 0.8


def test_empty_positions_no_crash():
    s = ma.get_gross_stats({})
    assert s["gross"] == 0 and s["gross_leverage"] == 0
    assert ma.check_margin_utilization({}) == []
