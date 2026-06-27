"""
BB Pre-Fetcher — batch-загрузка BB для всех символов (28.06.2026).

Заменяет N последовательных get_bb_data() на один вызов.
Кеширует результат на 5 минут — auto_entry/auto_short используют кеш.
"""
import time
from .api import bybit

# Кеш: symbol → {upper, middle, lower, ts}
_bb_batch_cache: dict[str, dict] = {}
_BB_BATCH_TTL = 300  # 5 минут


def prefetch_bb_for_all(symbols: list[str], interval: str = 'D') -> int:
    """Загрузить BB для списка символов. Возвращает количество успешно загруженных."""
    global _bb_batch_cache
    now = time.time()
    loaded = 0

    for sym in symbols:
        if sym in _bb_batch_cache and now - _bb_batch_cache[sym].get('ts', 0) < _BB_BATCH_TTL:
            loaded += 1
            continue

        try:
            kline = bybit('GET',
                f'/v5/market/kline?category=linear&symbol={sym}&interval={interval}&limit=25')
            if not kline or kline.get('retCode') != 0:
                continue

            closes = [float(c[4]) for c in reversed(kline['result']['list'][:20])]
            if len(closes) < 5:
                continue

            sma = sum(closes) / len(closes)
            variance = sum((c - sma) ** 2 for c in closes) / len(closes)
            std = variance ** 0.5

            _bb_batch_cache[sym] = {
                'upper': round(sma + 2.0 * std, 8),
                'middle': round(sma, 8),
                'lower': round(sma - 2.0 * std, 8),
                'ts': now,
            }
            loaded += 1
        except Exception:
            continue

    return loaded


def get_cached_bb(symbol: str) -> dict | None:
    """Получить BB из кеша. None если нет или просрочен."""
    now = time.time()
    if symbol in _bb_batch_cache:
        entry = _bb_batch_cache[symbol]
        if now - entry.get('ts', 0) < _BB_BATCH_TTL:
            return {'upper': entry['upper'], 'middle': entry['middle'], 'lower': entry['lower']}
    return None


def get_cached_bb_lower(symbol: str) -> float:
    """Быстрый доступ к lower BB."""
    bb = get_cached_bb(symbol)
    return bb['lower'] if bb else 0.0


def get_cached_bb_upper(symbol: str) -> float:
    """Быстрый доступ к upper BB."""
    bb = get_cached_bb(symbol)
    return bb['upper'] if bb else 0.0
