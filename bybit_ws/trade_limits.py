"""Анти-overtrading guard'ы: дневной лимит сделок + пауза после серии SL.

Детерминированные чистые функции. Всё состояние передаётся аргументами —
модуль не читает файлы и не знает про окружение.
"""


def allow_by_daily_count(trades_today: int, max_daily_trades: int) -> tuple[bool, str]:
    """Разрешён ли новый вход по дневному лимиту сделок.

    Возвращает (True, "") если trades_today < max_daily_trades,
    иначе (False, "daily_limit").
    Если max_daily_trades <= 0 — лимит считается выключенным и возвращает (True, "").
    """
    try:
        # Проверяем, что оба значения можно привести к int
        if not isinstance(trades_today, (int, float)) or not isinstance(max_daily_trades, (int, float)):
            # Мусорный вход — возвращаем безопасный дефолт
            return (True, "")
        
        trades_today = int(trades_today)
        max_daily_trades = int(max_daily_trades)
        
        # Если лимит выключен
        if max_daily_trades <= 0:
            return (True, "")
        
        # Проверяем лимит
        if trades_today < max_daily_trades:
            return (True, "")
        else:
            return (False, "daily_limit")
    
    except (TypeError, ValueError):
        # Любая ошибка преобразования — безопасный дефолт
        return (True, "")


def allow_after_sl_streak(
    consecutive_sl: int,
    max_consecutive_sl: int,
    last_sl_ts,
    now,
    cooldown_seconds: int,
) -> tuple[bool, str]:
    """Разрешён ли вход с учётом серии SL подряд и cooldown'а.

    - Если max_consecutive_sl <= 0 — механика выключена, всегда (True, "").
    - Если consecutive_sl < max_consecutive_sl — серия не превышена, (True, "").
    - Если серия превышена (consecutive_sl >= max_consecutive_sl):
        * last_sl_ts is None → (True, "")  (нет данных о последнем SL)
        * now - last_sl_ts >= cooldown_seconds → (True, "")  (cooldown прошёл)
        * иначе → (False, "sl_streak_cooldown")
    """
    try:
        # Проверяем базовые параметры
        if not isinstance(consecutive_sl, (int, float)) or not isinstance(max_consecutive_sl, (int, float)):
            return (True, "")
        
        consecutive_sl = int(consecutive_sl)
        max_consecutive_sl = int(max_consecutive_sl)
        
        # Если механика выключена
        if max_consecutive_sl <= 0:
            return (True, "")
        
        # Если серия не превышена
        if consecutive_sl < max_consecutive_sl:
            return (True, "")
        
        # Серия превышена — проверяем cooldown
        # Если last_sl_ts is None — нет данных
        if last_sl_ts is None:
            return (True, "")
        
        # Проверяем, что last_sl_ts и now — числовые
        if not isinstance(last_sl_ts, (int, float)) or not isinstance(now, (int, float)):
            # Нечисловые значения — трактуем как "cooldown прошёл"
            return (True, "")
        
        # Если last_sl_ts > now — аномалия, трактуем как "cooldown прошёл"
        if last_sl_ts > now:
            return (True, "")
        
        # Проверяем cooldown
        elapsed = now - last_sl_ts
        if not isinstance(cooldown_seconds, (int, float)):
            # Мусорный cooldown — безопасный дефолт
            return (True, "")
        
        cooldown_seconds = int(cooldown_seconds)
        
        if elapsed >= cooldown_seconds:
            return (True, "")
        else:
            return (False, "sl_streak_cooldown")
    
    except (TypeError, ValueError):
        # Любая ошибка — безопасный дефолт
        return (True, "")
