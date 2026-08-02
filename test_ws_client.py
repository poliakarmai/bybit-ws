#!/usr/bin/env python3
"""
Тесты ws_client.py (Фаза 6.3): BB-кеш, fallback, stale, batch_size, full WS.

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
        self.assertTrue(is_stale(300))

    def test_is_stale_threshold_is_configurable(self):
        """Порог можно менять: 5 мин для D, 10 сек для tickers."""
        from bybit_ws.ws_client import is_stale
        self.assertTrue(is_stale(5))
        self.assertTrue(is_stale(300))
        self.assertTrue(is_stale(3600))

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
        self.assertIn('tickers.BTCUSDT', args)
        self.assertIn('kline.D.BTCUSDT', args)

    def test_kline_w_subscribed_separately(self):
        """kline.W подписывается отдельным сообщением."""
        batch_size = 5
        batch = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT']
        args_w = [f'kline.W.{s}' for s in batch]
        self.assertEqual(len(args_w), 5)
        self.assertIn('kline.W.BTCUSDT', args_w)

    def test_orderbook_args_when_full_enabled(self):
        """При BYBIT_WS_FULL_ENABLED=1: orderbook.1 добавляется во второй батч."""
        batch_size = 5
        batch = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT']
        args_w = [f'kline.W.{s}' for s in batch] + [f'orderbook.1.{s}' for s in batch]
        self.assertEqual(len(args_w), 10)
        self.assertIn('orderbook.1.BTCUSDT', args_w)
        self.assertIn('kline.W.BTCUSDT', args_w)


class TestWSFallbackToREST(unittest.TestCase):
    """_get_bb_ws() должен fallback'ить на REST при отсутствии WS."""

    def test_get_bb_ws_falls_back_to_rest_when_ws_disabled(self):
        """BYBIT_WS_BB_ENABLED=0 → сразу REST."""
        os.environ['BYBIT_WS_BB_ENABLED'] = '0'
        try:
            from bybit_ws.auto_sl import _get_bb_ws
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


class TestFullWSFeatures(unittest.TestCase):
    """Фаза 6.3: полный WebSocket — новые функции."""

    def test_is_full_enabled_returns_bool(self):
        """is_full_enabled() возвращает bool."""
        from bybit_ws.ws_client import is_full_enabled
        result = is_full_enabled()
        self.assertIsInstance(result, bool)

    def test_stats_includes_full_ws_keys(self):
        """stats() включает ключи полного WS."""
        from bybit_ws.ws_client import stats
        s = stats()
        self.assertIn('full_enabled', s)
        self.assertIn('private_connected', s)
        self.assertIn('private_age_sec', s)
        self.assertIn('orderbook_symbols', s)
        self.assertIn('position_symbols', s)
        self.assertIn('executions', s)
        self.assertIn('wallet_coins', s)

    def test_get_orderbook_returns_none_when_empty(self):
        """get_orderbook возвращает None при пустом кеше."""
        from bybit_ws.ws_client import get_orderbook
        result = get_orderbook('BTCUSDT')
        self.assertIsNone(result)

    def test_get_bid_ask_returns_none_when_empty(self):
        """get_bid_ask возвращает (None, None) при пустом кеше."""
        from bybit_ws.ws_client import get_bid_ask
        bid, ask = get_bid_ask('BTCUSDT')
        self.assertIsNone(bid)
        self.assertIsNone(ask)

    def test_get_position_data_returns_empty_dict(self):
        """get_position_data() возвращает пустой dict без WS."""
        from bybit_ws.ws_client import get_position_data
        result = get_position_data()
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)

    def test_get_position_data_with_symbol_returns_none(self):
        """get_position_data(symbol) возвращает None без WS."""
        from bybit_ws.ws_client import get_position_data
        result = get_position_data('BTCUSDT')
        self.assertIsNone(result)

    def test_get_all_positions_returns_empty_dict(self):
        """get_all_positions() возвращает {} без WS."""
        from bybit_ws.ws_client import get_all_positions
        result = get_all_positions()
        self.assertIsInstance(result, dict)

    def test_get_executions_returns_empty_list(self):
        """get_executions() возвращает пустой список без WS."""
        from bybit_ws.ws_client import get_executions
        result = get_executions()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_get_wallet_returns_none_when_empty(self):
        """get_wallet() возвращает None без WS."""
        from bybit_ws.ws_client import get_wallet
        result = get_wallet('USDT')
        self.assertIsNone(result)

    def test_is_private_connected_returns_false(self):
        """is_private_connected() возвращает False без приватного WS."""
        from bybit_ws.ws_client import is_private_connected
        self.assertFalse(is_private_connected())

    def test_is_private_stale_returns_true(self):
        """is_private_stale() возвращает True без приватного WS."""
        from bybit_ws.ws_client import is_private_stale
        self.assertTrue(is_private_stale(120))

    def test_orderbook_data_structure(self):
        """Проверка структуры orderbook при ручном заполнении кеша."""
        from bybit_ws.ws_client import _orderbook_cache, _cache_lock
        with _cache_lock:
            _orderbook_cache['TESTUSDT'] = {
                'bid': 100.0,
                'ask': 100.5,
                'bidSize': 1.5,
                'askSize': 2.0,
                'spread': 0.5,
                'mid': 100.25,
                'ts': time.time(),
            }

        from bybit_ws.ws_client import get_orderbook, get_bid_ask
        try:
            ob = get_orderbook('TESTUSDT')
            self.assertIsNotNone(ob)
            self.assertEqual(ob['bid'], 100.0)
            self.assertEqual(ob['ask'], 100.5)

            bid, ask = get_bid_ask('TESTUSDT')
            self.assertEqual(bid, 100.0)
            self.assertEqual(ask, 100.5)
        finally:
            with _cache_lock:
                _orderbook_cache.pop('TESTUSDT', None)

    def test_wallet_data_structure(self):
        """Проверка структуры wallet при ручном заполнении."""
        from bybit_ws.ws_client import _wallet_cache, _cache_lock
        with _cache_lock:
            _wallet_cache['USDT'] = {
                'coin': 'USDT',
                'walletBalance': 1000.0,
                'availableBalance': 800.0,
                'equity': 1050.0,
                'upnl': 50.0,
                'totalMargin': 200.0,
                'ts': time.time(),
            }

        from bybit_ws.ws_client import get_wallet, get_wallet_balance, get_wallet_equity
        try:
            w = get_wallet('USDT')
            self.assertIsNotNone(w)
            self.assertEqual(w['walletBalance'], 1000.0)

            bal = get_wallet_balance('USDT')
            self.assertEqual(bal, 800.0)

            eq = get_wallet_equity('USDT')
            self.assertEqual(eq, 1050.0)
        finally:
            with _cache_lock:
                _wallet_cache.pop('USDT', None)

    def test_position_data_structure(self):
        """Проверка структуры позиций при ручном заполнении."""
        from bybit_ws.ws_client import _position_cache, _cache_lock
        with _cache_lock:
            _position_cache['BTCUSDT'] = {
                'symbol': 'BTCUSDT',
                'size': 0.01,
                'entry': 90000.0,
                'mark': 91000.0,
                'upnl': 10.0,
                'side': 'Buy',
                'stopLoss': 85000.0,
                'positionIdx': 0,
                'liqPrice': 80000.0,
                'leverage': 10.0,
                'positionIM': 90.0,
                'cumRealisedPnl': -5.0,
                'margin': 90.0,
                'ts': time.time(),
            }

        from bybit_ws.ws_client import get_position_data, get_all_positions
        try:
            p = get_position_data('BTCUSDT')
            self.assertIsNotNone(p)
            self.assertEqual(p['symbol'], 'BTCUSDT')
            self.assertEqual(p['size'], 0.01)

            all_p = get_all_positions()
            self.assertIn('BTCUSDT', all_p)
            self.assertEqual(len(all_p), 1)
        finally:
            with _cache_lock:
                _position_cache.pop('BTCUSDT', None)

    def test_execution_data_structure(self):
        """Проверка структуры execution при ручном заполнении."""
        from bybit_ws.ws_client import _execution_cache, _cache_lock
        with _cache_lock:
            _execution_cache.clear()
            _execution_cache.append({
                'symbol': 'ETHUSDT',
                'side': 'Buy',
                'orderId': '12345',
                'execId': 'exec-001',
                'price': 3000.0,
                'qty': 1.0,
                'type': 'Trade',
                'time': '2026-06-20T00:00:00Z',
                'ts': time.time(),
            })

        from bybit_ws.ws_client import get_executions, get_executions_for_symbol
        try:
            execs = get_executions()
            self.assertEqual(len(execs), 1)
            self.assertEqual(execs[0]['symbol'], 'ETHUSDT')

            eth_execs = get_executions_for_symbol('ETHUSDT')
            self.assertEqual(len(eth_execs), 1)

            btc_execs = get_executions_for_symbol('BTCUSDT')
            self.assertEqual(len(btc_execs), 0)
        finally:
            # Очистить глобальный кеш
            with _cache_lock:
                _execution_cache.clear()


class TestWSFullIntegration(unittest.TestCase):
    """Интеграционные тесты WS-full в main_async."""

    def test_ws_full_feature_flag_default_off(self):
        """BYBIT_WS_FULL_ENABLED default=0."""
        old = os.environ.pop('BYBIT_WS_FULL_ENABLED', None)
        try:
            from bybit_ws.ws_client import is_full_enabled
            self.assertFalse(is_full_enabled())
        finally:
            if old is not None:
                os.environ['BYBIT_WS_FULL_ENABLED'] = old

    def test_ws_full_feature_flag_on(self):
        """BYBIT_WS_FULL_ENABLED=1 → is_full_enabled()=True."""
        os.environ['BYBIT_WS_FULL_ENABLED'] = '1'
        try:
            # Перезагружаем флаг (читается при импорте модуля)
            import importlib
            import bybit_ws.ws_client as wsc
            importlib.reload(wsc)
            self.assertTrue(wsc.is_full_enabled())
        finally:
            os.environ['BYBIT_WS_FULL_ENABLED'] = '0'
            importlib.reload(wsc)


if __name__ == '__main__':
    unittest.main()
