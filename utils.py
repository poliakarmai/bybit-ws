"""Общие утилиты: tier-классификация, округление, lot_step.

Фикс код-ревью Manus AI: устранение дублирования кода между модулями.
"""

from .api import bybit
from .config import Config


def get_tier_ab(cfg=None):
    """Tier A+B монеты из конфига."""
    if cfg is None:
        cfg = Config()
    try:
        return set(cfg.tiers.A) | set(cfg.tiers.B)
    except Exception:
        return set()


def get_one_way(cfg=None):
    """One-way монеты из конфига."""
    if cfg is None:
        cfg = Config()
    try:
        return set(cfg.tiers.one_way)
    except Exception:
        return set()


def get_lot_step(sym):
    """Шаг лота для символа."""
    try:
        data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
        instruments = data.get('result', {}).get('list', [])
        if instruments:
            return float(instruments[0].get('lotSizeFilter', {}).get('qtyStep', 0.1))
    except Exception:
        pass
    return 0.1


def round_to_tick(price, sym=None):
    """Округлить цену до ближайшего тика."""
    if price < 1:
        tick = 0.0001
    elif price < 10:
        tick = 0.001
    elif price < 100:
        tick = 0.01
    elif price < 1000:
        tick = 0.1
    else:
        tick = 1.0
    return round(round(price / tick) * tick, 8)


def get_precision(step):
    """Кол-во знаков после запятой для шага lot."""
    if step >= 1:
        return 0
    s = str(step)
    if '.' in s:
        return len(s.split('.')[1])
    return 0


# Константы вместо магических чисел
DEFAULT_LEVERAGE = 3
X10_LEVERAGE = 10
DEFAULT_TIMEOUT = 15  # секунд для API-вызовов
HEAVY_CYCLE_INTERVAL = 300  # 5 минут
LIGHT_CYCLE_INTERVAL = 30   # 30 секунд
