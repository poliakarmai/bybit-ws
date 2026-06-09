"""API-запросы к Bybit v2 — с process-group таймаутом."""
import json, os, signal, subprocess, time
from . import BYBIT_CLI, EVENTS_LOG
from .alerts import log_event  # единое логирование

RETRY_DELAYS = [1, 3, 5]
MAX_RETRIES = len(RETRY_DELAYS)

def bybit(method, path, body=None, retries=None):
    if retries is None:
        # POST-запросы тоже с retry — ордера критичны (фикс код-ревью Manus AI)
        retries = MAX_RETRIES
    for attempt in range(retries + 1):
        cmd = [BYBIT_CLI, 'raw', method, path]
        if body:
            cmd.append(json.dumps(body))
        proc = None
        try:
            # start_new_session=True → можно убить всю группу процессов
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True
            )
            stdout, stderr = proc.communicate(timeout=15)
            if proc.returncode != 0:
                err = stderr.strip()[:100]
                if attempt < retries:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)]
                    log_event(f'bybit retry {attempt+1}/{retries} in {delay}s: {err}')
                    time.sleep(delay)
                    continue
                log_event(f'bybit error (final): {err}')
                return None
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            log_event(f'bybit json error: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)])
                continue
            return None
        except subprocess.TimeoutExpired:
            # Убить всю группу процессов: bash + curl + все потомки
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                proc.wait(timeout=5)
            log_event(f'bybit timeout after 15s: {method} {path[:60]}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)])
                continue
            return None
        except Exception as e:
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            log_event(f'bybit exception: {e}')
            if attempt < retries:
                time.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)-1)])
                continue
            return None
    return None

def log_event(msg):
    from datetime import datetime
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(EVENTS_LOG, 'a') as f:
        f.write(f'[{ts}] {msg}\n')

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
            open_time = p.get('openTime', '')
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
            key = f'{sym}_{oid}'
            orders[key] = {
                'symbol': sym, 'orderId': oid, 'status': o['orderStatus'],
                'kind': kind, 'price': price, 'trigger': trigger,
                'qty': qty, 'side': side, 'createdTime': create_time,
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
    except:
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
    except:
        return None
