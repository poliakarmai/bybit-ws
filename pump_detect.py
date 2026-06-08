"""Детектор аномальных пампов (>120% за 24ч) с DCA-шорт стратегией.

v2.0: не только алертит, но и СТАВИТ шорт-ордера.
- Первый вход: сразу при обнаружении пампа (market SHORT, $5, 3x)
- DCA 1: +15% от пика (limit SHORT, $5, 3x)
- DCA 2: +30% от пика (limit SHORT, $5, 3x)
- SL: +7% от входа
- TP: -20% от входа
- Макс 2 пампа-шорта одновременно
- Кулдаун 4 часа на монету
"""

import json
import math
import os
import time

from . import DATA_DIR, BYBIT_CLI
from .api import bybit
from .alerts import log_event, add_alert

PUMP_STATE_FILE = os.path.join(DATA_DIR, 'pumps.json')
PUMP_THRESHOLD = 1.20
DCA_STEP = 0.15
ALERT_COOLDOWN = 3600
MAX_TRACK_AGE = 86400 * 7

# Параметры шорта
PUMP_SHORT_MARGIN = 5.0     # $5 на вход
PUMP_SHORT_LEV = 3
SL_PCT = 0.07               # +7% стоп
TP_PCT = 0.20               # -20% тейк
MAX_PUMP_SHORTS = 2
PUMP_COOLDOWN = 14400       # 4 часа между шортами на монету

ONE_WAY = {'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT', 'AVAXUSDT', 'APTUSDT', 'SUIUSDT'}


def _load_state():
    if os.path.exists(PUMP_STATE_FILE):
        try:
            with open(PUMP_STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_state(state):
    with open(PUMP_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _cleanup_state(state, now):
    return {k: v for k, v in state.items() if now - v.get('first_seen_ts', 0) < MAX_TRACK_AGE}


def _get_lot_step(sym):
    try:
        data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
        instruments = data.get('result', {}).get('list', [])
        if instruments:
            return float(instruments[0].get('lotSizeFilter', {}).get('qtyStep', 0.1))
    except:
        pass
    return 0.1


def _round_to_tick(price, sym):
    if price < 1:
        tick = 0.0001
    elif price < 10:
        tick = 0.001
    elif price < 100:
        tick = 0.01
    elif price < 1000:
        tick = 0.1
    else:
        tick = 1.0
    return round(price / tick) * tick


def _count_active_pump_shorts(positions):
    """Посчитать активные пампа-шорты (помечены в стейте)."""
    state = _load_state()
    state = _cleanup_state(state, time.time())
    return sum(1 for v in state.values() if v.get('short_entry_ts'))


def _place_pump_short(sym, price, level_label, state, peak_price):
    """Поставить один пампа-шорт ордер."""
    now = time.time()

    if sym in ONE_WAY:
        log_event(f'🔕 Pump-SHORT {sym}: one-way, нельзя шортить')
        return False

    usdt_qty = PUMP_SHORT_MARGIN * PUMP_SHORT_LEV
    qty_step = _get_lot_step(sym)
    qty = math.ceil(usdt_qty / price / qty_step) * qty_step
    if qty <= 0:
        return False

    entry = _round_to_tick(price, sym)
    sl_price = _round_to_tick(entry * (1 + SL_PCT), sym)
    tp_price = _round_to_tick(entry * (1 - TP_PCT), sym)

    try:
        order = bybit('POST', '/v5/order/create', {
            'category': 'linear',
            'symbol': sym,
            'side': 'Sell',
            'orderType': 'Market' if level_label == 'init' else 'Limit',
            'qty': str(qty),
            'price': str(entry) if level_label != 'init' else None,
            'positionIdx': 2,
            'timeInForce': 'IOC' if level_label == 'init' else 'GTC',
        })
        if order.get('retCode') != 0:
            log_event(f'⚠️ Pump-SHORT {sym} [{level_label}]: {order.get("retMsg","?")}')
            return False

        # SL
        bybit('POST', '/v5/position/trading-stop', {
            'category': 'linear',
            'symbol': sym,
            'side': 'Buy',
            'positionIdx': 2,
            'orderType': 'Market',
            'qty': str(qty),
            'stopLoss': str(sl_price),
            'slTriggerBy': 'MarkPrice',
        })

        # TP
        bybit('POST', '/v5/order/create', {
            'category': 'linear',
            'symbol': sym,
            'side': 'Buy',
            'orderType': 'Limit',
            'qty': str(qty),
            'price': str(tp_price),
            'positionIdx': 2,
            'timeInForce': 'GTC',
            'reduceOnly': True,
        })

        log_event(f'🚀 Pump-SHORT {sym} [{level_label}]: ${entry:.4f} ×{qty}, '
                  f'SL ${sl_price:.4f}, TP ${tp_price:.4f}')

        if level_label == 'init':
            state[sym]['short_entry_ts'] = now
            state[sym]['short_entry_price'] = entry
        state[sym]['dca_placed'] = state[sym].get('dca_placed', []) + [level_label]
        state[sym]['peak_price'] = max(peak_price, price)
        _save_state(state)
        return True

    except Exception as e:
        log_event(f'⚠️ Pump-SHORT {sym}: исключение — {e}')
        return False


def check_pumps(positions=None):
    """Проверить топ-80 на пампа и поставить шорты. Возвращает список алертов."""
    alerts = []
    now = time.time()
    state = _cleanup_state(_load_state(), now)

    if positions is None:
        positions = {}

    live_syms = set(positions.keys()) if isinstance(positions, dict) else set()
    active_pump_shorts = _count_active_pump_shorts(positions)

    data = bybit('GET', '/v5/market/tickers?category=linear')
    if not data or data.get('retCode') != 0:
        return alerts

    tickers = data['result'].get('list', [])
    if not tickers:
        return alerts

    tickers.sort(key=lambda t: float(t.get('turnover24h', 0) or 0), reverse=True)
    top_tickers = tickers[:80]

    for t in top_tickers:
        sym = t['symbol']
        chg_pct = float(t.get('price24hPcnt', 0) or 0)
        last_price = float(t.get('lastPrice', 0) or 0)

        if chg_pct < PUMP_THRESHOLD:
            continue
        if 'USD' not in sym or not sym.endswith('USDT'):
            continue
        turnover = float(t.get('turnover24h', 0) or 0)
        if turnover < 1_000_000:
            continue

        prev = state.get(sym, {})
        prev_peak = prev.get('peak_price', 0)
        prev_alerts = prev.get('alerts', [])
        last_alert_ts = prev_alerts[-1] if prev_alerts else 0

        if last_price > prev_peak:
            prev['peak_price'] = last_price

        # Первое обнаружение
        if not prev:
            prev = {
                'first_seen_ts': now,
                'first_price': last_price,
                'peak_price': last_price,
                'alerts': [now],
                'dca_level': 0,
                'dca_placed': [],
            }
            alerts.append(
                f'🚀 ПАМП {sym}: +{chg_pct*100:.0f}% за 24ч '
                f'(цена ${last_price:.4f}, оборот ${turnover:,.0f})'
            )

            # Вход #1 — немедленный шорт
            if sym not in live_syms and active_pump_shorts < MAX_PUMP_SHORTS and sym not in ONE_WAY:
                if _place_pump_short(sym, last_price, 'init', prev, last_price):
                    alerts.append(f'🐻 Pump-SHORT {sym}: вход #1 @ ${last_price:.4f}')
                    active_pump_shorts += 1

        # Уже отслеживаем — DCA-уровни
        elif last_price > prev_peak * (1 + DCA_STEP) and now - last_alert_ts > ALERT_COOLDOWN:
            dca_level = prev.get('dca_level', 0) + 1
            prev['dca_level'] = dca_level
            prev['alerts'].append(now)
            level_label = f'dca{dca_level}'

            alerts.append(
                f'🔥 ДОКУПКА SHORT {sym}: +{chg_pct*100:.0f}% за 24ч, '
                f'DCA уровень #{dca_level} @ ${last_price:.4f}'
            )

            # Ставим DCA-шорт если ещё не ставили этот уровень
            if (sym not in live_syms and active_pump_shorts < MAX_PUMP_SHORTS
                    and level_label not in prev.get('dca_placed', [])
                    and sym not in ONE_WAY):
                if _place_pump_short(sym, last_price, level_label, prev, prev_peak):
                    alerts.append(f'🐻 Pump-DCA {sym}: уровень #{dca_level} @ ${last_price:.4f}')
                    active_pump_shorts += 1

        elif chg_pct >= PUMP_THRESHOLD and now - last_alert_ts > ALERT_COOLDOWN:
            prev['alerts'].append(now)
            alerts.append(
                f'📈 ПАМП {sym} продолжается: +{chg_pct*100:.0f}%, '
                f'цена ${last_price:.4f}, ждём DCA'
            )

        state[sym] = prev

    _save_state(state)
    return alerts
