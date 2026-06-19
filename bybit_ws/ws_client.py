#!/usr/bin/env python3
"""
WebSocket-клиент Bybit (Фаза 4 — WebSocket вместо REST polling).

Подписывается на kline-потоки для топ-50 монет, обновляет in-memory кеш
цен и BB-данных в реальном времени. Работает в отдельном потоке.

Публичный WebSocket Bybit v5:
    wss://stream.bybit.com/v5/public/linear

Подписки:
    kline.{interval}.{symbol} — свечи (D, W, M, 4h, 1h, 15m, 5m)
    tickers.{symbol} — текущая цена, 24h изменение, оборот
"""

import json
import logging
import threading
import time
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime

try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

from .alerts import log_event
from .config import Config

# ═══════════════════════════════════════════════════════════
# Глобальный кеш (потокобезопасный через threading.Lock)
# ═══════════════════════════════════════════════════════════

_cache_lock = threading.Lock()
_price_cache: Dict[str, float] = {}       # symbol → last price
_ticker_cache: Dict[str, dict] = {}       # symbol → {price, change24h, volume24h, ...}
_kline_cache: Dict[str, Dict[str, list]] = {}  # symbol → {interval: [candles]}
_bb_cache: Dict[str, Dict[str, dict]] = {}     # symbol → {interval: bb_data}
_connected = False
_last_update = 0.0
_subscribed_symbols: set = set()

# ═══════════════════════════════════════════════════════════
# TOP-50 Bybit фьючерсы (из AUTO_ENTRY_WATCH + ликвидные)
# ═══════════════════════════════════════════════════════════

WATCH_SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOGEUSDT',
    'HYPEUSDT', 'NEARUSDT', 'SUIUSDT', 'TONUSDT', 'WLDUSDT', 'LINKUSDT',
    'AAVEUSDT', 'AVAXUSDT', 'DOTUSDT', 'INJUSDT', 'ONDOUSDT', 'ARBUSDT',
    'ENAUSDT', 'FETUSDT', 'APTUSDT', 'ATOMUSDT', 'RUNEUSDT',
    'UNIUSDT', 'OPUSDT', 'ALGOUSDT', 'BCHUSDT', 'FILUSDT', 'VETUSDT',
    'SANDUSDT', 'MANAUSDT', 'GALAUSDT', 'PEPEUSDT', 'SHIBUSDT',
    'EGLDUSDT', 'ZECUSDT', 'BONKUSDT', 'FLOKIUSDT', 'WIFUSDT',
    'TIAUSDT', 'SEIUSDT', 'STRKUSDT', 'JUPUSDT', 'PYTHUSDT',
    'WUSDT', 'ENAUSDT', 'MOVEUSDT', 'STGUSDT', 'ESPORTSUSDT',
]

# BB-параметры
BB_PERIOD = 20
BB_STD = 2.0
KLINE_LIMIT = 100  # сколько свечей держать в кеше на ТФ

# Таймфреймы для kline-подписки
KLINES_TO_WATCH = ['D', 'W']  # D=дневные, W=недельные (для trailing_sl)


def _calc_sma(values: List[float], period: int) -> List[float]:
    """Простое скользящее среднее."""
    if len(values) < period:
        return []
    sma = []
    for i in range(period - 1, len(values)):
        sma.append(sum(values[i - period + 1:i + 1]) / period)
    return sma


def _calc_bb(closes: List[float]) -> Optional[dict]:
    """Bollinger Bands (SMA-20, 2σ) из списка closes."""
    if len(closes) < BB_PERIOD:
        return None

    sma = _calc_sma(closes, BB_PERIOD)
    if not sma:
        return None

    middle = sma[-1]
    # Стандартное отклонение по последним 20 свечам
    recent = closes[-BB_PERIOD:]
    mean = sum(recent) / len(recent)
    variance = sum((x - mean) ** 2 for x in recent) / len(recent)
    std = math.sqrt(variance)

    upper = middle + BB_STD * std
    lower = middle - BB_STD * std
    current = closes[-1]
    bb_range = upper - lower
    pos = ((current - lower) / bb_range * 100) if bb_range > 0 else 50

    return {
        'upper': round(upper, 8),
        'middle': round(middle, 8),
        'lower': round(lower, 8),
        'pos': round(pos, 1),
        'width': round(bb_range / middle * 100, 2) if middle > 0 else 0,
        'current': round(current, 8),
        # Aliases for REST compatibility (bb_pos, cur, bb_width)
        'bb_pos': round(pos, 1),
        'cur': round(current, 8),
        'bb_width': round(bb_range / middle * 100, 2) if middle > 0 else 0,
    }


def _on_message(ws, message: str):
    """Обработчик входящих WS-сообщений."""
    global _connected, _last_update

    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    # Пинг-понг
    if 'op' in data and data['op'] == 'pong':
        return

    # Ответ на подписку
    if 'op' in data and data['op'] == 'subscribe':
        log_event(f'🔌 WS subscribed: {data.get("args", [])}')
        return

    # Данные
    if 'topic' not in data:
        return

    topic = data['topic']
    msg_data = data.get('data', [])

    with _cache_lock:
        _connected = True
        _last_update = time.time()

        # kline свечи
        if topic.startswith('kline.'):
            parts = topic.split('.')
            if len(parts) >= 3:
                interval = parts[1]
                symbol = parts[2]

                candles = msg_data if isinstance(msg_data, list) else [msg_data]
                for candle in candles:
                    if isinstance(candle, dict) and 'start' in candle:
                        # Формат kline: {start, end, interval, open, high, low, close, volume, turnover, confirm}
                        ts = int(candle['start'])
                        o = float(candle['open'])
                        h = float(candle['high'])
                        l = float(candle['low'])
                        c_val = float(candle['close'])
                        v = float(candle.get('volume', 0))

                        if symbol not in _kline_cache:
                            _kline_cache[symbol] = {}
                        if interval not in _kline_cache[symbol]:
                            _kline_cache[symbol][interval] = []

                        klines = _kline_cache[symbol][interval]

                        # Обновить или добавить свечу
                        updated = False
                        for i, k in enumerate(klines):
                            if k[0] == ts:
                                klines[i] = [ts, o, h, l, c_val, v]
                                updated = True
                                break
                        if not updated:
                            klines.append([ts, o, h, l, c_val, v])
                            klines.sort(key=lambda x: x[0])

                        # Обрезать лимит
                        if len(klines) > KLINE_LIMIT:
                            _kline_cache[symbol][interval] = klines[-KLINE_LIMIT:]

                        # Пересчитать BB
                        closes = [k[4] for k in _kline_cache[symbol][interval]]
                        bb = _calc_bb(closes)
                        if bb:
                            if symbol not in _bb_cache:
                                _bb_cache[symbol] = {}
                            _bb_cache[symbol][interval] = bb

        # Тикер (цена)
        elif topic.startswith('tickers.'):
            symbol = topic.split('.', 1)[1] if '.' in topic else topic
            items = msg_data if isinstance(msg_data, list) else [msg_data]
            for item in items:
                if isinstance(item, dict):
                    _price_cache[symbol] = float(item.get('lastPrice', 0))
                    _ticker_cache[symbol] = {
                        'price': float(item.get('lastPrice', 0)),
                        'change24h': float(item.get('price24hPcnt', 0)) * 100,
                        'volume24h': float(item.get('volume24h', 0)),
                        'turnover24h': float(item.get('turnover24h', 0)),
                        'high24h': float(item.get('highPrice24h', 0)),
                        'low24h': float(item.get('lowPrice24h', 0)),
                    }


def _on_error(ws, error):
    log_event(f'⚠️ WS error: {error}')


def _on_close(ws, close_status_code, close_msg):
    global _connected
    with _cache_lock:
        _connected = False
    log_event(f'🔌 WS closed: {close_status_code} {close_msg}')


def _on_open(ws):
    global _connected
    log_event('🔌 WS connected to Bybit public stream')

    # Подписаться на тикеры всех отслеживаемых символов
    symbols = list(WATCH_SYMBOLS)
    # Подписка: batch_size=5 → 5 tickers + 5 kline.D = 10 args (лимит Bybit v5: макс 10 args на subscribe)
    # Разбито на 2 сообщения для kline.W (трейлинг-SL использует Weekly BB)
    batch_size = 5
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        # Batch 1: tickers + kline.D
        args = [f'tickers.{s}' for s in batch] + [f'kline.D.{s}' for s in batch]
        ws.send(json.dumps({'op': 'subscribe', 'args': args}))
        time.sleep(0.15)  # rate limit: max 10 msgs/sec
        # Batch 2: kline.W (weekly for trailing_sl)
        args_w = [f'kline.W.{s}' for s in batch]
        ws.send(json.dumps({'op': 'subscribe', 'args': args_w}))
        time.sleep(0.15)

    with _cache_lock:
        _connected = True
        _subscribed_symbols.update(symbols)


def _run_forever():
    """Основной цикл WebSocket с авто-переподключением."""
    while True:
        try:
            if websocket is None:
                log_event('⚠️ WS: websocket-client not installed, retrying in 60s')
                time.sleep(60)
                continue

            ws_url = 'wss://stream.bybit.com/v5/public/linear'
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log_event(f'⚠️ WS exception: {e}')

        log_event('🔌 WS reconnecting in 10s...')
        time.sleep(10)


# ═══════════════════════════════════════════════════════════
# Публичное API
# ═══════════════════════════════════════════════════════════

def start():
    """Запустить WebSocket-клиент в фоновом потоке."""
    if websocket is None:
        log_event('⚠️ WS: websocket-client not installed — install with: pip install websocket-client')
        return None

    t = threading.Thread(target=_run_forever, daemon=True, name='bybit-ws-client')
    t.start()
    log_event('🔌 WS client thread started')
    return t


def is_connected() -> bool:
    with _cache_lock:
        return _connected

def is_stale(max_age_sec: float = 300) -> bool:
    """True если кеш старше max_age_sec (WS отвалился, данные устарели)."""
    with _cache_lock:
        if not _connected:
            return True
        if _last_update == 0:
            return True
        return (time.time() - _last_update) > max_age_sec


def get_price(symbol: str) -> Optional[float]:
    """Текущая цена из WS-кеша."""
    with _cache_lock:
        return _price_cache.get(symbol)


def get_ticker(symbol: str) -> Optional[dict]:
    """Тикер из WS-кеша."""
    with _cache_lock:
        return _ticker_cache.get(symbol)


def get_bb(symbol: str, interval: str = 'D') -> Optional[dict]:
    """BB-данные из WS-кеша."""
    with _cache_lock:
        return _bb_cache.get(symbol, {}).get(interval)


def get_kline(symbol: str, interval: str = 'D') -> Optional[List]:
    """Кешированные свечи."""
    with _cache_lock:
        return _kline_cache.get(symbol, {}).get(interval)


def get_all_prices() -> Dict[str, float]:
    """Все цены (копия)."""
    with _cache_lock:
        return dict(_price_cache)


def stats() -> dict:
    """Статистика WS-клиента."""
    with _cache_lock:
        return {
            'connected': _connected,
            'last_update': _last_update,
            'symbols': len(_subscribed_symbols),
            'prices': len(_price_cache),
            'bbs': sum(1 for bb in _bb_cache.values() if bb),
            'age_sec': round(time.time() - _last_update, 1) if _last_update else -1,
            'cached_klines': sum(
                len(intervals) for intervals in _kline_cache.values()
            ),
        }
