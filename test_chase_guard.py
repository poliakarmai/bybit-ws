"""Тесты для анти-chasing guard (bybit_ws.chase_guard.is_chasing).

Покрывает: рост выше порога, ниже порога, падение, границу, edge-cases,
детерминированность.
"""

from __future__ import annotations

import pytest

from bybit_ws.chase_guard import is_chasing


class TestIsChasing:
    """Основные сценарии функции is_chasing."""

    # ── 1. Рост 5% за 5 свечей → True ──────────────────────────────
    def test_growth_above_threshold_returns_true(self):
        """Цена выросла на 5% за 5 свечей — chasing = True."""
        # base = 100, last = 105 → рост 5.0% > 3.0%
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is True

    def test_growth_hand_computed(self):
        """Hand-computed пример: base=200, last=212 → (212-200)/200*100 = 6% > 3%."""
        closes = [200.0, 203.0, 205.0, 208.0, 210.0, 212.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is True

    # ── 2. Рост 1% → False (ниже порога 3%) ───────────────────────
    def test_growth_below_threshold_returns_false(self):
        """Рост 1% при пороге 3% — chasing = False."""
        # base = 100, last = 101 → рост 1.0% <= 3.0%
        closes = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    # ── 3. Падение цены → False ────────────────────────────────────
    def test_price_drop_returns_false(self):
        """Цена упала — chasing = False."""
        closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    # ── 4. Ровно на пороге → False (строгое >) ─────────────────────
    def test_exactly_at_threshold_returns_false(self):
        """Рост ровно 3.0% при пороге 3.0% — False (строгое >)."""
        # base = 100, last = 103 → рост ровно 3.0%, НЕ > 3.0%
        closes = [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    # ── 5. None / пустой / короткий список → False ─────────────────
    def test_none_returns_false(self):
        assert is_chasing(None, pct=3.0, lookback=5) is False

    def test_empty_list_returns_false(self):
        assert is_chasing([], pct=3.0, lookback=5) is False

    def test_too_short_returns_false(self):
        """Длина < lookback+1 → False."""
        # need lookback+1 = 6 элементов, даём только 5
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    # ── 6. base == 0 → False ──────────────────────────────────────
    def test_base_zero_returns_false(self):
        """base = 0 → деление невозможно, возвращаем False."""
        closes = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    def test_base_negative_returns_false(self):
        """base < 0 → некорректная цена, возвращаем False."""
        closes = [-10.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        assert is_chasing(closes, pct=3.0, lookback=5) is False

    # ── 7. lookback <= 0 или pct <= 0 → False ─────────────────────
    def test_lookback_zero_returns_false(self):
        assert is_chasing([100.0, 200.0], pct=3.0, lookback=0) is False

    def test_lookback_negative_returns_false(self):
        assert is_chasing([100.0, 200.0], pct=3.0, lookback=-1) is False

    def test_pct_zero_returns_false(self):
        assert is_chasing([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], pct=0.0, lookback=5) is False

    def test_pct_negative_returns_false(self):
        assert is_chasing([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], pct=-1.0, lookback=5) is False

    # ── 8. Детерминированность ─────────────────────────────────────
    def test_deterministic_same_input_same_result(self):
        """Два вызова с одинаковым входом → одинаковый результат."""
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        result1 = is_chasing(closes, pct=3.0, lookback=5)
        result2 = is_chasing(closes, pct=3.0, lookback=5)
        assert result1 == result2

    def test_deterministic_multiple_calls(self):
        """10 вызовов подряд → все одинаковые."""
        closes = [50.0, 51.0, 52.0, 53.0, 54.0, 55.0]
        results = [is_chasing(closes, pct=3.0, lookback=5) for _ in range(10)]
        assert all(r == results[0] for r in results)

    # ── Дополнительные edge-cases ──────────────────────────────────
    def test_custom_lookback(self):
        """lookback=2: base = closes[-3], last = closes[-1]."""
        # base = 100, last = 104 → 4% > 3%
        closes = [100.0, 102.0, 104.0]
        assert is_chasing(closes, pct=3.0, lookback=2) is True

    def test_single_candle_lookback_1(self):
        """lookback=1: сравниваем соседние свечи."""
        # base = 100, last = 104 → 4% > 3%
        closes = [100.0, 104.0]
        assert is_chasing(closes, pct=3.0, lookback=1) is True

    def test_tuple_input(self):
        """Кортеж вместо списка — тоже работает."""
        closes = (100.0, 101.0, 102.0, 103.0, 104.0, 105.0)
        assert is_chasing(closes, pct=3.0, lookback=5) is True

    def test_integer_prices(self):
        """Целые числа (int) вместо float — тоже работает."""
        closes = [100, 101, 102, 103, 104, 110]
        assert is_chasing(closes, pct=3.0, lookback=5) is True

    def test_no_exception_on_bad_types(self):
        """Никогда не бросает исключений."""
        # Различные «плохие» входы
        assert is_chasing("abc", pct=3.0, lookback=5) is False
        assert is_chasing(42, pct=3.0, lookback=5) is False
        assert is_chasing([None, None], pct=3.0, lookback=1) is False
