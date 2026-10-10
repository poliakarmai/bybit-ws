"""Тесты для trade_limits.py — анти-overtrading guard'ы.

Покрывает:
- allow_by_daily_count: под лимитом → True; равно/выше лимита → False + "daily_limit"; max_daily_trades<=0 → True
- allow_after_sl_streak: серия ниже порога → True; серия выше порога и cooldown не прошёл → False + "sl_streak_cooldown"; cooldown прошёл → True; max_consecutive_sl<=0 → True; last_sl_ts=None → True
- Мусорный вход не бросает (None, "abc" вместо int)
"""

import sys
# Добавляем корень репо в sys.path, чтобы bybit_ws/ резолвился как пакет
sys.path.insert(0, '/home/openclaw/bybit-ws')

import pytest

from bybit_ws.trade_limits import allow_by_daily_count, allow_after_sl_streak


class TestAllowByDailyCount:
    """Тесты дневного лимита сделок."""

    def test_under_limit(self):
        """Под лимитом → True."""
        allowed, reason = allow_by_daily_count(5, 10)
        assert allowed is True
        assert reason == ""

    def test_at_limit(self):
        """Равно лимиту → False + "daily_limit"."""
        allowed, reason = allow_by_daily_count(10, 10)
        assert allowed is False
        assert reason == "daily_limit"

    def test_above_limit(self):
        """Выше лимита → False + "daily_limit"."""
        allowed, reason = allow_by_daily_count(15, 10)
        assert allowed is False
        assert reason == "daily_limit"

    def test_limit_disabled_zero(self):
        """max_daily_trades=0 → лимит выключен → True."""
        allowed, reason = allow_by_daily_count(100, 0)
        assert allowed is True
        assert reason == ""

    def test_limit_disabled_negative(self):
        """max_daily_trades<0 → лимит выключен → True."""
        allowed, reason = allow_by_daily_count(100, -5)
        assert allowed is True
        assert reason == ""

    def test_garbage_trades_today_none(self):
        """trades_today=None → безопасный дефолт (True, "")."""
        allowed, reason = allow_by_daily_count(None, 10)
        assert allowed is True
        assert reason == ""

    def test_garbage_trades_today_string(self):
        """trades_today="abc" → безопасный дефолт (True, "")."""
        allowed, reason = allow_by_daily_count("abc", 10)
        assert allowed is True
        assert reason == ""

    def test_garbage_max_daily_trades_none(self):
        """max_daily_trades=None → безопасный дефолт (True, "")."""
        allowed, reason = allow_by_daily_count(5, None)
        assert allowed is True
        assert reason == ""

    def test_garbage_max_daily_trades_string(self):
        """max_daily_trades="abc" → безопасный дефолт (True, "")."""
        allowed, reason = allow_by_daily_count(5, "abc")
        assert allowed is True
        assert reason == ""

    def test_float_values(self):
        """Float значения корректно преобразуются в int."""
        allowed, reason = allow_by_daily_count(5.7, 10.9)
        assert allowed is True
        assert reason == ""


class TestAllowAfterSlStreak:
    """Тесты серии SL подряд и cooldown'а."""

    def test_streak_below_threshold(self):
        """Серия ниже порога → True."""
        allowed, reason = allow_after_sl_streak(2, 5, 1000.0, 1500.0, 300)
        assert allowed is True
        assert reason == ""

    def test_streak_above_threshold_cooldown_not_passed(self):
        """Серия выше порога, cooldown не прошёл → False + "sl_streak_cooldown"."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1200.0, 300)
        assert allowed is False
        assert reason == "sl_streak_cooldown"

    def test_streak_above_threshold_cooldown_passed(self):
        """Серия выше порога, cooldown прошёл → True."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1400.0, 300)
        assert allowed is True
        assert reason == ""

    def test_streak_above_threshold_cooldown_exactly_met(self):
        """Серия выше порога, elapsed == cooldown → True."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1300.0, 300)
        assert allowed is True
        assert reason == ""

    def test_max_consecutive_sl_disabled_zero(self):
        """max_consecutive_sl=0 → механика выключена → True."""
        allowed, reason = allow_after_sl_streak(100, 0, 1000.0, 1050.0, 300)
        assert allowed is True
        assert reason == ""

    def test_max_consecutive_sl_disabled_negative(self):
        """max_consecutive_sl<0 → механика выключена → True."""
        allowed, reason = allow_after_sl_streak(100, -5, 1000.0, 1050.0, 300)
        assert allowed is True
        assert reason == ""

    def test_last_sl_ts_none(self):
        """last_sl_ts=None → нет данных → True."""
        allowed, reason = allow_after_sl_streak(5, 5, None, 1500.0, 300)
        assert allowed is True
        assert reason == ""

    def test_last_sl_ts_greater_than_now(self):
        """last_sl_ts > now → аномалия → трактуем как cooldown прошёл → True."""
        allowed, reason = allow_after_sl_streak(5, 5, 2000.0, 1500.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_consecutive_sl_none(self):
        """consecutive_sl=None → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak(None, 5, 1000.0, 1200.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_consecutive_sl_string(self):
        """consecutive_sl="abc" → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak("abc", 5, 1000.0, 1200.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_max_consecutive_sl_none(self):
        """max_consecutive_sl=None → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak(5, None, 1000.0, 1200.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_max_consecutive_sl_string(self):
        """max_consecutive_sl="abc" → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak(5, "abc", 1000.0, 1200.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_last_sl_ts_string(self):
        """last_sl_ts="abc" → трактуем как cooldown прошёл → True."""
        allowed, reason = allow_after_sl_streak(5, 5, "abc", 1500.0, 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_now_string(self):
        """now="abc" → трактуем как cooldown прошёл → True."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, "abc", 300)
        assert allowed is True
        assert reason == ""

    def test_garbage_cooldown_seconds_none(self):
        """cooldown_seconds=None → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1200.0, None)
        assert allowed is True
        assert reason == ""

    def test_garbage_cooldown_seconds_string(self):
        """cooldown_seconds="abc" → безопасный дефолт (True, "")."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1200.0, "abc")
        assert allowed is True
        assert reason == ""

    def test_float_values(self):
        """Float значения корректно преобразуются в int."""
        allowed, reason = allow_after_sl_streak(5.9, 5.1, 1000.5, 1200.7, 300.9)
        assert allowed is False
        assert reason == "sl_streak_cooldown"

    def test_zero_cooldown(self):
        """cooldown_seconds=0 → cooldown всегда прошёл → True."""
        allowed, reason = allow_after_sl_streak(5, 5, 1000.0, 1000.0, 0)
        assert allowed is True
        assert reason == ""
