#!/usr/bin/env python3
"""
WebSocket-клиент Bybit (Фаза 6.3 — полный WebSocket: orderbook, position, execution, wallet).

Подписывается на:
  Публичные потоки (wss://stream.bybit.com/v5/public/linear):
    kline.{interval}.{symbol} — свечи (D, W, 4h, 15m)
    tickers.{symbol}         — текущая цена, 24h изменение
    orderbook.{depth}.{symbol} — стакан (depth 1: лучшие bid/ask)

  Приватные потоки (wss://stream.bybit.com/v5/private) — BYBIT_WS_FULL_ENABLED=1:
    position  — real-time обновление позиций (PnL, маржа, ликвидация)
    execution — уведомления о fill ордеров
    wallet    — баланс кошелька

Feature flags:
    BYBIT_WS_FULL_ENABLED=0 (default) — только публичные потоки (kline-кеш)
    BYBIT_WS_FULL_ENABLED=1          — полный WS: +orderbook, position, execution, wallet

Архитектура:
    - Публичный WS: отдельный поток (как раньше)
    - Приватный WS: отдельный поток (только при BYBIT_WS_FULL_ENABLED=1)
    - Кеши потокобезопасны (threading.Lock)
"""

import hashlib
import hmac
import json
import os
import threading
import time
import math
from typing import Dict, List, Optional, Tuple

try:
    import websocket  # pip install websocket-client
except ImportError:
    websocket = None

from .alerts import log_event

# ═══════════════════════════════════════════════════════════
# Feature flags
# ═══════════════════════════════════════════════════════════

_WS_FULL_ENABLED = os.environ.get('BYBIT_WS_FULL_ENABLED', '0') == '1'

# ═══════════════════════════════════════════════════════════
# Глобальный кеш (потокобезопасный через threading.Lock)
# ═══════════════════════════════════════════════════════════

_cache_lock = threading.Lock()
_price_cache: Dict[str, float] = {}          # symbol → last price
_ticker_cache: Dict[str, dict] = {}          # symbol → {price, change24h, volume24h, ...}
_kline_cache: Dict[str, Dict[str, list]] = {}  # symbol → {interval: [candles]}
_bb_cache: Dict[str, Dict[str, dict]] = {}     # symbol → {interval: bb_data}

# Новые кеши (Фаза 6.3 — полный WebSocket)
_orderbook_cache: Dict[str, dict] = {}       # symbol → {bid, ask, bidSize, askSize, ts}
_position_cache: Dict[str, dict] = {}        # symbol → {entry, mark, upnl, size, side, liq, leverage, ...}
_execution_cache: List[dict] = []             # список последних fill-уведомлений (до 100)
_wallet_cache: dict = {}                      # {coin: {walletBalance, availableBalance, ...}}

_connected = False
_private_connected = False
_last_update = 0.0
_last_private_update = 0.0
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
KLINE_LIMIT = 100

# Таймфреймы для kline-подписки
KLINES_TO_WATCH = ['D', 'W', '4h', '15m']


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
        # Алиасы для REST-совместимости (_score_candidate)
        'bb_pos': round(pos, 1),
        'bb_pct': round(pos, 1),
        'bb_upper': round(upper, 8),
        'bb_lower': round(lower, 8),
        'bb_sma': round(middle, 8),
        'cur': round(current, 8),
        'bb_width': round(bb_range / middle * 100, 2) if middle > 0 else 0,
    }


# ═══════════════════════════════════════════════════════════
# HMAC-аутентификация для приватного WebSocket
# ═══════════════════════════════════════════════════════════

def _load_api_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Загрузить API ключи (как в api.py)."""
    api_key = os.environ.get('BYBIT_API_KEY')
    api_secret = os.environ.get('BYBIT_API_SECRET')
    if api_key and api_secret:
        return api_key, api_secret

    legacy = os.path.expanduser('~/.config/bybit-cli/config')
    try:
        with open(legacy) as f:
            for line in f:
                line = line.strip()
                if line.startswith('BYBIT_API_KEY='):
                    api_key = line.split('=', 1)[1].strip()
                elif line.startswith('BYBIT_API_SECRET='):
                    api_secret = line.split('=', 1)[1].strip()
    except Exception:
        pass
    return api_key, api_secret


def _ws_auth_message() -> Optional[dict]:
    """Создать auth-сообщение для приватного WebSocket Bybit v5."""
    api_key, api_secret = _load_api_credentials()
    if not api_key or not api_secret:
        log_event('⚠️ WS private: no API credentials')
        return None

    expires = int((time.time() + 5) * 1000)  # +5 сек запас
    sign_str = f"GET/realtime{expires}"
    signature = hmac.new(
        api_secret.encode(),
        sign_str.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        'op': 'auth',
        'args': [api_key, expires, signature]
    }


# ═══════════════════════════════════════════════════════════
# Публичный WebSocket — обработчики
# ═══════════════════════════════════════════════════════════

def _on_message_public(ws, message: str):
    """Обработчик публичных WS-сообщений (kline, tickers, orderbook)."""
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
        log_event(f'🔌 WS public subscribed: {data.get("args", [])}')
        return

    # Данные
    if 'topic' not in data:
        return

    topic = data['topic']
    msg_data = data.get('data', [])

    with _cache_lock:
        _connected = True
        _last_update = time.time()

        # ── kline свечи ──
        if topic.startswith('kline.'):
            parts = topic.split('.')
            if len(parts) >= 3:
                interval = parts[1]
                symbol = parts[2]

                candles = msg_data if isinstance(msg_data, list) else [msg_data]
                for candle in candles:
                    if isinstance(candle, dict) and 'start' in candle:
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

                        updated = False
                        for i, k in enumerate(klines):
                            if k[0] == ts:
                                klines[i] = [ts, o, h, l, c_val, v]
                                updated = True
                                break
                        if not updated:
                            klines.append([ts, o, h, l, c_val, v])
                            klines.sort(key=lambda x: x[0])

                        if len(klines) > KLINE_LIMIT:
                            _kline_cache[symbol][interval] = klines[-KLINE_LIMIT:]

                        # Пересчитать BB
                        closes = [k[4] for k in _kline_cache[symbol][interval]]
                        bb = _calc_bb(closes)
                        if bb:
                            if symbol not in _bb_cache:
                                _bb_cache[symbol] = {}
                            _bb_cache[symbol][interval] = bb

        # ── Тикер (цена) ──
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

        # ── Orderbook (depth 1: лучшие bid/ask) ──
        elif topic.startswith('orderbook.'):
            # Формат топика: orderbook.{depth}.{symbol}
            parts = topic.split('.')
            if len(parts) >= 3:
                symbol = parts[2]
                ob_data = msg_data if isinstance(msg_data, dict) else (msg_data[0] if isinstance(msg_data, list) and msg_data else {})

                if isinstance(ob_data, dict):
                    bids = ob_data.get('b', ob_data.get('bids', []))
                    asks = ob_data.get('a', ob_data.get('asks', []))

                    bid_price = float(bids[0][0]) if bids and len(bids) > 0 else 0
                    bid_size = float(bids[0][1]) if bids and len(bids[0]) > 1 else 0
                    ask_price = float(asks[0][0]) if asks and len(asks) > 0 else 0
                    ask_size = float(asks[0][1]) if asks and len(asks[0]) > 1 else 0

                    _orderbook_cache[symbol] = {
                        'bid': bid_price,
                        'ask': ask_price,
                        'bidSize': bid_size,
                        'askSize': ask_size,
                        'spread': round(ask_price - bid_price, 8) if bid_price > 0 and ask_price > 0 else 0,
                        'mid': round((bid_price + ask_price) / 2, 8) if bid_price > 0 and ask_price > 0 else 0,
                        'ts': time.time(),
                    }


def _on_error_public(ws, error):
    log_event(f'⚠️ WS public error: {error}')


def _on_close_public(ws, close_status_code, close_msg):
    global _connected
    with _cache_lock:
        _connected = False
    log_event(f'🔌 WS public closed: {close_status_code} {close_msg}')


def _on_open_public(ws):
    global _connected
    log_event('🔌 WS public connected to Bybit' if not _WS_FULL_ENABLED
              else '🔌 WS public connected to Bybit (+orderbook)')

    symbols = list(WATCH_SYMBOLS)
    batch_size = 5

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]

        # Batch 1: tickers + kline.D
        args = [f'tickers.{s}' for s in batch] + [f'kline.D.{s}' for s in batch]
        ws.send(json.dumps({'op': 'subscribe', 'args': args}))
        time.sleep(0.15)

        # Batch 2: kline.W + orderbook.1 (если FULL)
        args_w = [f'kline.W.{s}' for s in batch]
        if _WS_FULL_ENABLED:
            args_w += [f'orderbook.1.{s}' for s in batch]
        ws.send(json.dumps({'op': 'subscribe', 'args': args_w}))
        time.sleep(0.15)

    with _cache_lock:
        _connected = True
        _subscribed_symbols.update(symbols)


# ═══════════════════════════════════════════════════════════
# Приватный WebSocket — обработчики (BYBIT_WS_FULL_ENABLED=1)
# ═══════════════════════════════════════════════════════════

def _on_message_private(ws, message: str):
    """Обработчик приватных WS-сообщений (position, execution, wallet)."""
    global _private_connected, _last_private_update

    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    # Пинг-понг
    if 'op' in data and data['op'] == 'pong':
        return

    # Ответ на аутентификацию
    if 'op' in data and data['op'] == 'auth':
        if data.get('success'):
            log_event('🔐 WS private authenticated')
            _private_connected = True
        else:
            log_event(f'⚠️ WS private auth failed: {data.get("ret_msg", "unknown")}')
        return

    # Ответ на подписку
    if 'op' in data and data['op'] == 'subscribe':
        log_event(f'🔌 WS private subscribed: {data.get("args", [])}')
        return

    # Данные
    if 'topic' not in data:
        return

    topic = data['topic']
    msg_data = data.get('data', {})

    with _cache_lock:
        _private_connected = True
        _last_private_update = time.time()

        # ── Позиции ──
        if topic == 'position':
            # Bybit v5 private: position topic присылает список позиций
            positions = msg_data if isinstance(msg_data, list) else [msg_data]
            for p in positions:
                if not isinstance(p, dict):
                    continue
                sym = p.get('symbol', '')
                size = float(p.get('size', 0))
                if size > 0:
                    stop = p.get('stopLoss')
                    liq = p.get('liqPrice', '')
                    _position_cache[sym] = {
                        'symbol': sym,
                        'size': size,
                        'entry': float(p.get('avgPrice', p.get('entryPrice', 0))),
                        'mark': float(p.get('markPrice', 0)),
                        'upnl': float(p.get('unrealisedPnl', 0)),
                        'side': p.get('side', ''),
                        'stopLoss': float(stop) if stop else None,
                        'positionIdx': int(p.get('positionIdx', 0)),
                        'liqPrice': float(liq) if liq and liq != '' else None,
                        'leverage': float(p.get('leverage', '1')),
                        'positionIM': float(p.get('positionIM', 0)),
                        'cumRealisedPnl': float(p.get('cumRealisedPnl', 0)),
                        'margin': float(p.get('positionIM', p.get('margin', 0))),
                        'ts': time.time(),
                    }
                else:
                    # Позиция закрыта — удалить из кеша
                    _position_cache.pop(sym, None)

        # ── Исполнения (fill ордеров) ──
        elif topic == 'execution':
            executions = msg_data if isinstance(msg_data, list) else [msg_data]
            for ex in executions:
                if isinstance(ex, dict):
                    entry = {
                        'symbol': ex.get('symbol', ''),
                        'side': ex.get('side', ''),
                        'orderId': ex.get('orderId', ''),
                        'execId': ex.get('execId', ''),
                        'price': float(ex.get('execPrice', ex.get('price', 0))),
                        'qty': float(ex.get('execQty', ex.get('qty', 0))),
                        'type': ex.get('execType', ex.get('orderType', '')),
                        'time': ex.get('execTime', ex.get('updatedTime', '')),
                        'ts': time.time(),
                    }
                    _execution_cache.append(entry)
                    # Держим не более 100 последних
                    if len(_execution_cache) > 100:
                        _execution_cache[:] = _execution_cache[-100:]

                    log_event(f'📊 WS execution: {entry["symbol"]} {entry["side"]} '
                              f'{entry["qty"]} @ {entry["price"]} [{entry["type"]}]')

        # ── Кошелёк ──
        elif topic == 'wallet':
            wallets = msg_data if isinstance(msg_data, list) else [msg_data]
            for w in wallets:
                if isinstance(w, dict):
                    coin = w.get('coin', 'USDT')
                    _wallet_cache[coin] = {
                        'coin': coin,
                        'walletBalance': float(w.get('walletBalance', 0)),
                        'availableBalance': float(w.get('availableToWithdraw',
                                           w.get('availableBalance', 0))),
                        'equity': float(w.get('equity',
                                    w.get('totalWalletBalance', 0))),
                        'upnl': float(w.get('unrealisedPnl', 0)),
                        'totalMargin': float(w.get('totalMarginBalance',
                                         w.get('totalPerpUPL', 0))),
                        'ts': time.time(),
                    }


def _on_error_private(ws, error):
    log_event(f'⚠️ WS private error: {error}')


def _on_close_private(ws, close_status_code, close_msg):
    global _private_connected
    with _cache_lock:
        _private_connected = False
    log_event(f'🔌 WS private closed: {close_status_code} {close_msg}')


def _on_open_private(ws):
    """При открытии приватного WS: аутентификация → подписка на приватные топики."""
    log_event('🔌 WS private connected — authenticating...')

    # Аутентификация
    auth_msg = _ws_auth_message()
    if auth_msg:
        ws.send(json.dumps(auth_msg))
        time.sleep(0.5)  # Подождать auth-ответ

        # Подписка на приватные топики: position, execution, wallet
        private_args = ['position', 'execution', 'wallet']
        ws.send(json.dumps({'op': 'subscribe', 'args': private_args}))
        log_event(f'🔌 WS private subscribed: {private_args}')
    else:
        log_event('⚠️ WS private: auth message failed — closing')
        ws.close()


# ═══════════════════════════════════════════════════════════
# Фоновые потоки
# ═══════════════════════════════════════════════════════════

def _run_public_forever():
    """Публичный WebSocket — авто-переподключение."""
    while True:
        try:
            if websocket is None:
                log_event('⚠️ WS public: websocket-client not installed, retrying in 60s')
                time.sleep(60)
                continue

            ws_url = 'wss://stream.bybit.com/v5/public/linear'
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=_on_open_public,
                on_message=_on_message_public,
                on_error=_on_error_public,
                on_close=_on_close_public,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log_event(f'⚠️ WS public exception: {e}')

        log_event('🔌 WS public reconnecting in 10s...')
        time.sleep(10)


def _run_private_forever():
    """Приватный WebSocket — авто-переподключение."""
    while True:
        try:
            if websocket is None:
                log_event('⚠️ WS private: websocket-client not installed, retrying in 60s')
                time.sleep(60)
                continue

            ws_url = 'wss://stream.bybit.com/v5/private'
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=_on_open_private,
                on_message=_on_message_private,
                on_error=_on_error_private,
                on_close=_on_close_private,
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log_event(f'⚠️ WS private exception: {e}')

        log_event('🔌 WS private reconnecting in 10s...')
        time.sleep(10)


# ═══════════════════════════════════════════════════════════
# Публичное API
# ═══════════════════════════════════════════════════════════

def start():
    """Запустить WebSocket-клиенты в фоновых потоках."""
    if websocket is None:
        log_event('⚠️ WS: websocket-client not installed — install with: pip install websocket-client')
        return None

    threads = []

    # Публичный WS (всегда)
    t_pub = threading.Thread(target=_run_public_forever, daemon=True, name='bybit-ws-public')
    t_pub.start()
    threads.append(t_pub)
    log_event('🔌 WS public client thread started')

    # Приватный WS (только при BYBIT_WS_FULL_ENABLED=1)
    if _WS_FULL_ENABLED:
        t_priv = threading.Thread(target=_run_private_forever, daemon=True, name='bybit-ws-private')
        t_priv.start()
        threads.append(t_priv)
        log_event('🔐 WS private client thread started (FULL mode)')

    return threads[0] if len(threads) == 1 else threads


def is_full_enabled() -> bool:
    """Проверить feature flag."""
    return _WS_FULL_ENABLED


# ─── Состояние ───

def is_connected() -> bool:
    with _cache_lock:
        return _connected


def is_private_connected() -> bool:
    with _cache_lock:
        return _private_connected


def is_stale(max_age_sec: float = 300) -> bool:
    """True если публичный кеш старше max_age_sec."""
    with _cache_lock:
        if not _connected:
            return True
        if _last_update == 0:
            return True
        return (time.time() - _last_update) > max_age_sec


def is_private_stale(max_age_sec: float = 120) -> bool:
    """True если приватный кеш старше max_age_sec."""
    with _cache_lock:
        if not _private_connected:
            return True
        if _last_private_update == 0:
            return True
        return (time.time() - _last_private_update) > max_age_sec


# ─── Цены / тикеры ───

def get_price(symbol: str) -> Optional[float]:
    """Текущая цена из WS-кеша (tickers)."""
    with _cache_lock:
        return _price_cache.get(symbol)


def get_ticker(symbol: str) -> Optional[dict]:
    """Тикер из WS-кеша."""
    with _cache_lock:
        return _ticker_cache.get(symbol)


# ─── BB / свечи ───

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


# ─── Orderbook (Фаза 6.3) ───

def get_orderbook(symbol: str) -> Optional[dict]:
    """Лучшие bid/ask из WS-кеша (depth 1)."""
    with _cache_lock:
        return _orderbook_cache.get(symbol)


def get_bid_ask(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """Возвращает (bid, ask) для символа."""
    ob = get_orderbook(symbol)
    if ob:
        return ob.get('bid'), ob.get('ask')
    return None, None


# ─── Позиции (Фаза 6.3 — real-time из WS вместо REST) ───

def get_position_data(symbol: Optional[str] = None) -> Optional[dict]:
    """
    Позиции из приватного WS-кеша.
    - symbol=None → все позиции {symbol: data}
    - symbol=STR   → одна позиция или None
    """
    with _cache_lock:
        if symbol:
            return _position_cache.get(symbol)
        return dict(_position_cache)


def get_all_positions() -> dict:
    """Все позиции из WS-кеша (аналог fetch_positions())."""
    return get_position_data() or {}


# ─── Исполнения (Фаза 6.3) ───

def get_executions(limit: int = 20) -> List[dict]:
    """Последние fill-уведомления."""
    with _cache_lock:
        return list(_execution_cache[-limit:])


def get_executions_for_symbol(symbol: str, limit: int = 10) -> List[dict]:
    """Fill-уведомления для конкретного символа."""
    with _cache_lock:
        return [e for e in _execution_cache if e.get('symbol') == symbol][-limit:]


# ─── Кошелёк (Фаза 6.3) ───

def get_wallet(coin: str = 'USDT') -> Optional[dict]:
    """Баланс кошелька из WS-кеша."""
    with _cache_lock:
        return _wallet_cache.get(coin)


def get_wallet_balance(coin: str = 'USDT') -> Optional[float]:
    """Доступный баланс кошелька."""
    w = get_wallet(coin)
    return w.get('availableBalance') if w else None


def get_wallet_equity(coin: str = 'USDT') -> Optional[float]:
    """Equity кошелька."""
    w = get_wallet(coin)
    return w.get('equity') if w else None


# ─── Статистика ───

def stats() -> dict:
    """Статистика WS-клиента."""
    with _cache_lock:
        base = {
            'connected': _connected,
            'private_connected': _private_connected,
            'full_enabled': _WS_FULL_ENABLED,
            'last_update': _last_update,
            'last_private_update': _last_private_update,
            'symbols': len(_subscribed_symbols),
            'prices': len(_price_cache),
            'bbs': sum(1 for bb in _bb_cache.values() if bb),
            'age_sec': round(time.time() - _last_update, 1) if _last_update else -1,
            'private_age_sec': round(time.time() - _last_private_update, 1) if _last_private_update else -1,
            'cached_klines': sum(
                len(intervals) for intervals in _kline_cache.values()
            ),
            'orderbook_symbols': len(_orderbook_cache),
            'position_symbols': len(_position_cache),
            'executions': len(_execution_cache),
            'wallet_coins': len(_wallet_cache),
        }
        return base
