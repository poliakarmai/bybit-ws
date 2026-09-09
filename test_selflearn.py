#!/usr/bin/env python3
"""Unit-тесты self-learning bybit-ws.

Запуск:  cd ~/bybit-ws && python3 test_selflearn.py
(НЕ pytest — ломается из-за корневого __init__.py, см. скилл bybit-ws-maintenance Питфол 65.)

Покрывают разрывы, найденные 09.09.2026:
- adapter SQL-фильтр должен грузить только auto (исторический баг: historical/legacy искажали PnL)
- canary-гейт (should_use_canary / get_canary_param) при неактивном canary
- _finalize_canary: rollback при <5 сделок, promote при явном преимуществе
- _get_regime_tp_levels: инвариант 3 множителя >0
"""
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent


class TestAdapterSqlFilter(unittest.TestCase):
    """adapter.py:33 — SQL должен грузить только strategy='auto' (исключает historical/legacy/imported)."""

    def test_sql_filters_strategy_auto(self):
        adapter_src = (REPO / "bybit_ws" / "journal" / "adapter.py").read_text(encoding="utf-8")
        self.assertIn("strategy = 'auto'", adapter_src,
                      "adapter SQL должен фильтровать strategy='auto'")
        self.assertNotIn("strategy != 'imported'", adapter_src,
                         "старый фильтр (грузил historical/legacy) не должен вернуться")


class TestRegimeTpLevels(unittest.TestCase):
    """auto_tp.py:44 — _get_regime_tp_levels() возвращает 3 ATR-множителя >0."""

    def test_returns_three_positive_multipliers(self):
        from bybit_ws.auto_tp import _get_regime_tp_levels
        # mock predict_regime — не дёргаем LSTM/torch (без env → ложный "HMAC mismatch" на fallback-ключе)
        with mock.patch("bybit_ws.lstm_regime.predict_regime", return_value={"regime": "NEUTRAL"}):
            levels, regime = _get_regime_tp_levels()
        self.assertEqual(len(levels), 3, f"ожидали 3 TP-уровня, получили {levels}")
        self.assertEqual(regime, "NEUTRAL")
        for k in levels:
            self.assertGreater(k, 0, "ATR-множитель TP должен быть >0")


class TestCanaryGate(unittest.TestCase):
    """self_learn.py:396/400 — canary-гейт при неактивном canary."""

    def _sl(self):
        from bybit_ws.journal import self_learn as sl
        return sl

    def test_should_use_canary_false_when_inactive(self):
        sl = self._sl()
        with mock.patch.object(sl, "is_canary_active", return_value=False):
            self.assertIs(sl.should_use_canary(), False)

    def test_get_canary_param_returns_baseline_when_inactive(self):
        sl = self._sl()
        with mock.patch.object(sl, "is_canary_active", return_value=False):
            self.assertEqual(sl.get_canary_param("min_score", 30), 30)
            self.assertEqual(sl.get_canary_param("tp_mult", 1.0), 1.0)
            self.assertEqual(sl.get_canary_param("sl_pct", 5.0, symbol="BTCUSDT"), 5.0)


class TestFinalizeCanary(unittest.TestCase):
    """self_learn.py:497 — _finalize_canary: rollback <5 сделок, promote при явном преимуществе."""

    def _sl(self):
        from bybit_ws.journal import self_learn as sl
        return sl

    def _base_state(self, trades, wins):
        return {
            "active": True,
            "started_at": None,
            "canary_trades": trades,
            "canary_wins": wins,
            "baseline_wr": 0.5,
            "baseline_trades": trades,
            "promoted": False,
            "rolled_back": False,
            "history": [],
            "symbol_params": {},
            "params": {"tp_mult": 1.2},
        }

    def test_rollback_insufficient_data(self):
        # <5 сделок → rolled_back, без обращения к bayesian
        sl = self._sl()
        state = self._base_state(2, 1)
        with mock.patch.object(sl, "_save_canary_state"), \
             mock.patch.object(sl, "update_symbol_profile"), \
             mock.patch.object(sl, "_log_canary_decision"):
            sl._finalize_canary(state)
        self.assertTrue(state["rolled_back"], "мало сделок → должен откатиться")
        self.assertFalse(state["promoted"])

    def test_promote_when_clearly_better(self):
        # 10/10 wins vs baseline 50% → Bayesian P(better)>0.95 → promote
        sl = self._sl()
        state = self._base_state(10, 10)
        with mock.patch.object(sl, "_save_canary_state"), \
             mock.patch.object(sl, "update_symbol_profile"), \
             mock.patch.object(sl, "_log_canary_decision"):
            sl._finalize_canary(state)
        self.assertTrue(state["promoted"], "явное преимущество → должен промоутиться")
        self.assertFalse(state["rolled_back"])

    def test_rollback_when_clearly_worse(self):
        # 0/10 wins vs baseline 90% → P(better)<0.05 → rollback
        sl = self._sl()
        state = self._base_state(10, 0)
        state["baseline_wr"] = 0.9
        with mock.patch.object(sl, "_save_canary_state"), \
             mock.patch.object(sl, "update_symbol_profile"), \
             mock.patch.object(sl, "_log_canary_decision"):
            sl._finalize_canary(state)
        self.assertTrue(state["rolled_back"], "явно хуже → должен откатиться")
        self.assertFalse(state["promoted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
