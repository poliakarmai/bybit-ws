"""BB Scalping M5/M15 — вход x10 при касании полосы BB с RSI-фильтром.

Стратегия: на M5 свечах цена возвращается к Middle BB в 70%+ случаев.
С x10 даже 1% движения = 10% прибыли на маржу.

LONG: цена касается Lower BB M5 + RSI(14) < 35
SHORT: цена касается Upper BB M5 + RSI(14) > 65
SL: 3% от входа, TP: Middle BB
Плечо: 10x, маржа: $10
Лимит: макс 3 скальп-позиции, кулдаун 1ч на монету
Только Tier A/B (меньше волатильность — меньше ложных срабатываний)
"""
import json
import math
import os
import time
from datetime import datetime

from .api import bybit
from .alerts import log_event, add_alert, _is_duplicate
from .position_sizing import margin_for_strategy

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
SCALP_STATE_FILE = os.path.join(DATA_DIR, 'scalp_state.json')

# Параметры
SCALP_LEVERAGE = 10
SCALP_MARGIN = 10.0         # $10 на позицию
SCALP_SL_PCT = 0.03          # 3% SL
MAX_SCALP_POSITIONS = 3
SCALP_COOLDOWN = 3600        # 1 час на монету
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
BB_PERIOD = 20
RSI_PERIOD = 14

# Tier A/B — только ликвидные монеты
SCALP_TIERS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT',
    'ADAUSDT', 'DOTUSDT', 'LTCUSDT', 'XRPUSDT', 'UNIUSDT',
    'NEARUSDT', 'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT',
    'ENAUSDT', 'ATOMUSDT', 'ALGOUSDT', 'FETUSDT', 'RUNEUSDT',
}


def _load_state():
    try:
        if os.path.exists(SCALP_STATE_FILE):
            with open(SCALP_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log_event(f'⚠️ bb_scalp: {e}')
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(SCALP_STATE_FILE), exist_ok=True)
    with open(SCALP_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _get_kline(sym, interval='5', limit=30):
    """Получить свечи и посчитать BB + RSI."""
    try:
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval={interval}&limit={limit}')
        closes = [float(c[4]) for c in data.get('result', {}).get('list', [])]
        if len(closes) < BB_PERIOD:
            return None
        return closes
    except Exception:
        return None


def _calc_bb(closes, period=BB_PERIOD):
    """Bollinger Bands: SMA ± 2σ."""
    window = closes[:period]
    sma = sum(window) / len(window)
    variance = sum((c - sma) ** 2 for c in window) / len(window)
    std = math.sqrt(variance)
    return {
        'middle': round(sma, 8),
        'upper': round(sma + 2 * std, 8),
        'lower': round(sma - 2 * std, 8),
    }


def _calc_rsi(closes, period=RSI_PERIOD):
    """RSI за указанный период."""
    if len(closes) < period + 1:
        return 50
    gains = 0
    losses = 0
    for i in range(1, period + 1):
        diff = closes[i - 1] - closes[i]  # reversed: closes[0] is newest
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100
    rs = (gains / period) / (losses / period)
    return round(100 - (100 / (1 + rs)), 1)


def check_scalp_signals(positions, balance_usdt):
    """Сканирует Tier A/B монеты на скальп-сигналы. Возвращает список алертов."""
    state = _load_state()
    now = time.time()
    alerts = []
    entries = []

    # Считаем текущие скальп-позиции
    scalp_count = sum(1 for p in positions.values()
                       if p.get('leverage') == SCALP_LEVERAGE and p['side'] in ('Buy', 'Sell'))

    if scalp_count >= MAX_SCALP_POSITIONS:
        return [], []

    # Проверяем кулдаун и free-маржу
    free_margin = balance_usdt - sum(float(p.get('margin', 0)) for p in positions.values())

    for sym in SCALP_TIERS:
        if sym in positions:
            continue

        # Кулдаун
        last_entry = state.get(sym, {}).get('last_scalp', 0)
        if now - last_entry < SCALP_COOLDOWN:
            continue

        closes = _get_kline(sym, '5', 30)
        if not closes or len(closes) < BB_PERIOD + 1:
            continue

        bb = _calc_bb(closes)
        rsi = _calc_rsi(closes)
        price = closes[0]

        # LONG: цена у Lower BB + RSI < 35
        if price <= bb['lower'] * 1.005 and rsi < RSI_OVERSOLD:
            direction = 'LONG'
            entry_price = price
            sl_price = round(entry_price * (1 - SCALP_SL_PCT), 8)
            tp_price = bb['middle']
            side = 'Buy'
        # SHORT: цена у Upper BB + RSI > 65
        elif price >= bb['upper'] * 0.995 and rsi > RSI_OVERBOUGHT:
            direction = 'SHORT'
            entry_price = price
            sl_price = round(entry_price * (1 + SCALP_SL_PCT), 8)
            tp_price = bb['middle']
            side = 'Sell'
        else:
            continue

        # Проверка free margin
        scalp_margin = margin_for_strategy('scalp', score=5.5)
        if scalp_margin <= 0 or free_margin < scalp_margin * 1.5:
            continue

        qty = math.ceil(scalp_margin * SCALP_LEVERAGE / entry_price * 100) / 100
        if qty <= 0:
            continue

        msg = (f'⚡ СКАЛЬП {sym} {direction} x{SCALP_LEVERAGE}: '
               f'${entry_price:.4f} RSI={rsi} SL={sl_price:.4f} TP={tp_price:.4f}')

        if not _is_duplicate(msg, 'ENTRY'):
            alerts.append(msg)
            entries.append({
                'symbol': sym, 'side': side, 'direction': direction,
                'entry': entry_price, 'sl': sl_price, 'tp': tp_price,
                'qty': qty, 'leverage': SCALP_LEVERAGE, 'margin': SCALP_MARGIN,
                'rsi': rsi,
            })
            state[sym] = {'last_scalp': now}

    _save_state(state)
    return alerts, entries


def execute_scalp(entry_info):
    """Выполнить скальп-вход: плечо → market → SL/TP."""
    sym = entry_info['symbol']
    side = entry_info['side']

    # Установка плеча
    try:
        lev_body = {'category': 'linear', 'symbol': sym,
                     'buyLeverage': str(SCALP_LEVERAGE),
                     'sellLeverage': str(SCALP_LEVERAGE)}
        bybit('POST', '/v5/position/set-leverage', lev_body)
    except Exception as e:
        log_event(f'⚠️ bb_scalp: {e}')

    # Market order — пробуем idx=0, затем idx=1
    for idx in (0, 1):
        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear', 'symbol': sym, 'side': side,
                'orderType': 'Market', 'qty': str(entry_info['qty']),
                'positionIdx': idx, 'timeInForce': 'IOC',
            })
            if order.get('retCode') == 0:
                log_event(f'⚡ СКАЛЬП {sym} {entry_info["direction"]}: market @ ~${entry_info["entry"]:.4f} '
                          f'x{SCALP_LEVERAGE} qty={entry_info["qty"]} idx={idx}')
                # SL/TP через trading-stop
                ts_body = {
                    'category': 'linear', 'symbol': sym, 'positionIdx': idx,
                    'stopLoss': str(entry_info['sl']),
                    'takeProfit': str(entry_info['tp']),
                    'slTriggerBy': 'MarkPrice', 'tpTriggerBy': 'MarkPrice',
                    'tpslMode': 'Full',
                }
                bybit('POST', '/v5/position/trading-stop', ts_body)
                return True
            elif order.get('retCode') == 10001:
                continue
            else:
                log_event(f'⚠️ СКАЛЬП {sym}: {order.get("retMsg", "?")}')
                return False
        except Exception as e:
            log_event(f'⚠️ СКАЛЬП {sym}: исключение — {e}')
            return False
    return False
