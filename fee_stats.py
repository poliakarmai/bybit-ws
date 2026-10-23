"""Замер входов: fill rate, maker-доля, комиссии (этап 4 аудита — post-only).

Считает по бирже, а не по БД:
  /v5/order/history    → заявки (статус, TIF, исполненный объём, комиссия)
  /v5/execution/list   → исполнения (isMaker) — открывающие = closedSize == 0

Ключевая метрика: доля maker-исполнений среди ОТКРЫВАЮЩИХ и fill rate лимитных
входов. Ручные сделки (Алексея) попадают в общие числа: их Market-входы не
отличимы от Market-входов других стратегий, поэтому maker-доля по всем входам
считается вместе, а лимитные входы (авто-ветка) — отдельно.

Запуск:
    cd /home/openclaw && set -a && . ~/.config/bybit-ws/env && set +a
    python3 -m bybit_ws.fee_stats --days 30
"""
import argparse
import time

from .api import bybit

WINDOW_MS = 6 * 86400 * 1000  # лимит биржи: 7 дней на окно (order/history и execution/list)


def _paged(path, days, limit=50):
    """Все записи эндпоинта за period; окна по 6 дней + nextPageCursor."""
    now = int(time.time() * 1000)
    rows, end = [], now
    floor = now - days * 86400 * 1000
    while end > floor:
        start = max(end - WINDOW_MS, floor)
        cursor = ''
        while True:
            q = f'{path}?category=linear&startTime={start}&endTime={end}&limit={limit}'
            if cursor:
                q += f'&cursor={cursor}'
            j = bybit('GET', q)
            if not j or j.get('retCode') != 0:
                raise RuntimeError(f'{path}: {j and j.get("retMsg")}')
            lst = j['result'].get('list') or []
            rows += lst
            cursor = j['result'].get('nextPageCursor') or ''
            if not cursor or not lst:
                break
        end = start
    return rows


def is_entry_order(o):
    """Заявка на ОТКРЫТИЕ (не reduceOnly, не SL/TP-условная)."""
    return (o.get('orderType') in ('Limit', 'Market')
            and str(o.get('reduceOnly')).lower() != 'true'
            and not o.get('stopOrderType'))


def summarize(orders, executions):
    """Сводка: fill rate лимитных входов + maker-доля открывающих исполнений."""
    entries = [o for o in orders if is_entry_order(o)]
    limit_entries = [o for o in entries if o.get('orderType') == 'Limit']
    filled = [o for o in limit_entries if o.get('orderStatus') == 'Filled']
    cancelled = [o for o in limit_entries
                 if o.get('orderStatus') in ('Cancelled', 'PartiallyFilledCancelled')]

    opens = [e for e in executions if float(e.get('closedSize') or 0) == 0]
    maker = [e for e in opens if str(e.get('isMaker')).lower() == 'true']

    def fee(rows):
        return sum(float(r.get('cumExecFee') or r.get('execFee') or 0) for r in rows)

    return {
        'orders': len(orders),
        'entry_orders': len(entries),
        'limit_entries': len(limit_entries),
        'limit_filled': len(filled),
        'limit_cancelled': len(cancelled),
        'fill_rate': (100.0 * len(filled) / len(limit_entries)) if limit_entries else 0.0,
        'tif': sorted({o.get('timeInForce') for o in limit_entries}),
        'limit_entry_fee': fee(limit_entries),
        'executions': len(executions),
        'opening_executions': len(opens),
        'maker_executions': len(maker),
        'maker_share': (100.0 * len(maker) / len(opens)) if opens else 0.0,
        'opening_fee': fee(opens),
        'avg_open_fee': (fee(opens) / len(opens)) if opens else 0.0,
    }


def run(days=30):
    s = summarize(_paged('/v5/order/history', days), _paged('/v5/execution/list', days, limit=100))
    print(f'=== входы за {days} дн.')
    print(f"заявок {s['orders']}, из них на открытие {s['entry_orders']} "
          f"(лимитных {s['limit_entries']}, TIF {s['tif']})")
    print(f"лимитные входы: filled {s['limit_filled']}, cancelled {s['limit_cancelled']} "
          f"→ fill rate {s['fill_rate']:.1f}%, комиссия ${s['limit_entry_fee']:.2f}")
    print(f"исполнений {s['executions']}, открывающих {s['opening_executions']}, "
          f"maker {s['maker_executions']} → {s['maker_share']:.1f}%")
    print(f"комиссия открытий ${s['opening_fee']:.2f}, средняя ${s['avg_open_fee']:.4f}")
    return s


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    run(ap.parse_args().days)
