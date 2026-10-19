#!/usr/bin/env python3
"""Бэкфилл fees/leverage/external_id в trade_history из Bybit closed-pnl.

Матчинг по (closed_at, symbol): замер 19.10 даёт 451 совпадение из 466 строк
с расхождением PnL $0.00–$0.04. qty/avgEntry НЕ использовать — DCA ломает.

Обновляются ТОЛЬКО пустые поля. pnl не трогается.

Запуск:
    set -a && . ~/.config/bybit-ws/env && set +a
    python3 backfill_from_bybit.py --from 2026-06-01 --to 2026-10-19
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/home/openclaw')
from bybit_ws.api import bybit  # noqa: E402
from bybit_ws.state_db import StateDB  # noqa: E402

WINDOW_DAYS = 7
MAX_PAGES = 50  # ponytail: 5000 сделок на окно; при переполнении — сузить окно


def fetch_closed(start: int, end: int) -> dict[tuple[int, str], dict]:
    """{(closed_at, symbol): запись closed-pnl} за (start, end]."""
    out: dict[tuple[int, str], dict] = {}
    cur = start
    while cur < end:
        w_end = min(cur + WINDOW_DAYS * 86400, end)
        cursor = ''
        for _ in range(MAX_PAGES):
            q = (f'category=linear&startTime={cur * 1000}&endTime={w_end * 1000}&limit=100')
            if cursor:
                q += f'&cursor={cursor}'
            j = bybit('GET', '/v5/position/closed-pnl?' + q)
            if not j or j.get('retCode') != 0:
                raise RuntimeError(f'closed-pnl: {j and j.get("retMsg")}')
            for it in j['result'].get('list') or []:
                key = (int(it.get('updatedTime') or 0) // 1000, it.get('symbol') or '')
                out.setdefault(key, it)
            cursor = j['result'].get('nextPageCursor') or ''
            if not cursor:
                break
        cur = w_end
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='start', required=True, help='YYYY-MM-DD')
    ap.add_argument('--to', dest='end', required=True, help='YYYY-MM-DD (не включая)')
    a = ap.parse_args()
    ts = lambda s: int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    start, end = ts(a.start), ts(a.end)

    db = StateDB()
    pnl_before = db.conn.execute('SELECT ROUND(SUM(pnl), 2) FROM trade_history').fetchone()[0]

    closed = fetch_closed(start, end)
    rows = db.conn.execute(
        'SELECT id, symbol, closed_at FROM trade_history WHERE closed_at >= ? AND closed_at < ?',
        (start, end)).fetchall()

    matched, updated, missing = 0, 0, []
    for rid, sym, closed_at in rows:
        item = closed.get((closed_at, sym))
        if not item:
            missing.append((sym, datetime.fromtimestamp(closed_at).strftime('%Y-%m-%d %H:%M')))
            continue
        matched += 1
        fees = float(item.get('openFee') or 0) + float(item.get('closeFee') or 0)
        try:
            lev = int(float(item.get('leverage') or 0)) or None
        except (TypeError, ValueError):
            lev = None
        ext = str(item.get('orderId') or '') or None
        cur = db.conn.execute(
            'UPDATE trade_history SET fees=?, leverage=?, external_id=? '
            'WHERE id=? AND (fees IS NULL OR fees=0)', (fees, lev, ext, rid))
        updated += cur.rowcount
    db.conn.commit()

    filled = db.conn.execute('SELECT COUNT(*) FROM trade_history WHERE fees > 0').fetchone()[0]
    total = db.conn.execute('SELECT COUNT(*) FROM trade_history').fetchone()[0]
    pnl_after = db.conn.execute('SELECT ROUND(SUM(pnl), 2) FROM trade_history').fetchone()[0]

    print(f'сматчено {matched}, обновлено {updated}, не нашлось {len(missing)}')
    for sym, when in missing:
        print(f'  нет на бирже: {sym:12} {when}')
    print(f'fees>0: {filled}/{total} строк')
    print(f'Σ pnl до {pnl_before} / после {pnl_after} '
          f'({"не изменился" if pnl_before == pnl_after else "ИЗМЕНИЛСЯ — БАГ"})')
    assert pnl_before == pnl_after, 'pnl изменился — бэкфилл не должен его трогать'


if __name__ == '__main__':
    main()
