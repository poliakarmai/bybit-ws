"""Риск-постоянный ATR-сайзинг (#10) и крутилки ATR-стопа (#3).

Было: margin = risk_per_trade при любом ATR → риск сделки = notional × стоп%,
разброс ×3.3 (стоп 3% → риск $1.5, стоп 10% → $5 при $5 и 10x).
Стало: margin = риск / (стоп% × плечо) → риск в $ один и тот же.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bybit_ws.config as cfg_mod  # noqa: E402
import bybit_ws.position_sizing as ps  # noqa: E402
import bybit_ws.unified_sl as usl  # noqa: E402


def _atr(value):
    return lambda *a, **k: value


def test_risk_is_constant_across_atr(monkeypatch):
    """Риск сделки в $ одинаков при спокойной и волатильной монете."""
    risks = []
    for atr in (0.5, 2.0, 5.0, 20.0):  # стоп 0.75% (пол 3%), 3%, 7.5%, 30% (потолок 10%)
        monkeypatch.setattr(ps, "get_atr", _atr(atr))
        s = ps.atr_margin("TESTUSDT", price=100.0, leverage=10, risk_per_trade=5.0)
        risk = s["margin"] * 10 * s["stop_pct"]
        risks.append(risk)
        assert 4.0 <= risk <= 6.0, f"ATR {atr}: риск ${risk:.2f} вместо $5"
    assert max(risks) - min(risks) <= 2.0  # разброс от округления margin до 0.1


def test_stop_pct_clamped(monkeypatch):
    monkeypatch.setattr(ps, "get_atr", _atr(0.2))  # 0.3% → min 3%
    assert ps.atr_margin("TESTUSDT", price=100.0, leverage=10)["stop_pct"] == pytest.approx(0.03)
    monkeypatch.setattr(ps, "get_atr", _atr(30.0))  # 45% → max 10%
    assert ps.atr_margin("TESTUSDT", price=100.0, leverage=10)["stop_pct"] == pytest.approx(0.10)


def test_custom_clamp(monkeypatch):
    monkeypatch.setattr(ps, "get_atr", _atr(10.0))  # 1.5×10/100 = 15%
    s = ps.atr_margin("TESTUSDT", price=100.0, leverage=10, sl_clamp=(0.03, 0.20))
    assert s["stop_pct"] == pytest.approx(0.15) and s["clamped"] is False


def test_legacy_without_price(monkeypatch):
    """Без price/leverage — старое поведение (margin = risk_per_trade)."""
    monkeypatch.setattr(ps, "get_atr", _atr(3.0))
    s = ps.atr_margin("TESTUSDT", risk_per_trade=5.0)
    assert s["margin"] == pytest.approx(5.0) and s["method"] == "atr"
    assert "stop_pct" not in s


def test_no_atr_is_error(monkeypatch):
    monkeypatch.setattr(ps, "get_atr", _atr(0.0))
    assert ps.atr_margin("TESTUSDT", price=100.0)["margin"] == 0


def test_unified_sl_defaults_and_config(monkeypatch):
    monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **k: {})
    assert usl._sl_params() == (1.5, 0.03, 0.10)

    monkeypatch.setattr(cfg_mod, "get_config",
                        lambda *a, **k: {"sl": {"atr_multiplier": 2.0, "max_pct": 0.2}})
    mult, lo, hi = usl._sl_params()
    assert (mult, lo, hi) == (2.0, 0.03, 0.2)


def test_default_sl_uses_config_multiplier(monkeypatch):
    """Множитель из конфига реально доходит до стоп-цены (entry 100, ATR 4 → 8%)."""
    monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **k: {"sl": {"atr_multiplier": 2.0}})
    monkeypatch.setattr("bybit_ws.auto_tp._get_atr_value", _atr(4.0))
    target, desc = usl._calc_default_sl("TESTUSDT", {}, True, 100.0, 100.0, 10)
    assert target == pytest.approx(92.0) and desc.startswith("ATR SL 8.0%")
