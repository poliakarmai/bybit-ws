"""Тесты для candle_cache.py — TTL-кэш kline-свечей.

Проверяет:
- get_candles кэширует и не вызывает fetcher повторно
- TTL-экспирация (ttl=0 → fetcher вызывается каждый раз)
- fetcher вернул None → не кэшируется, следующий вызов снова зовёт fetcher
- invalidate(symbol, interval) → следующий вызов зовёт fetcher
- invalidate() без аргументов → всё очищено
- cache_stats() — hits/misses/size корректны
"""

import pytest

from bybit_ws import candle_cache


class FakeFetcher:
    """Поддельный fetcher со счётчиком вызовов и контролируемым возвратом."""

    def __init__(self, return_value=None, side_effect=None):
        """Если задан side_effect — это список возвратов (по одному на вызов).
        Иначе return_value возвращается при каждом вызове.
        """
        self.call_count = 0
        self.call_args = []
        self._return_value = return_value
        self._side_effect = side_effect
        # Отдельные счётчики для (symbol, interval)
        self.calls_by_key = {}

    def __call__(self, symbol: str, interval: str):
        self.call_count += 1
        self.call_args.append((symbol, interval))
        key = (symbol, interval)
        self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
        if self._side_effect is not None:
            idx = self.call_count - 1
            if idx < len(self._side_effect):
                return self._side_effect[idx]
            return self._side_effect[-1]
        return self._return_value


@pytest.fixture(autouse=True)
def _reset_cache():
    """Перед каждым тестом сбрасываем глобальный кэш и счётчики.
    autouse=True — фикстура применяется ко всем тестам без явного запроса.
    """
    with candle_cache._lock:
        candle_cache._cache.clear()
        candle_cache._hits = 0
        candle_cache._misses = 0
    yield


class TestGetCandlesCacheHit:
    """Кэш срабатывает на повторных вызовах."""

    def test_same_key_calls_fetcher_once(self):
        """Два вызова get_candles с одним ключом → fetcher вызван 1 раз."""
        fake = FakeFetcher(return_value=[{"close": "100"}])
        r1 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        r2 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)

        assert r1 == [{"close": "100"}]
        assert r2 == [{"close": "100"}]
        assert fake.call_count == 1, f"Ожидался 1 вызов fetcher, было {fake.call_count}"
        assert fake.calls_by_key[("BTCUSDT", "D")] == 1

    def test_different_keys_call_fetcher_separately(self):
        """Разные (symbol, interval) → fetcher вызывается для каждого."""
        fake = FakeFetcher(return_value=[{"close": "100"}])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("BTCUSDT", "W", fetcher=fake, ttl=300.0)

        assert fake.call_count == 3
        assert fake.calls_by_key[("BTCUSDT", "D")] == 1
        assert fake.calls_by_key[("ETHUSDT", "D")] == 1
        assert fake.calls_by_key[("BTCUSDT", "W")] == 1

    def test_three_repeated_calls_hit_twice(self):
        """Три вызова подряд → fetcher 1 раз, hits=2, misses=1."""
        fake = FakeFetcher(return_value=[1, 2, 3])
        for _ in range(3):
            candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)

        assert fake.call_count == 1
        stats = candle_cache.cache_stats()
        assert stats["size"] == 1
        assert stats["hits"] == 2
        assert stats["misses"] == 1


class TestTTLExpiration:
    """TTL-экспирация."""

    def test_ttl_zero_calls_fetcher_every_time(self):
        """ttl=0 → запись протухает мгновенно → fetcher вызывается каждый раз."""
        fake = FakeFetcher(return_value=[{"close": "1"}])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=0)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=0)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=0)

        assert fake.call_count == 3, f"Ожидалось 3 вызова fetcher, было {fake.call_count}"


class TestFetcherReturnsNone:
    """fetcher вернул None — не кэшируется."""

    def test_none_is_not_cached(self):
        """fetcher → None: не кэшируется, следующий вызов зовёт fetcher снова."""
        fake = FakeFetcher(return_value=None)
        r1 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        r2 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)

        assert r1 is None
        assert r2 is None
        assert fake.call_count == 2, f"Ожидалось 2 вызова fetcher, было {fake.call_count}"
        stats = candle_cache.cache_stats()
        assert stats["size"] == 0, "None не должен попадать в кэш"
        assert stats["misses"] == 2

    def test_none_then_value_caches_value(self):
        """Сначала None, потом list — list кэшируется."""
        fake = FakeFetcher(side_effect=[None, [{"close": "100"}]])
        r1 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        r2 = candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)

        assert r1 is None
        assert r2 == [{"close": "100"}]
        assert fake.call_count == 2


class TestInvalidate:
    """Инвалидация кэша."""

    def test_invalidate_specific_key(self):
        """invalidate(symbol, interval) → следующий вызов зовёт fetcher заново."""
        fake = FakeFetcher(return_value=[1, 2, 3])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        assert fake.call_count == 1

        candle_cache.invalidate(symbol="BTCUSDT", interval="D")

        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        assert fake.call_count == 2, f"После invalidate ожидался ещё 1 вызов, итого 2, было {fake.call_count}"

    def test_invalidate_all_clears_everything(self):
        """invalidate() без аргументов → всё очищено."""
        fake = FakeFetcher(return_value=[1])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("ETHUSDT", "W", fetcher=fake, ttl=300.0)
        assert candle_cache.cache_stats()["size"] == 2

        candle_cache.invalidate()

        assert candle_cache.cache_stats()["size"] == 0
        # Следующие вызовы снова зовут fetcher
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        assert fake.call_count == 3

    def test_invalidate_by_symbol_only(self):
        """invalidate(symbol) без interval → удаляет все interval для этого symbol,
        но НЕ трогает другие символы.
        """
        fake = FakeFetcher(return_value=[1])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("BTCUSDT", "W", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)
        assert candle_cache.cache_stats()["size"] == 3

        candle_cache.invalidate(symbol="BTCUSDT")

        stats = candle_cache.cache_stats()
        assert stats["size"] == 1
        # ETHUSDT остался
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)
        # Попадание в кэш — fetcher не дёргается
        assert fake.call_count == 3

    def test_invalidate_by_interval_only(self):
        """invalidate(interval) без symbol → удаляет все symbol для этого interval,
        но НЕ трогает другие ТФ.
        """
        fake = FakeFetcher(return_value=[1])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("BTCUSDT", "W", fetcher=fake, ttl=300.0)
        assert candle_cache.cache_stats()["size"] == 3

        candle_cache.invalidate(interval="D")

        stats = candle_cache.cache_stats()
        assert stats["size"] == 1
        # BTCUSDT/W остался
        candle_cache.get_candles("BTCUSDT", "W", fetcher=fake, ttl=300.0)
        assert fake.call_count == 3

    def test_invalidate_nonexistent_key_is_noop(self):
        """invalidate несуществующего ключа — без ошибок."""
        candle_cache.invalidate(symbol="NOTHING", interval="D")
        candle_cache.invalidate(symbol="NOTHING")
        candle_cache.invalidate(interval="X")


class TestCacheStats:
    """Метрики cache_stats()."""

    def test_initial_stats_are_zero(self):
        """Перед любыми вызовами — нули."""
        stats = candle_cache.cache_stats()
        assert stats == {"size": 0, "hits": 0, "misses": 0}

    def test_stats_track_hits_and_misses(self):
        """hits/misses/size корректны после серии вызовов."""
        fake = FakeFetcher(return_value=[1])
        # 1 miss (запись)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        # 1 hit
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        # 1 miss (новый ключ)
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)
        # 1 hit
        candle_cache.get_candles("ETHUSDT", "D", fetcher=fake, ttl=300.0)

        stats = candle_cache.cache_stats()
        assert stats["size"] == 2
        assert stats["hits"] == 2
        assert stats["misses"] == 2

    def test_none_calls_increment_miss_not_hit(self):
        """fetcher вернул None → miss, не hit; size не растёт."""
        fake = FakeFetcher(return_value=None)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)

        stats = candle_cache.cache_stats()
        assert stats["size"] == 0
        assert stats["hits"] == 0
        assert stats["misses"] == 2

    def test_invalidate_preserves_counters(self):
        """invalidate очищает size, но НЕ сбрасывает hits/misses."""
        fake = FakeFetcher(return_value=[1])
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
        # 1 miss, 1 hit
        assert candle_cache.cache_stats()["hits"] == 1
        assert candle_cache.cache_stats()["misses"] == 1

        candle_cache.invalidate()

        stats = candle_cache.cache_stats()
        assert stats["size"] == 0
        assert stats["hits"] == 1, "invalidate не должен сбрасывать hits"
        assert stats["misses"] == 1, "invalidate не должен сбрасывать misses"


class TestThreadSafety:
    """Базовая проверка thread-safety под нагрузкой."""

    def test_concurrent_access_does_not_corrupt(self):
        """100 потоков дёргают один ключ — fetcher вызывается ≥1, кэш консистентен."""
        import threading

        fake = FakeFetcher(return_value=[1, 2, 3])
        errors = []

        def worker():
            try:
                candle_cache.get_candles("BTCUSDT", "D", fetcher=fake, ttl=300.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Ошибки в потоках: {errors}"
        # fetcher мог быть вызван несколько раз из-за гонки (это ок —
        # не mutex на fetcher), но результат кэша — list, не None
        stats = candle_cache.cache_stats()
        assert stats["size"] == 1
        assert stats["hits"] + stats["misses"] == 100
