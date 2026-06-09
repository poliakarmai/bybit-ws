"""SL Re-entry — лесенка лимиток после стоп-лосса.

v2: режимы simple (DESIGN.md: один re-entry на Lower BB) и ladder (3 уровня).
Конфигурируемые параметры (фикс код-ревью Manus AI).
"""

import json, math, os, time
from datetime import datetime

from .api import bybit, get_bb_data
from .alerts import log_event, add_alert, _is_duplicate
from .config import Config
from .position_sizing import margin_for_strategy

SL_REENTRY_FILE = os.path.expanduser('~/.local/share/bybit-ws/sl_reentry.json')


def _get_reentry_config(cfg):
    """Параметры SL re-entry из конфига (стратегия + tiers)."""
    re = getattr(cfg.strategy, 'reentry', None)
    mode = getattr(re, 'mode', 'ladder') if re is not None else 'ladder'
    return {
        'mode': mode,
        'levels': (
            [float(x) for x in re.get('levels', [0.95, 0.90, 0.85])]
            if re is not None and hasattr(re, 'get') else [0.95, 0.90, 0.85]
        ),
        'margin': float(re.get('margin', 10)) if re is not None and hasattr(re, 'get') else 10,
        'leverage': int(re.get('leverage', 3)) if re is not None and hasattr(re, 'get') else 3,
        'cooldown': int(re.get('cooldown', 14400)) if re is not None and hasattr(re, 'get') else 14400,
        'max_reentries': int(re.get('max_reentries', 2)) if re is not None and hasattr(re, 'get') else 2,
        'tier_ab': set(cfg.tiers.A) | set(cfg.tiers.B),
        'one_way': set(getattr(cfg.tiers, 'one_way', [])),
    }


def _load_state():
    try:
        if os.path.exists(SL_REENTRY_FILE):
            with open(SL_REENTRY_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(SL_REENTRY_FILE), exist_ok=True)
    with open(SL_REENTRY_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def notify_sl_hit(symbol, sl_price, entry_price):
    """Вызывается из main.py когда позиция закрыта по SL."""
    cfg = Config()
    rc = _get_reentry_config(cfg)
    state = _load_state()
    now = time.time()

    if symbol in state:
        last_ts = state[symbol].get('last_reentry_ts', 0)
        count = state[symbol].get('reentry_count', 0)
        if now - last_ts < rc['cooldown']:
            log_event(f'🔕 SL re-entry: {symbol} кулдаун')
            return
        if count >= rc['max_reentries']:
            log_event(f'🔕 SL re-entry: {symbol} исчерпан лимит')
            return

    state[symbol] = {
        'sl_price': float(sl_price),
        'entry_price': float(entry_price),
        'sl_time': now,
        'sl_time_str': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pending': True,
        'last_reentry_ts': state.get(symbol, {}).get('last_reentry_ts', 0),
        'reentry_count': state.get(symbol, {}).get('reentry_count', 0),
    }
    _save_state(state)
    log_event(f'📌 SL re-entry: {symbol} в очереди (SL=${sl_price:.4f})')


def check_sl_reentry(positions, correlation_stop=False):
    """Проверить очередь и выставить лимитки."""
    if correlation_stop:
        if not _is_duplicate('SL re-entry blocked by correlation', 'STOP'):
            log_event('🔕 SL re-entry: correlation_stop активен')
        return []

    state = _load_state()
    if not state:
        return []

    cfg = Config()
    rc = _get_reentry_config(cfg)
    actions = []
    now = time.time()
    active_syms = set(positions.keys()) if isinstance(positions, dict) else set()

    try:
        banned = set(cfg.risk.get('banned_symbols', []))
    except Exception:
        banned = set()

    for sym, info in list(state.items()):
        if sym in banned:
            info['pending'] = False
            state[sym] = info
            continue
        if not info.get('pending'):
            continue
        if sym in active_syms:
            info['pending'] = False
            state[sym] = info
            continue

        sl_price = info['sl_price']
        entry_price = info['entry_price']

        try:
            ticker_data = bybit('GET', f'/v5/market/tickers?category=linear&symbol={sym}')
            tickers = ticker_data.get('result', {}).get('list', [])
            if not tickers:
                continue
            current = float(tickers[0].get('lastPrice', 0))
        except Exception as e:
            log_event(f'⚠️ SL re-entry {sym}: ошибка тикера — {e}')
            continue

        if current <= 0:
            continue

        if current > sl_price * 1.02:
            info['pending'] = False
            state[sym] = info
            log_event(f'🔕 SL re-entry: {sym} отскочила (${current:.4f} > SL ${sl_price:.4f})')
            continue

        drop_from_entry = (1 - current / entry_price) * 100 if entry_price else 0
        qty_step = _get_lot_step(sym)
        orders_placed = 0

        if rc['mode'] == 'simple':
            # DESIGN.md: один re-entry на Lower BB Daily, маржа x0.5 от предыдущей
            bb = get_bb_data(sym, 'D')
            price = bb['lower'] if bb and bb.get('lower', 0) > 0 else current * 0.95
            price = round(price, 4)
            price = _round_to_tick(price, sym)
            margin = margin_for_strategy('reentry', score=6.5)
            if margin > 0 and price < current:
                usdt_qty = margin * rc['leverage']
                qty = round(usdt_qty / price / qty_step) * qty_step
                if qty > 0:
                    for pos_idx in (0, 1):
                        order = bybit('POST', '/v5/order/create', {
                            'category': 'linear', 'symbol': sym, 'side': 'Buy',
                            'orderType': 'Limit', 'qty': str(qty),
                            'price': str(price), 'positionIdx': pos_idx,
                            'timeInForce': 'GTC',
                        })
                        if order.get('retCode') == 0:
                            orders_placed += 1
                            log_event(f'📌 SL re-entry {sym}: simple @ ${price:.4f} ×{qty}')
                            break
                        elif order.get('retCode') == 10001:
                            continue
                        else:
                            break
        else:
            # Ladder-режим: уровни от SL-цены
            for level_pct in rc['levels']:
                price = round(sl_price * level_pct, 4)
                if price < 0.0001:
                    continue
                price = _round_to_tick(price, sym)
                margin = margin_for_strategy('reentry', score=6.5)
                if margin <= 0:
                    continue
                usdt_qty = margin * rc['leverage']
                qty = round(usdt_qty / price / qty_step) * qty_step
                qty = round(qty, _get_precision(qty_step))
                if qty <= 0 or price >= current * 0.995:
                    continue

                for pos_idx in (0, 1):
                    order = bybit('POST', '/v5/order/create', {
                        'category': 'linear', 'symbol': sym, 'side': 'Buy',
                        'orderType': 'Limit', 'qty': str(qty),
                        'price': str(price), 'positionIdx': pos_idx,
                        'timeInForce': 'GTC',
                    })
                    if order.get('retCode') == 0:
                        orders_placed += 1
                        log_event(f'📌 SL re-entry {sym}: лимитка ${price:.4f} ×{qty} (ур.{rc["levels"].index(level_pct)+1}/{len(rc["levels"])}, idx={pos_idx})')
                        break
                    elif order.get('retCode') == 10001:
                        continue
                    else:
                        log_event(f'⚠️ SL re-entry {sym}: ошибка ${price:.4f} — {order.get("retMsg","?")}')
                        break

        if orders_placed > 0:
            add_alert('ENTRY', f'📌 SL re-entry {sym}: {orders_placed} лимиток ниже SL (падение {drop_from_entry:.0f}% от входа)')
            info['pending'] = False
            info['last_reentry_ts'] = now
            info['reentry_count'] = info.get('reentry_count', 0) + 1
            state[sym] = info
            actions.append(sym)
        else:
            log_event(f'⏳ SL re-entry {sym}: не удалось поставить лимитки, ждём')

    _save_state(state)
    return actions


def _get_lot_step(sym):
    try:
        data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
        instruments = data.get('result', {}).get('list', [])
        if instruments:
            return float(instruments[0].get('lotSizeFilter', {}).get('qtyStep', 0.1))
    except Exception:
        pass
    return 0.1


def _round_to_tick(price, sym):
    tick_size = 0.01
    if price < 1:
        tick_size = 0.0001
    elif price < 10:
        tick_size = 0.001
    elif price < 100:
        tick_size = 0.01
    elif price < 1000:
        tick_size = 0.1
    else:
        tick_size = 1
    return round(price / tick_size) * tick_size


def _get_precision(step):
    if step >= 1:
        return 0
    s = str(step)
    if '.' in s:
        return len(s.split('.')[1])
    return 0
