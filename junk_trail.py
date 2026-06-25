"""Trailing TP для JUNK-шортов — фиксация прибыли при движении вниз.

Принцип:
- Профит > 15%: подтягиваем TP на уровень, фиксирующий 70% текущей прибыли
- Профит > 30%: затягиваем до 85% текущей прибыли
- TP-ордер: reduceOnly лимитный Buy (как в auto_short.py)
- Старый TP отменяется, новый ставится ниже — ближе к текущей цене

Вызывается в основном цикле вместе с trailing_sl.
"""

import json
import os
import time

from .api import cancel_order, place_take_profit
from .alerts import log_event, add_alert

TRAIL_STATE_FILE = os.path.expanduser('~/.local/share/bybit-ws/junk_trail.json')


def _load_state():
    try:
        if os.path.exists(TRAIL_STATE_FILE):
            with open(TRAIL_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log_event(f'⚠️ junk_trail: {e}')
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(TRAIL_STATE_FILE), exist_ok=True)
    with open(TRAIL_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _round_to_tick(price):
    """Округление цены до разумного тика."""
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


def trailing_junk_tp(positions, orders):
    """Подтянуть TP для JUNK-шортов с хорошим профитом.

    Возвращает список действий: [(sym, old_tp, new_tp, pnl_pct), ...]
    """
    state = _load_state()
    now = time.time()
    actions = []

    # Собираем TP-ордера для JUNK-позиций
    tp_orders = {}  # sym -> [(orderId, price, qty), ...]
    for key, o in orders.items():
        if o['kind'] == 'TP' and o['side'] == 'Buy' and o['status'] in ('New', 'PartiallyFilled', 'Untriggered'):
            sym = o['symbol']
            if sym not in tp_orders:
                tp_orders[sym] = []
            tp_orders[sym].append((o['orderId'], o['price'], o['qty']))

    for sym, p in positions.items():
        if not isinstance(p, dict):
            continue
        if p.get('side') != 'Sell':
            continue

        entry = float(p.get('entry', 0))
        mark = float(p.get('mark', 0))
        size = float(p.get('size', 0))

        if entry <= 0 or mark <= 0 or size <= 0:
            continue

        # Для шорта: профит когда mark < entry
        pnl_pct = (entry - mark) / entry * 100 if entry > 0 else 0
        if pnl_pct <= 0:
            continue

        # Определяем уровень трейлинга
        trail_level = state.get(sym, {}).get('trail_level', 0)

        new_tp = None
        reason = ''

        if pnl_pct >= 30 and trail_level < 2:
            # Жёсткая фиксация: 85% прибыли
            new_tp = entry - 0.85 * (entry - mark)
            reason = f'фиксация 85%'
            trail_level = 2
        elif pnl_pct >= 15 and trail_level < 1:
            # Первая фиксация: 70% прибыли
            new_tp = entry - 0.70 * (entry - mark)
            reason = f'фиксация 70%'
            trail_level = 1

        if new_tp is None:
            continue

        new_tp = _round_to_tick(new_tp)

        # Проверяем что новый TP реально лучше старого (ниже для шорта)
        existing_tps = tp_orders.get(sym, [])
        if existing_tps:
            lowest_existing = min(tp[1] for tp in existing_tps)
            # Для шорта: чем НИЖЕ TP, тем лучше (ближе к текущей цене)
            if new_tp >= lowest_existing:
                continue

        # Не ставим TP ВЫШЕ mark (бессмысленно)
        if new_tp >= mark:
            continue

        # Отменяем старые TP
        for oid, old_price, old_qty in existing_tps:
            cancel_order(sym, oid)

        # Ставим новый TP
        ok = place_take_profit(sym, p.get('positionIdx', 0), 'Sell', size, new_tp)
        if not ok:
            log_event(f'⚠️ junk_trail TP {sym}: не удалось поставить ${new_tp:.6f}')
            continue

        # Сохраняем стейт
        state[sym] = {
            'trail_level': trail_level,
            'trail_ts': now,
            'pnl_pct': round(pnl_pct, 1),
            'tp_price': new_tp,
        }
        _save_state(state)

        msg = (f'🔒 JUNK-Trail TP {sym}: {reason} при +{pnl_pct:.1f}%, '
               f'вход ${entry:.6f} → TP ${new_tp:.6f} (был ниже)')
        add_alert('TP', msg)
        log_event(msg)
        actions.append((sym, pnl_pct, new_tp, reason))

    return actions
