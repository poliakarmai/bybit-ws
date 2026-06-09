"""Mean Reversion Extreme x10 — вход при экстремальных BB% на дневном таймфрейме.

Стратегия: при BB% < 5% (LONG) или > 95% (SHORT) на Daily — высокая
вероятность возврата к Middle BB (10-20% движения).

Только Tier A/B монеты (меньше шума).
Плечо x10, SL 5% от входа, TP на Middle BB.
Маржа $10, макс 5 позиций этого типа.

Отличие от основной Bollinger Grid (3x): выше плечо, жёстче условия входа,
быстрее выход (TP на Middle, не на Upper/Lower).
"""
import json
import math
import os
import time
from datetime import datetime

from .api import bybit
from .alerts import log_event, add_alert, _is_duplicate

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
MEAN_STATE_FILE = os.path.join(DATA_DIR, 'mean_revert_state.json')

MEAN_LEVERAGE = 10
MEAN_MARGIN = 10.0
MEAN_SL_PCT = 0.05           # 5% SL (при x10 = -50% маржи)
MAX_MEAN_POSITIONS = 5
MEAN_COOLDOWN = 7200          # 2 часа на монету

# Экстремальные пороги BB%
BB_EXTREME_LOW = 5.0          # BB% < 5% → LONG
BB_EXTREME_HIGH = 95.0        # BB% > 95% → SHORT

# Только Tier A/B
MEAN_TIERS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT',
    'ADAUSDT', 'DOTUSDT', 'LTCUSDT', 'XRPUSDT', 'UNIUSDT',
    'NEARUSDT', 'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT',
    'ENAUSDT', 'ATOMUSDT', 'ALGOUSDT', 'FETUSDT', 'RUNEUSDT',
}

# ONE_WAY (только LONG для этих)
ONE_WAY = {'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT',
           'AVAXUSDT', 'APTUSDT', 'SUIUSDT'}


def _load_state():
    try:
        if os.path.exists(MEAN_STATE_FILE):
            with open(MEAN_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(MEAN_STATE_FILE), exist_ok=True)
    with open(MEAN_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _get_daily_bb(sym):
    """Получить Daily BB: (lower, middle, upper, bb_pct)."""
    try:
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=20')
        closes = [float(c[4]) for c in data.get('result', {}).get('list', [])]
        if len(closes) < 5:
            return None
        sma = sum(closes) / len(closes)
        variance = sum((c - sma) ** 2 for c in closes) / len(closes)
        std = math.sqrt(variance)
        lower = sma - 2 * std
        upper = sma + 2 * std
        bb_pct = (closes[0] - lower) / (upper - lower) * 100 if upper != lower else 50
        return (round(lower, 8), round(sma, 8), round(upper, 8), round(bb_pct, 1))
    except Exception:
        return None


def check_mean_revert(positions):
    """Сканирует на экстремальные BB% для Mean Reversion x10."""
    state = _load_state()
    now = time.time()
    alerts = []
    entries = []

    mean_count = sum(1 for p in positions.values()
                      if p.get('leverage') == MEAN_LEVERAGE and p.get('side') in ('Buy', 'Sell'))
    if mean_count >= MAX_MEAN_POSITIONS:
        return [], []

    for sym in MEAN_TIERS:
        if sym in positions:
            continue

        last_entry = state.get(sym, {}).get('last_mean', 0)
        if now - last_entry < MEAN_COOLDOWN:
            continue

        bb_data = _get_daily_bb(sym)
        if not bb_data:
            continue
        lower, middle, upper, bb_pct = bb_data

        direction = None
        side = None
        sl_mult = None

        # LONG: BB% < 5%
        if bb_pct < BB_EXTREME_LOW:
            direction = 'LONG'
            side = 'Buy'
            sl_price = round(lower * (1 - MEAN_SL_PCT), 8)
            tp_price = middle
        # SHORT: BB% > 95% (кроме ONE_WAY)
        elif bb_pct > BB_EXTREME_HIGH and sym not in ONE_WAY:
            direction = 'SHORT'
            side = 'Sell'
            sl_price = round(upper * (1 + MEAN_SL_PCT), 8)
            tp_price = middle
        else:
            continue

        price = lower if direction == 'LONG' else upper
        qty = math.ceil(MEAN_MARGIN * MEAN_LEVERAGE / max(price, 0.0001) * 100) / 100
        if qty <= 0:
            continue

        msg = (f'🔄 MEAN-REVERT {sym} {direction} x{MEAN_LEVERAGE}: '
               f'BB%={bb_pct}% TP={tp_price:.4f}')

        if not _is_duplicate(msg, 'ENTRY'):
            alerts.append(msg)
            entries.append({
                'symbol': sym, 'side': side, 'direction': direction,
                'entry': price, 'sl': sl_price, 'tp': tp_price,
                'qty': qty, 'leverage': MEAN_LEVERAGE, 'margin': MEAN_MARGIN,
                'bb_pct': bb_pct, 'strategy': 'mean_revert',
            })
            state[sym] = {'last_mean': now}

    _save_state(state)
    return alerts, entries


def execute_mean_revert(entry_info):
    """Выполнить mean-revert вход: лимитка + SL/TP."""
    sym = entry_info['symbol']
    side = entry_info['side']

    try:
        lev_body = {'category': 'linear', 'symbol': sym,
                     'buyLeverage': str(MEAN_LEVERAGE),
                     'sellLeverage': str(MEAN_LEVERAGE)}
        bybit('POST', '/v5/position/set-leverage', lev_body)
    except Exception:
        pass

    for idx in (0, 1):
        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear', 'symbol': sym, 'side': side,
                'orderType': 'Limit', 'qty': str(entry_info['qty']),
                'price': str(entry_info['entry']),
                'positionIdx': idx, 'timeInForce': 'GTC',
            })
            if order.get('retCode') == 0:
                log_event(f'🔄 MEAN-REVERT {sym}: лимитка ${entry_info["entry"]:.4f} x{MEAN_LEVERAGE} idx={idx}')
                return True
            elif order.get('retCode') == 10001:
                continue
            else:
                log_event(f'⚠️ MEAN-REVERT {sym}: {order.get("retMsg", "?")}')
                return False
        except Exception as e:
            log_event(f'⚠️ MEAN-REVERT {sym}: исключение — {e}')
            return False
    return False
