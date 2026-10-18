"""Авто-снятие просроченных сеток и очистка мусорных ордеров."""
from . import ORDERS_METADATA, GRID_TIMEOUTS
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

def report_expired_orders(orders_now, now_ts):
    """Лимитки старше GRID_TIMEOUTS — только АЛЕРТ, без отмены.

    Авто-отмена небезопасна: Bybit отдаёт `createType='CreateByUser'` и для
    ордеров бота (замер 18.10: 25 TP-лимиток бота — CreateByUser), поэтому
    ручные лимитки пользователя от ордеров бота этим полем не отличить, а
    `orderLinkId` пишут только dca.py и recycle.py. Уборщик, отменяющий всё
    старше TTL, снёс бы ручные заявки (18.10 на бирже: ZKUSDT $139, GRAMUSDT
    $54+$54, NMRUSDT $12.6, возраст 7ч–11.6д).
    ponytail: alert-only; отмена — когда все места постановки начнут писать
    свой orderLinkId-префикс.

    Дедуп — флагом `expired_alerted` в ORDERS_METADATA: один алерт на ордер.
    """
    meta = load_json(ORDERS_METADATA) or {}
    msgs = []
    for key, o in orders_now.items():
        # LIMIT_ENTRY (Buy без reduce) и OTHER (Sell без reduce) — оба лимитные входы;
        # TP/SL отсекаются выше. Замер 18.10: NMRUSDT Sell-лимитка 11.6д была OTHER.
        if o.get('kind') not in ('LIMIT_ENTRY', 'OTHER'):
            continue
        if o.get('status') not in ('New', 'PartiallyFilled'):
            continue
        try:
            created = float(o.get('createdTime') or 0) / 1000
        except (TypeError, ValueError):
            continue
        if not created:
            continue
        age = now_ts - created
        if age <= get_ttl_for_grid(o):
            continue
        if meta.get(key, {}).get('expired_alerted'):
            continue
        meta.setdefault(key, {})['expired_alerted'] = True
        msgs.append(f'⏰ Лимитка {o["symbol"]} висит {age / 3600:.0f}ч '
                    f'(qty {o["qty"]} @ ${o["price"]}), TTL {get_ttl_for_grid(o) // 3600}ч — снять вручную')
    if msgs:
        save_json(ORDERS_METADATA, meta)
    return msgs


def clean_stale_orders(positions, orders):
    cleaned = []
    active_syms = set(positions.keys())
    for key, o in orders.items():
        if o['kind'] in ('SL', 'TP'):
            if o['symbol'] not in active_syms:
                cancel_order(o['symbol'], o['orderId'])
                cleaned.append(f'🗑️ Мусор {o["kind"]} {o["symbol"]} @ ${o.get("trigger") or o.get("price"):.4f} — позиции нет')
    return cleaned
