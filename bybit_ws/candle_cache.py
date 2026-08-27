"""
TTL-кэш для kline-свечей Bybit — Фаза SHORT-оптимизация (таймаут сканера).

Проблема: SHORT-сканер перебирает ~80 символов, на каждый делает 3 REST-фетча
свечей (D/W/M) и не укладывается в 20 секунд. Кэш сокращает повторные запросы
к Bybit API.

Архитектура:
- Кэш самодостаточный: fetcher передаётся аргументом (dependency injection).
  Модуль НЕ импортирует bybit — тесты идут без сети.
- Глобальный in-memory dict с ключом (symbol, interval) → {"ts": float, "data": list}.
- Один threading.Lock на весь кэш — функции вызываются из потоков (сканер,
  MCP-сервер, фоновые воркеры).
- TTL: запись живёт ttl секунд с момента сохранения. По истечении — перезапрос.

Использование:
    from bybit_ws.candle_cache import get_candles, invalidate, cache_stats
    from bybit_ws.mtf_confirmation import _fetch_candles

    # С dependency injection
    data = get_candles('BTCUSDT', 'D', fetcher=_fetch_candles, ttl=300.0)
"""

import threading
import time
from typing import Callable, Optional

# Глобальное состояние модуля
_cache: dict = {}
_lock = threading.Lock()
_hits = 0
_misses = 0


def _inc_hit() -> None:
    """Инкремент счётчика попаданий (вызывается под _lock)."""
    global _hits
    _hits += 1


def _inc_miss() -> None:
    """Инкремент счётчика промахов (вызывается под _lock)."""
    global _misses
    _misses += 1


def get_candles(
    symbol: str,
    interval: str,
    fetcher: Callable[[str, str], Optional[list]],
    ttl: float = 300.0,
) -> Optional[list]:
    """Получить свечи для (symbol, interval) с TTL-кэшированием.

    Args:
        symbol: торговая пара (например, 'BTCUSDT').
        interval: таймфрейм ('D', 'W', 'M' и т.п.).
        fetcher: callable(symbol, interval) -> Optional[list]. Тип не проверяем.
        ttl: время жизни записи в секундах (по умолчанию 300 = 5 минут).

    Returns:
        list свечей или None, если fetcher вернул None / кэш пуст и fetcher
        не смог получить данные.

    Поведение:
        - Кэш свежий (time.time() - ts < ttl) → возврат data, инкремент hits.
        - Кэш пуст / протух → вызов fetcher(symbol, interval).
            * fetcher вернул None → НЕ кэшировать, вернуть None, инкремент miss.
            * fetcher вернул list → закэшировать с ts=time.time(), вернуть,
              инкремент miss.
    """
    global _hits, _misses
    key = (symbol, interval)
    now = time.time()

    with _lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry["ts"]) < ttl:
            _hits += 1
            return entry["data"]

        # Кэш пуст или протух — зовём fetcher под локом, чтобы не было
        # гонки двух потоков с одним ключом (оба получат данные, но
        # последний write выиграет — безвредно).
        data = fetcher(symbol, interval)
        _misses += 1

        if data is None:
            # Не кэшируем None — если запрос упал, пробуем снова при следующем вызове
            return None

        _cache[key] = {"ts": now, "data": data}
        return data


def invalidate(
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
) -> None:
    """Инвалидировать записи кэша.

    Args:
        symbol: None → не фильтровать по символу; str → только записи с этим символом.
        interval: None → не фильтровать по ТФ; str → только записи с этим ТФ.

    Матрица:
        - symbol=None, interval=None → очистить ВСЁ.
        - symbol=str, interval=None → удалить все interval для данного symbol.
        - symbol=None, interval=str → удалить все symbol для данного interval.
        - symbol=str, interval=str → удалить конкретный ключ.

    Счётчики hits/misses НЕ сбрасываются (это метрики с начала сессии).
    """
    with _lock:
        if symbol is None and interval is None:
            _cache.clear()
            return

        if symbol is not None and interval is not None:
            _cache.pop((symbol, interval), None)
            return

        # Один из аргументов задан — фильтруем
        keys_to_delete = []
        for key in _cache.keys():
            sym, itv = key
            if symbol is not None and sym != symbol:
                continue
            if interval is not None and itv != interval:
                continue
            keys_to_delete.append(key)

        for key in keys_to_delete:
            _cache.pop(key, None)


def cache_stats() -> dict:
    """Снимок метрик кэша (thread-safe).

    Returns:
        dict с полями:
            - size: int — текущее число записей в кэше.
            - hits: int — суммарное число попаданий с начала процесса.
            - misses: int — суммарное число промахов с начала процесса.
    """
    with _lock:
        return {
            "size": len(_cache),
            "hits": _hits,
            "misses": _misses,
        }
