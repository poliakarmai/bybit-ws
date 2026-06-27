"""
Session-Based Parameters — адаптация стратегии под торговую сессию (28.06.2026).

NY open (14:30-17:00 UTC) — тренды, mean-reversion ломается:
  BB period ↑, SL tighter, TP wider

Asia night (21:00-02:00 UTC) — флэт, mean-reversion отлично:
  BB period ↓, SL wider, TP standard

Weekend — пониженная волатильность:
  BB period ↑, max positions меньше
"""
from datetime import datetime, timezone

# UTC times
NY_OPEN_START = 14, 30  # (hour, minute)
NY_OPEN_END = 17, 0
ASIA_NIGHT_START = 21, 0
ASIA_NIGHT_END = 2, 0   # crosses midnight


def _in_range(now: datetime, start: tuple, end: tuple) -> bool:
    """Проверить что now между start и end (UTC). Обрабатывает переход через полночь."""
    t = (now.hour, now.minute)
    if start <= end:
        return start <= t < end
    else:
        return t >= start or t < end


def get_session() -> str:
    """Определить текущую сессию.

    Returns:
        'ny_open', 'asia', 'weekend', 'normal'
    """
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return 'weekend'

    if _in_range(now, NY_OPEN_START, NY_OPEN_END):
        return 'ny_open'
    if _in_range(now, ASIA_NIGHT_START, ASIA_NIGHT_END):
        return 'asia'

    return 'normal'


# Параметры по сессиям
SESSION_PARAMS = {
    'ny_open': {
        'bb_period_shift': 5,    # +5 к BB period (более широкие полосы)
        'bb_std_shift': 0.5,     # +0.5 к std
        'tp_mult': 1.2,          # TP дальше на 20%
        'sl_mult': 0.7,          # SL ближе на 30% (тренд не прощает)
        'max_positions': 5,      # меньше позиций
        'entry_score_bonus': 10, # выше порог входа
    },
    'asia': {
        'bb_period_shift': -5,   # -5 к BB period
        'bb_std_shift': -0.3,
        'tp_mult': 1.0,
        'sl_mult': 1.3,          # SL шире (флэт даёт дышать)
        'max_positions': 10,
        'entry_score_bonus': -5, # ниже порог (больше сигналов)
    },
    'weekend': {
        'bb_period_shift': 10,
        'bb_std_shift': 0.5,
        'tp_mult': 0.8,          # TP ближе
        'sl_mult': 0.8,
        'max_positions': 3,      # мало позиций
        'entry_score_bonus': 15, # высокий порог
    },
    'normal': {
        'bb_period_shift': 0,
        'bb_std_shift': 0.0,
        'tp_mult': 1.0,
        'sl_mult': 1.0,
        'max_positions': 8,
        'entry_score_bonus': 0,
    },
}
