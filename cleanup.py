"""Авто-снятие просроченных сеток и очистка мусорных ордеров."""
import os, time
from . import DATA_DIR, ORDERS_METADATA, GRID_TIMEOUTS
from .api import cancel_order
from .alerts import log_event, add_alert
from .snapshot import load_json, save_json

def get_ttl_for_grid(order):
    if order['kind'] == 'LIMIT_ENTRY':
        return GRID_TIMEOUTS['M5']
    return GRID_TIMEOUTS['OTHER']

def check_expired_orders(orders_now, prev_orders_snapshot, now_ts):
    to_cancel = []
    meta = load_json(ORDERS_METADATA)
    for key, o in orders_now.items():
        if o['kind'] != 'LIMIT_ENTRY':
            continue
        if o['status'] not in ('New', 'PartiallyFilled'):
            continue
        created_at = None
        if key in meta and 'first_seen' in meta[key]:
            created_at = meta[key]['first_seen']
        elif o.get('createdTime'):
            try:
                created_at = float(o['createdTime']) / 1000
            except Exception as e:
                log_event(f'⚠️ cleanup: {e}')
        if created_at is None:
            if key not in meta:
                meta[key] = {'first_seen': now_ts}
            continue
        ttl = get_ttl_for_grid(o)
        age = now_ts - created_at
        if age > ttl:
            to_cancel.append((o['symbol'], o['orderId'], key, int(age)))
    save_json(ORDERS_METADATA, meta)
    return to_cancel

def apply_cancel_expired(to_cancel):
    for sym, oid, key, age in to_cancel:
        log_event(f'⏰ Просрочен {sym}/{oid[:8]}: возраст {age//60}м')
        cancel_order(sym, oid)
        meta = load_json(ORDERS_METADATA)
        meta.pop(key, None)
        save_json(ORDERS_METADATA, meta)
        add_alert('INFO', f'🗑️ Авто-снятие: {sym} лимитка не сработала за {age//60}м — отменена')

def clean_stale_orders(positions, orders):
    cleaned = []
    active_syms = set(positions.keys())
    for key, o in orders.items():
        if o['kind'] in ('SL', 'TP'):
            if o['symbol'] not in active_syms:
                cancel_order(o['symbol'], o['orderId'])
                cleaned.append(f'🗑️ Мусор {o["kind"]} {o["symbol"]} @ ${o.get("trigger") or o.get("price"):.4f} — позиции нет')
    return cleaned
