"""API-запросы к Bybit v5 — httpx + HMAC-SHA256.

v5: замена subprocess(BYBIT_CLI) на httpx (код-ревью 14.06.2026).
Ускорение 10-50x, устранение command injection, переиспользование соединений.
"""
import hashlib, hmac, json, os, time
import httpx
from .alerts import log_event

# === Credentials (читаются один раз при импорте) ===
_API_KEY = None
_API_SECRET = None
_BASE_URL = 'https://api.bytick.com'

def _load_credentials():
    global _API_KEY, _API_SECRET
    if _API_KEY:
        return
    # Приоритет: env (systemd EnvironmentFile) → legacy config
    _API_KEY = os.environ.get('BYBIT_API_KEY')
    _API_SECRET = os.environ.get('BYBIT_API_SECRET')
    if _API_KEY and _API_SECRET:
        return
    legacy = os.path.expanduser('~/.config/bybit-cli/config')
    try:
        with open(legacy) as f:
            for line in f:
                line = line.strip()
                if line.startswith('BYBIT_API_KEY='):
                    _API_KEY = line.split('=', 1)[1].strip()
                elif line.startswith('BYBIT_API_SECRET='):
                    _API_SECRET = line.split('=', 1)[1].strip()
        if _API_KEY and _API_SECRET:
            log_event('migrate: loaded credentials from legacy path')
    except Exception as e:
        log_event(f'⚠️ api credentials load: {e}')
    if not _API_KEY or not _API_SECRET:
        log_event('api: credentials not loaded')


# === Session (connection reuse) ===
_session = None

def _get_session():
    global _session
    if _session is None:
        _session = httpx.Client()
        _session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'bybit-ws/4.0',
        })
    return _session


# === HMAC signing ===
def _sign_request(method, path, body=None):
    """Return (timestamp, recv_window, sign) for X-BAPI headers."""
    _load_credentials()
    if not _API_KEY or not _API_SECRET:
        log_event('⚠️ api: sign attempted without credentials')
        return None, None, None
    ts = str(int(time.time() * 1000))
    recv = '5000'
    if method == 'GET' and '?' in path:
        # For GET, sign the query string (without leading ?)
        body_str = path.split('?', 1)[1]
    elif body:
        body_str = json.dumps(body, separators=(', ', ': '))
    else:
        body_str = ''
    sign_str = ts + _API_KEY + recv + body_str
    sign = hmac.new(_API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return ts, recv, sign


def _auth_headers(method, path, body=None):
    ts, recv, sign = _sign_request(method, path, body)
    return {
        'X-BAPI-API-KEY': _API_KEY,
        'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-RECV-WINDOW': recv,
        'X-BAPI-SIGN': sign,
    }


# === Retry config ===
RETRY_DELAYS = [1, 3, 5]
MAX_RETRIES = len(RETRY_DELAYS)
REQUEST_TIMEOUT = 15

# === Circuit breaker (анти-спам запросами) ===
# После N ошибок подряд — пауза на M секунд перед следующей попыткой.
# Сбрасывается при первом успешном запросе.
_cb_errors = 0         # счётчик последовательных ошибок
_cb_until = 0.0        # время (time.monotonic()), до которого запросы запрещены
_CB_THRESHOLD = 5      # ошибок подряд → включаем тормоз
_CB_COOLDOWN = 300     # секунд паузы при срабатывании (5 мин)
_CB_MAX_COOLDOWN = 900 # максимум паузы (15 мин) — exponential growth cap


def _cb_check() -> bool:
    """Проверить circuit breaker. True = можно делать запрос."""
    global _cb_errors, _cb_until
    if _cb_until > 0 and time.monotonic() < _cb_until:
        return False  # тормоз активен
    return True


def _cb_record(success: bool):
    """Записать результат запроса: success=True сбрасывает, False инкрементит."""
    global _cb_errors, _cb_until
    if success:
        _cb_errors = 0
        _cb_until = 0
    else:
        _cb_errors += 1
        if _cb_errors >= _CB_THRESHOLD:
            delay = min(_CB_COOLDOWN * (2 ** (_cb_errors - _CB_THRESHOLD)), _CB_MAX_COOLDOWN)
            _cb_until = time.monotonic() + delay
            log_event(f'🛑 API circuit breaker: {_cb_errors} ошибок подряд, пауза {delay}с')


def bybit(method, path, body=None, retries=None):
    """Отправить запрос к Bybit API с retry.

    Args:
        method: 'GET' или 'POST'
        path: путь API, например '/v5/position/list?category=linear&settleCoin=USDT'
        body: dict для JSON-тела (только POST)
        retries: кол-во повторных попыток (по умолчанию MAX_RETRIES для всех)

    Returns:
        dict из JSON-ответа или None при ошибке.
    """
    if retries is None:
        retries = MAX_RETRIES

    # Circuit breaker: если набрали ошибок — не долбим
    if not _cb_check():
        return None

    _load_credentials()
    session = _get_session()
    url = _BASE_URL + path

    for attempt in range(retries + 1):
        try:
            headers = _auth_headers(method, path, body)
            if method == 'GET':
                resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                if isinstance(body, dict):
                    body_bytes = json.dumps(body, separators=(', ', ': ')).encode()
                elif isinstance(body, str):
                    body_bytes = body.encode()
                else:
                    body_bytes = b''
                resp = session.post(url, content=body_bytes, headers=headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                err = f'HTTP {resp.status_code} {method} {path}: {resp.text[:80]}'
                if resp.status_code == 404:
                    log_event(f'bybit 404 (endpoint not found, skipping): {err}')
                    return None
                if attempt < retries:
                    if resp.status_code == 429:
                        delay = 2 ** attempt
                        log_event(f'bybit 429 rate-limit, backoff {attempt+1}/{retries} in {delay}s')
                    else:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        log_event(f'bybit retry {attempt+1}/{retries} in {delay}s: {err}')
                    time.sleep(delay)
                    continue
                log_event(f'bybit error (final): {err}')
                _cb_record(False)
                return None

            result = resp.json()
            if not isinstance(result, dict):
                log_event(f'bybit non-dict response ({type(result).__name__}): {method} {path[:60]}')
                return None

            # 10003 = API key invalid — не ретраить, ключ мёртв
            if result.get('retCode') == 10003:
                log_event(f'bybit: API key invalid (10003) — проверь ключ в админке')
                _cb_record(False)
                return None

            # 10004 = sign not match — логируем, но ретраим (может быть рассинхрон времени)
            if result.get('retCode') == 10004:
                log_event(f'bybit: sign not match (10004) — возможен рассинхрон времени')
                if attempt < retries:
                    time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                    continue
                _cb_record(False)
                return None

            _cb_record(True)
            return result

        except httpx.TimeoutException:
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            _cb_record(False)
            return None

        except httpx.ConnectError as e:
            log_event(f'bybit connection error: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            _cb_record(False)
            return None

        except json.JSONDecodeError as e:
            log_event(f'bybit json error: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            _cb_record(False)
            return None

        except Exception as e:
            log_event(f'bybit exception: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            _cb_record(False)
            return None

    return None


# === High-level API ===
# Bybit v5 REST API docs: https://bybit-exchange.github.io/docs/v5/


def fetch_positions():
    """Получить все открытые позиции по USDT-линейным контрактам.

    Endpoint: GET /v5/position/list
    Docs: https://bybit-exchange.github.io/docs/v5/position#get-position-info
    """
    data = bybit('GET', '/v5/position/list?category=linear&settleCoin=USDT')
    if not data or data.get('retCode') != 0:
        return {}
    positions = {}
    for p in data['result'].get('list', []):
        sym = p['symbol']
        size = float(p.get('size', 0))
        if size > 0:
            stop = p.get('stopLoss')
            liq = p.get('liqPrice', '')
            position_im = float(p.get('positionIM', 0))
            cum_rpnl = float(p.get('cumRealisedPnl', 0))
            open_time = int(p.get('openTime', 0) or 0)
            margin = float(p.get('positionIM', p.get('margin', 0)))
            positions[sym] = {
                'size': size,
                'entry': float(p['avgPrice']),
                'mark': float(p['markPrice']),
                'upnl': float(p['unrealisedPnl']),
                'side': p['side'],
                'stopLoss': float(stop) if stop else None,
                'positionIdx': int(p.get('positionIdx', 0)),
                'liqPrice': float(liq) if liq and liq != '' else None,
                'leverage': float(p.get('leverage', '1')),
                'positionIM': position_im,
                'cumRealisedPnl': cum_rpnl,
                'openTime': open_time,
                'margin': margin,
            }
    return positions


def fetch_orders():
    """Получить активные ордера с пагинацией (до 50 за запрос).

    Endpoint: GET /v5/order/realtime
    Docs: https://bybit-exchange.github.io/docs/v5/order#get-open-orders
    """
    orders = {}
    cursor = ''
    while True:
        path = f'/v5/order/realtime?category=linear&settleCoin=USDT&limit=50'
        if cursor:
            path += f'&cursor={cursor}'
        data = bybit('GET', path)
        # Защита от строкового ответа (HTTP-ошибка, таймаут)
        if not data or not isinstance(data, dict) or data.get('retCode') != 0:
            break
        olist = data['result'].get('list', [])
        if not olist:
            break
        for o in olist:
            sym = o['symbol']
            oid = o['orderId']
            otype = o['orderType']
            side = o['side']
            price = float(o.get('price', 0) or 0)
            trigger = float(o.get('triggerPrice', 0) or 0)
            qty = float(o['qty'])
            reduce = o.get('reduceOnly', False)
            create_time = o.get('createdTime', '')
            if reduce and otype == 'Market':
                kind = 'SL'
            elif reduce and otype == 'Limit':
                kind = 'TP'
            elif side == 'Buy':
                kind = 'LIMIT_ENTRY'
            else:
                kind = 'OTHER'
            cum_exec = float(o.get('cumExecQty', 0) or 0)
            key = f'{sym}_{oid}'
            orders[key] = {
                'symbol': sym, 'orderId': oid, 'status': o['orderStatus'],
                'kind': kind, 'price': price, 'trigger': trigger,
                'qty': qty, 'side': side, 'createdTime': create_time,
                'cumExecQty': cum_exec,
            }
        cursor = data['result'].get('nextPageCursor', '')
        if not cursor:
            break
    return orders


def fetch_open_orders() -> list[dict]:
    """Получить активные ордера как список dict (сырой формат API).

    Используется TP/SL self-check — нужны поля symbol, side, orderType,
    stopOrderType, orderStatus из оригинального ответа Bybit.
    """
    all_orders = []
    cursor = ''
    while True:
        path = f'/v5/order/realtime?category=linear&settleCoin=USDT&limit=50'
        if cursor:
            path += f'&cursor={cursor}'
        data = bybit('GET', path)
        if not data or not isinstance(data, dict) or data.get('retCode') != 0:
            break
        olist = data['result'].get('list', [])
        if not olist:
            break
        all_orders.extend(olist)
        cursor = data['result'].get('nextPageCursor', '')
        if not cursor:
            break
    return all_orders


def place_stop_loss(symbol, positionIdx, side, qty, stop_price):
    """Поставить/обновить стоп-лосс через trading-stop.

    Endpoint: POST /v5/position/trading-stop
    Docs: https://bybit-exchange.github.io/docs/v5/position#set-trading-stop
    """
    body = {'category': 'linear', 'symbol': symbol,
            'positionIdx': positionIdx,
            'stopLoss': str(stop_price), 'slTriggerBy': 'MarkPrice',
            'tpslMode': 'Full'}
    data = bybit('POST', '/v5/position/trading-stop', body)
    if data and data.get('retCode') == 0:
        log_event(f'✅ SL поставлен {symbol} @ ${stop_price:.4f}')
        return True
    else:
        err = data.get('retMsg', '?') if data else 'no response'
        log_event(f'❌ SL ошибка {symbol}: {err}')
        return False


def place_take_profit(symbol, positionIdx, side, qty, tp_price):
    """Поставить лимитный reduce-only ордер (тейк-профит).

    Endpoint: POST /v5/order/create
    Docs: https://bybit-exchange.github.io/docs/v5/order#create-order
    """
    tp_side = 'Sell' if side == 'Buy' else 'Buy'
    qty_str = str(int(qty)) if qty == int(qty) else str(qty)
    body = {'category': 'linear', 'symbol': symbol, 'side': tp_side,
            'positionIdx': positionIdx, 'orderType': 'Limit', 'qty': qty_str,
            'price': str(tp_price), 'reduceOnly': True, 'timeInForce': 'GTC'}
    data = bybit('POST', '/v5/order/create', body)
    if data and data.get('retCode') == 0:
        log_event(f'✅ TP поставлен {symbol} @ ${tp_price:.4f}')
        return True
    else:
        err = data.get('retMsg', '?') if data else 'no response'
        if '110043' in str(err):
            log_event(f'ℹ️ TP уже существует {symbol}')
            return True
        log_event(f'❌ TP ошибка {symbol}: {err}')
        return False


def cancel_order(symbol, order_id):
    """Отменить ордер.

    Endpoint: POST /v5/order/cancel
    Docs: https://bybit-exchange.github.io/docs/v5/order#cancel-order
    """
    body = {'category': 'linear', 'symbol': symbol, 'orderId': order_id}
    data = bybit('POST', '/v5/order/cancel', body)
    if data and data.get('retCode') == 0:
        log_event(f'🗑️ Отменён ордер {symbol}/{order_id[:8]}')
        return True
    return False


def get_bb_lower(symbol, interval='D'):
    """Получить нижнюю полосу Боллинджера (SMA - 2σ).

    Endpoint: GET /v5/market/kline
    Docs: https://bybit-exchange.github.io/docs/v5/market/kline#get-kline
    """
    import math
    data = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=20')
    if not data or data.get('retCode') != 0:
        return None
    try:
        candles = [float(c[4]) for c in data['result']['list'][:20]][::-1]
        if len(candles) < 20:
            return None
        sma = sum(candles) / 20
        var = sum((x - sma) ** 2 for x in candles) / 20
        std = math.sqrt(var)
        return sma - 2 * std
    except Exception as e:
        log_event(f'⚠️ api bb_lower: {e}')
        return None


def place_order(symbol, side, order_type, qty, price=None, reduce_only=False, position_idx=0):
    """Разместить ордер через Bybit v5 REST API."""
    body = {
        'category': 'linear',
        'symbol': symbol,
        'side': side,
        'orderType': order_type,
        'qty': str(qty),
        'positionIdx': position_idx,
    }
    if price is not None:
        body['price'] = str(price)
    if reduce_only:
        body['reduceOnly'] = True
    return bybit('POST', '/v5/order/create', body)


def get_bb_data(symbol, interval='D'):
    """Получить полные данные BB: lower, middle, upper, cur, bb_pos.

    Endpoint: GET /v5/market/kline
    Docs: https://bybit-exchange.github.io/docs/v5/market/kline#get-kline
    """
    import math
    data = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=20')
    if not data or data.get('retCode') != 0:
        return None
    try:
        candles = [float(c[4]) for c in data['result']['list'][:20]][::-1]
        if len(candles) < 20:
            return None
        cur = candles[-1]
        sma = sum(candles) / 20
        std = math.sqrt(sum((x - sma) ** 2 for x in candles) / 20)
        upper = sma + 2 * std
        lower = sma - 2 * std
        middle = sma
        bb_pos = (cur - lower) / (upper - lower) * 100 if upper != lower else 50
        return {'lower': lower, 'middle': middle, 'upper': upper, 'cur': cur, 'bb_pos': bb_pos}
    except Exception as e:
        log_event(f'⚠️ api get_bb_data: {e}')
        return None


def fetch_funding_total(symbol, since_ms):
    """Суммировать фандинг-выплаты по символу с openTime.

    Endpoint: GET /v5/account/transaction-log?type=FUNDING (authenticated)
    Note: /v5/account/funding-history removed in Bybit v5.
    Fallback: returns 0 if endpoint unavailable (avoids retry flood).

    Args:
        symbol: e.g. DOTUSDT
        since_ms: openTime позиции в миллисекундах (начало окна)

    Returns:
        float: суммарный фандинг (отрицательный = платил, положительный = получал)
    """
    total = 0.0
    cursor = ''
    while True:
        path = (
            f'/v5/account/transaction-log?category=linear'
            f'&currency=USDT&type=SETTLEMENT&startTime={since_ms}&limit=50'
        )
        if cursor:
            path += f'&cursor={cursor}'
        data = bybit('GET', path)
        if not data or data.get('retCode') != 0:
            # Endpoint removed in Bybit v5 — fallback to 0, don't retry
            if data is None:
                break
            if data.get('retCode') == 10001:
                break
            break
        items = data['result'].get('list', [])
        if not items:
            break
        for item in items:
            total += float(item.get('funding', 0))
        cursor = data['result'].get('nextPageCursor', '')
        if not cursor:
            break
    return total


def fetch_atr(symbol, interval='D', period=14):
    """Рассчитать ATR (Average True Range) через klines.

    Endpoint: GET /v5/market/kline
    Использует Wilder's smoothing: ATR = (prev_ATR*(period-1) + TR) / period

    Args:
        symbol: e.g. DOTUSDT
        interval: D, 4h, 1h, 15m, 5m
        period: число свечей для расчёта (default 14)

    Returns:
        float: ATR в долларах, или None при ошибке
    """
    try:
        # Нужно period+1 свечей (первая даёт prevClose для TR)
        data = bybit('GET',
                     f'/v5/market/kline?category=linear&symbol={symbol}'
                     f'&interval={interval}&limit={period + 1}')
        if not data or data.get('retCode') != 0:
            return None

        candles = data['result'].get('list', [])
        if len(candles) < period:
            return None

        # candles newest-first → разворачиваем для хронологического порядка
        candles = candles[:period + 1][::-1]

        # True Range для каждой свечи (кроме первой — нужен prevClose)
        tr_values = []
        for i in range(1, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)

        if not tr_values:
            return None

        # Wilder's smoothing
        atr = sum(tr_values) / len(tr_values)  # начальное SMA
        if len(tr_values) > 1:
            # Рекурсивное сглаживание для консистентности с Wilder's
            for tr in tr_values:
                atr = (atr * (period - 1) + tr) / period

        return round(atr, 8)

    except Exception as e:
        log_event(f'⚠️ api fetch_atr: {e}')
        return None


# ═══════════════════════════════════════════════════════════
# Async API (Фаза 4.7 — asyncio-миграция)
# ═══════════════════════════════════════════════════════════

import asyncio

async def bybit_async(method, path, body=None, retries=None):
    """Async-версия bybit() через run_in_executor.

    Синхронный httpx.Client в отдельном потоке — asyncio может
    реально отменить ожидание по таймауту (в отличие от httpx.AsyncClient,
    который блокирует event loop на уровне C-кода h11/h2).
    """
    import concurrent.futures
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, bybit, method, path, body, retries),
            timeout=15.0
        )
    except asyncio.TimeoutError:
        log_event(f'bybit-async executor timeout: {method} {path[:60]}')
        return None
    except concurrent.futures.CancelledError:
        return None


async def fetch_positions_async():
    """Async: получить все открытые позиции."""
    data = await bybit_async('GET', '/v5/position/list?category=linear&settleCoin=USDT')
    if not data or data.get('retCode') != 0:
        return {}
    positions = {}
    for p in data['result'].get('list', []):
        sym = p['symbol']
        size = float(p.get('size', 0))
        if size > 0:
            stop = p.get('stopLoss')
            liq = p.get('liqPrice', '')
            positions[sym] = {
                'size': size,
                'entry': float(p['avgPrice']),
                'mark': float(p['markPrice']),
                'upnl': float(p['unrealisedPnl']),
                'side': p['side'],
                'stopLoss': float(stop) if stop else None,
                'positionIdx': int(p.get('positionIdx', 0)),
                'liqPrice': float(liq) if liq and liq != '' else None,
                'leverage': float(p.get('leverage', '1')),
                'positionIM': float(p.get('positionIM', 0)),
                'cumRealisedPnl': float(p.get('cumRealisedPnl', 0)),
                'openTime': int(p.get('openTime', 0) or 0),
                'margin': float(p.get('positionIM', p.get('margin', 0))),
            }
    return positions


async def fetch_orders_async():
    """Async: получить активные ордера."""
    orders = {}
    cursor = ''
    while True:
        path = f'/v5/order/realtime?category=linear&settleCoin=USDT&limit=50'
        if cursor:
            path += f'&cursor={cursor}'
        data = await bybit_async('GET', path)
        if not data or not isinstance(data, dict) or data.get('retCode') != 0:
            break
        olist = data['result'].get('list', [])
        if not olist:
            break
        for o in olist:
            sym = o['symbol']
            oid = o['orderId']
            reduce = o.get('reduceOnly', False)
            otype = o['orderType']
            kind = 'SL' if (reduce and otype == 'Market') else ('TP' if (reduce and otype == 'Limit') else ('LIMIT_ENTRY' if o['side'] == 'Buy' else 'OTHER'))
            key = f'{sym}_{oid}'
            orders[key] = {
                'symbol': sym, 'orderId': oid, 'status': o['orderStatus'],
                'kind': kind, 'price': float(o.get('price', 0) or 0),
                'trigger': float(o.get('triggerPrice', 0) or 0),
                'qty': float(o['qty']), 'side': o['side'],
                'createdTime': o.get('createdTime', ''),
                'cumExecQty': float(o.get('cumExecQty', 0) or 0),
            }
        cursor = data['result'].get('nextPageCursor', '')
        if not cursor:
            break
    return orders


async def get_bb_data_async(symbol, interval='D'):
    """Async: BB-данные."""
    import math
    data = await bybit_async('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit=20')
    if not data or data.get('retCode') != 0:
        return None
    try:
        candles = [float(c[4]) for c in data['result']['list'][:20]][::-1]
        if len(candles) < 20:
            return None
        cur = candles[-1]
        sma = sum(candles) / 20
        std = math.sqrt(sum((x - sma) ** 2 for x in candles) / 20)
        upper = sma + 2 * std
        lower = sma - 2 * std
        middle = sma
        bb_pos = (cur - lower) / (upper - lower) * 100 if upper != lower else 50
        return {'lower': lower, 'middle': middle, 'upper': upper, 'cur': cur, 'bb_pos': bb_pos}
    except Exception as e:
        log_event(f'⚠️ api get_bb_data: {e}')
        return None


async def fetch_positions_and_orders():
    """Async: последовательная загрузка позиций и ордеров.

    Последовательные запросы вместо asyncio.gather — намеренно.
    Таймаут 15с на каждый запрос внутри bybit_async (run_in_executor).
    """
    positions = await fetch_positions_async()
    orders = await fetch_orders_async()
    return positions, orders


# ═══════════════════════════════════════════
# Phase 6.5: Unified Exchange Adapter Bridge
# ═══════════════════════════════════════════

def get_bb_data_unified(symbol: str, interval: str = 'D', period: int = 20, std: float = 2.0) -> dict:
    """Get BB data through unified adapter (supports Bybit/Binance/OKX).
    Falls back to native Bybit get_bb_data if adapter unavailable.
    """
    try:
        from .exchange_adapter import exchange as get_exchange
        ex = get_exchange()
        if ex.name != 'bybit':
            return ex.get_bb(symbol, interval, period, std)
    except Exception:
        pass
    return get_bb_data(symbol, interval)
