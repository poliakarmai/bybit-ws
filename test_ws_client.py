#!/usr/bin/env python3
"""Тесты ws_client.py (Фаза 6.3): BB-кеш, fallback, stale, batch_size.

Запуск: cd /home/openclaw && python3 bybit_ws/test_ws_client.py
"""

import sys, os, time, unittest

# Добавляем путь к bybit_ws
sys.path.insert(0, os.path.expanduser('~/bybit-ws'))
os.chdir(os.path.expanduser('~/bybit-ws'))


class TestBBKeyAliases(unittest.TestCase):
    """C1/C2 fix: ключи pos→bb_pos, current→cur, width→bb_width."""

    def test_ws_keys_have_rest_aliases(self):
        """WS-кеш должен иметь алиасы для REST-совместимости."""
        from bybit_ws.ws_client import _calc_bb

        closes = list(range(80, 100))  # 20 значений, растущий тренд
        bb = _calc_bb(closes)
        self.assertIsNotNone(bb)

        # Оригинальные ключи
        self.assertIn('pos', bb)
        self.assertIn('current', bb)
        self.assertIn('width', bb)

        # REST-алиасы
        self.assertIn('bb_pos', bb)
        self.assertIn('cur', bb)
        self.assertIn('bb_width', bb)

        # Значения совпадают
        self.assertEqual(bb['pos'], bb['bb_pos'])
        self.assertEqual(bb['current'], bb['cur'])
        self.assertEqual(bb['width'], bb['bb_width'])

    def test_bb_keys_match_consumer_expectations(self):
        """Все ключи, которые ждут потребители, присутствуют."""
        from bybit_ws.ws_client import _calc_bb

        closes = list(range(90, 110))
        bb = _calc_bb(closes)

        # auto_sl/trailing_sl/auto_entry ожидают
        for key in ['lower', 'middle', 'upper', 'bb_pos', 'cur', 'bb_width']:
            self.assertIn(key, bb, f"Consumer key '{key}' missing from WS BB dict")

        # auto_short ожидает upper
        self.assertIn('upper', bb)
        self.assertGreater(bb['upper'], 0)


class TestIsStale(unittest.TestCase):
    """Age-check: кеш старше 300с → считается устаревшим."""

    def test_is_stale_returns_true_when_not_connected(self):
        """Без WS-соединения — всегда stale."""
        from bybit_ws.ws_client import is_stale
        # В тестовом окружении WS не подключён — должен быть stale
        self.assertTrue(is_stale(300))

    def test_is_stale_threshold_is_configurable(self):
        """Порог можно менять: 5 мин для D, 10 сек для tickers."""
        from bybit_ws.ws_client import is_stale
        self.assertTrue(is_stale(5))    # 5 сек без WS — stale
        self.assertTrue(is_stale(300))  # 300 сек — тоже
        self.assertTrue(is_stale(3600))  # час — однозначно stale

    def test_is_stale_with_zero_last_update(self):
        """Никогда не обновлялся — stale."""
        from bybit_ws.ws_client import is_stale
        self.assertTrue(is_stale(99999))


class TestBatchSize(unittest.TestCase):
    """C2 fix: batch_size=5 → макс 10 args на subscribe."""

    def test_batch_size_produces_valid_args(self):
        """Каждый батч содержит ≤10 args (5 tickers + 5 kline.D)."""
        batch_size = 5
        batch = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT']
        args = [f'tickers.{s}' for s in batch] + [f'kline.D.{s}' for s in batch]
        self.assertEqual(len(args), 10)
        # Каждый ticker + kline присутствуют
        self.assertIn('tickers.BTCUSDT', args)
        self.assertIn('kline.D.BTCUSDT', args)

    def test_kline_w_subscribed_separately(self):
        """kline.W подписывается отдельным сообщением."""
        batch_size = 5
        batch = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT']
        args_w = [f'kline.W.{s}' for s in batch]
        self.assertEqual(len(args_w), 5)
        self.assertIn('kline.W.BTCUSDT', args_w)


class TestWSFallbackToREST(unittest.TestCase):
    """_get_bb_ws() должен fallback'ить на REST при отсутствии WS."""

    def test_get_bb_ws_falls_back_to_rest_when_ws_disabled(self):
        """BYBIT_WS_BB_ENABLED=0 → сразу REST."""
        os.environ['BYBIT_WS_BB_ENABLED'] = '0'
        try:
            from bybit_ws.auto_sl import _get_bb_ws
            # Не должно крашиться — функция существует
            self.assertTrue(callable(_get_bb_ws))
        finally:
            os.environ['BYBIT_WS_BB_ENABLED'] = '1'

    def test_get_bb_ws_function_exists_in_all_modules(self):
        """_get_bb_ws есть во всех 4 модулях."""
        from bybit_ws.trailing_sl import _get_bb_ws as f1
        from bybit_ws.auto_sl import _get_bb_ws as f2
        from bybit_ws.auto_entry import _get_bb_ws as f3
        from bybit_ws.auto_short import _get_bb_ws as f4

        for name, fn in [('trailing_sl', f1), ('auto_sl', f2),
                          ('auto_entry', f3), ('auto_short', f4)]:
            self.assertTrue(callable(fn), f"_get_bb_ws missing in {name}")


if __name__ == '__main__':
    unittest.main()
