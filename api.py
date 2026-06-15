"""API-запросы к Bybit v3 — нативный requests + HMAC-SHA256.

v3: замена subprocess(BYBIT_CLI) на requests (код-ревью 14.06.2026).
Ускорение 10-50×, устранение command injection, переиспользование соединений.
"""
import hashlib, hmac, json, os, time
import requests
from .alerts import log_event

# === Credentials (читаются один раз при импорте) ===
_API_KEY = None
_API_SECRET = None
_BASE_URL = 'https://api.bytick.com'

def _load_credentials():
    global _API_KEY, _API_SECRET
    if _API_KEY:
        return
    config_path = os.path.expanduser('~/.config/bybit-cli/config')
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('BYBIT_API_KEY='):
                    _API_KEY = line.split('=', 1)[1].strip()
                elif line.startswith('BYBIT_API_SECRET='):
                    _API_SECRET = line.split('=', 1)[1].strip()
    except Exception as e:
        log_event(f'⚠️ api: cannot read credentials: {e}')
    if not _API_KEY or not _API_SECRET:
        log_event('⚠️ api: credentials not loaded — API calls will fail')


# === Session (connection reuse) ===
_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
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
REQUEST_TIMEOUT = 15  # совпадает с таймаутом subprocess.communicate


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

    _load_credentials()
    session = _get_session()
    url = _BASE_URL + path

    for attempt in range(retries + 1):
        try:
            headers = _auth_headers(method, path, body)
            if method == 'GET':
                resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                resp = session.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                err = f'HTTP {resp.status_code}: {resp.text[:100]}'
                if attempt < retries:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    log_event(f'bybit retry {attempt+1}/{retries} in {delay}s: {err}')
                    time.sleep(delay)
                    continue
                log_event(f'bybit error (final): {err}')
                return None

            return resp.json()

        except requests.exceptions.Timeout:
            log_event(f'bybit timeout after {REQUEST_TIMEOUT}s: {method} {path[:60]}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            return None

        except requests.exceptions.ConnectionError as e:
            log_event(f'bybit connection error: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            return None

        except json.JSONDecodeError as e:
            log_event(f'bybit json error: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            return None

        except Exception as e:
            log_event(f'bybit exception: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            return None

    return None


# === High-level API (без изменений — используют bybit() выше) ===

def fetch_positions():
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
            margin = float(p.get('margin', 0))
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
    orders = {}
    cursor = ''
    while True:
        path = f'/v5/order/realtime?category=linear&settleCoin=USDT&limit=50'
        if cursor:
            path += f'&cursor={cursor}'
        data = bybit('GET', path)
        if not data or data.get('retCode') != 0:
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


def place_stop_loss(symbol, positionIdx, side, qty, stop_price):
    sl_side = 'Sell' if side == 'Buy' else 'Buy'
    body = {'category': 'linear', 'symbol': symbol, 'side': sl_side,
            'positionIdx': positionIdx, 'orderType': 'Market', 'qty': str(qty),
            'stopLoss': str(stop_price), 'triggerBy': 'LastPrice', 'slTriggerBy': 'MarkPrice'}
    data = bybit('POST', '/v5/position/trading-stop', body)
    if data and data.get('retCode') == 0:
        log_event(f'✅ SL поставлен {symbol} @ ${stop_price:.4f}')
        return True
    else:
        err = data.get('retMsg', '?') if data else 'no response'
        log_event(f'❌ SL ошибка {symbol}: {err}')
        return False


def place_take_profit(symbol, positionIdx, side, qty, tp_price):
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
    body = {'category': 'linear', 'symbol': symbol, 'orderId': order_id}
    data = bybit('POST', '/v5/order/cancel', body)
    if data and data.get('retCode') == 0:
        log_event(f'🗑️ Отменён ордер {symbol}/{order_id[:8]}')
        return True
    return False


def get_bb_lower(symbol, interval='D'):
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
    except Exception:
        return None


def get_bb_data(symbol, interval='D'):
    """Получить полные данные BB: lower, middle, upper, cur, bb_pos."""
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
    except Exception:
        return None
