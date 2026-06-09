"""Funding Rate Momentum x10 — вход при экстремальном фондинге + BB.

Стратегия: экстремальный фондинг притягивает арбитражёров.
Когда ставка >0.1% (лонгисты платят) + цена у Upper BB → SHORT.
Когда ставка <−0.1% (шортисты платят) + цена у Lower BB → LONG.

Плечо x10, маржа $10, удержание до разворота фондинга или TP на Middle BB.
Дополнительный доход: сам фондинг капает каждые 8 часов.
Макс 3 позиции этого типа, кулдаун 4ч на монету.
"""
import json
import math
import os
import time
from datetime import datetime

from .api import bybit
from .alerts import log_event, add_alert, _is_duplicate

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
FUNDING_STATE_FILE = os.path.join(DATA_DIR, 'funding_entry_state.json')

FUNDING_LEVERAGE = 10
FUNDING_MARGIN = 10.0
FUNDING_SL_PCT = 0.04          # 4% SL
MAX_FUNDING_POSITIONS = 3
FUNDING_COOLDOWN = 14400        # 4 часа на монету

# Пороги фондинга
FUNDING_LONG_THRESHOLD = -0.001   # −0.1% → LONG (шортисты платят)
FUNDING_SHORT_THRESHOLD = 0.001   # +0.1% → SHORT (лонгисты платят)

# BB-фильтр: цена должна быть близка к полосе
BB_LONG_MAX = 15.0               # BB% < 15% для LONG (у Lower BB)
BB_SHORT_MIN = 85.0              # BB% > 85% для SHORT (у Upper BB)

# Только Tier A/B + активно торгуемые
FUNDING_TIERS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT',
    'ADAUSDT', 'DOTUSDT', 'LTCUSDT', 'XRPUSDT', 'UNIUSDT',
    'NEARUSDT', 'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT',
    'ENAUSDT', 'ATOMUSDT', 'ALGOUSDT', 'FETUSDT', 'RUNEUSDT',
    'WLDUSDT', 'SUIUSDT',
}

ONE_WAY = {'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT',
           'AVAXUSDT', 'APTUSDT', 'SUIUSDT'}


def _load_state():
    try:
        if os.path.exists(FUNDING_STATE_FILE):
            with open(FUNDING_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(FUNDING_STATE_FILE), exist_ok=True)
    with open(FUNDING_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _get_bb_and_funding(sym):
    """Получить Daily BB% и ставку фондинга."""
    try:
        # BB Daily
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=20')
        closes = [float(c[4]) for c in kline.get('result', {}).get('list', [])]
        if len(closes) < 5:
            return None
        sma = sum(closes) / len(closes)
        variance = sum((c - sma) ** 2 for c in closes) / len(closes)
        std = math.sqrt(variance)
        lower = sma - 2 * std
        upper = sma + 2 * std
        bb_pct = (closes[0] - lower) / (upper - lower) * 100 if upper != lower else 50

        # Funding rate
        ticker = bybit('GET', f'/v5/market/tickers?category=linear&symbol={sym}')
        funding_rate = float(ticker.get('result', {}).get('list', [{}])[0].get('fundingRate', 0))

        return {
            'lower': round(lower, 8),
            'middle': round(sma, 8),
            'upper': round(upper, 8),
            'bb_pct': round(bb_pct, 1),
            'funding_rate': round(funding_rate, 6),
            'price': closes[0],
        }
    except Exception:
        return None


def check_funding_signals(positions):
    """Сканирует на сигналы Funding Rate Momentum."""
    state = _load_state()
    now = time.time()
    alerts = []
    entries = []

    funding_count = sum(1 for p in positions.values()
                         if p.get('leverage') == FUNDING_LEVERAGE and p.get('side') in ('Buy', 'Sell'))
    if funding_count >= MAX_FUNDING_POSITIONS:
        return [], []

    for sym in FUNDING_TIERS:
        if sym in positions:
            continue

        last_entry = state.get(sym, {}).get('last_funding', 0)
        if now - last_entry < FUNDING_COOLDOWN:
            continue

        data = _get_bb_and_funding(sym)
        if not data:
            continue

        direction = None
        side = None
        entry_price = None
        sl_price = None

        # LONG: фондинг отрицательный + цена у Lower BB
        if (data['funding_rate'] < FUNDING_LONG_THRESHOLD and
                data['bb_pct'] < BB_LONG_MAX):
            direction = 'LONG'
            side = 'Buy'
            entry_price = data['price']
            sl_price = round(entry_price * (1 - FUNDING_SL_PCT), 8)
            tp_price = data['middle']
            trigger = f'funding={data["funding_rate"]*100:.2f}% BB%={data["bb_pct"]}%'

        # SHORT: фондинг положительный + цена у Upper BB (не ONE_WAY)
        elif (data['funding_rate'] > FUNDING_SHORT_THRESHOLD and
              data['bb_pct'] > BB_SHORT_MIN and
              sym not in ONE_WAY):
            direction = 'SHORT'
            side = 'Sell'
            entry_price = data['price']
            sl_price = round(entry_price * (1 + FUNDING_SL_PCT), 8)
            tp_price = data['middle']
            trigger = f'funding=+{data["funding_rate"]*100:.2f}% BB%={data["bb_pct"]}%'
        else:
            continue

        qty = math.ceil(FUNDING_MARGIN * FUNDING_LEVERAGE / entry_price * 100) / 100
        if qty <= 0:
            continue

        msg = (f'💰 FUNDING {sym} {direction} x{FUNDING_LEVERAGE}: '
               f'{trigger} TP={tp_price:.4f}')

        if not _is_duplicate(msg, 'ENTRY'):
            alerts.append(msg)
            entries.append({
                'symbol': sym, 'side': side, 'direction': direction,
                'entry': entry_price, 'sl': sl_price, 'tp': tp_price,
                'qty': qty, 'leverage': FUNDING_LEVERAGE, 'margin': FUNDING_MARGIN,
                'funding_rate': data['funding_rate'],
                'strategy': 'funding_momentum',
            })
            state[sym] = {'last_funding': now}

    _save_state(state)
    return alerts, entries


def execute_funding_entry(entry_info):
    """Выполнить funding-вход: market + SL/TP."""
    sym = entry_info['symbol']
    side = entry_info['side']

    try:
        lev_body = {'category': 'linear', 'symbol': sym,
                     'buyLeverage': str(FUNDING_LEVERAGE),
                     'sellLeverage': str(FUNDING_LEVERAGE)}
        bybit('POST', '/v5/position/set-leverage', lev_body)
    except Exception:
        pass

    for idx in (0, 1):
        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear', 'symbol': sym, 'side': side,
                'orderType': 'Market', 'qty': str(entry_info['qty']),
                'positionIdx': idx, 'timeInForce': 'IOC',
            })
            if order.get('retCode') == 0:
                log_event(f'💰 FUNDING {sym} {entry_info["direction"]}: market x{FUNDING_LEVERAGE} '
                          f'funding={entry_info.get("funding_rate", 0)*100:.2f}%')
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
                log_event(f'⚠️ FUNDING {sym}: {order.get("retMsg", "?")}')
                return False
        except Exception as e:
            log_event(f'⚠️ FUNDING {sym}: исключение — {e}')
            return False
    return False
