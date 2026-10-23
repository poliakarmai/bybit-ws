#!/usr/bin/env python3
"""Бэкфилл фандинга из Bybit transaction-log в funding_log.

Замер сырого ответа (2026-10-19): фандинг приходит записями
`type='SETTLEMENT'`, сумма — в полях `funding` == `change`, а `cashFlow` у
таких записей всегда '0'. Поэтому фандинг берём из `change`, а cashFlow
TRADE (для сверки) — из `cashFlow`.

Пагинация: не своя. Окна по 6 дней (лимит биржи 7, иначе retCode 10001) и
страницы по `nextPageCursor` — берётся из `reconcile.iter_log`. Прежний сдвиг
окна до секунды терял записи с ms > 0 (замер 23.10: 19 записей на 6-дневном
окне, 290 против 309 уникальных), из-за чего `funding_log` был недобран.

Запуск:
    set -a && . ~/.config/bybit-ws/env && set +a
    python3 funding_backfill.py --from 2026-06-01 --to 2026-10-19
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/openclaw')
from bybit_ws.state_db import StateDB  # noqa: E402

def _iter_window(start: int, end: int, fetch=None):
    """Все записи transaction-log за [start, end). Одна реализация пагинации на проект —
    `reconcile.iter_log` (6-дневные окна + nextPageCursor + дедуп по id)."""
    from reconcile import iter_log
    yield from iter_log(start, end, fetch=fetch)


def fetch_funding(start: int, end: int):
    """Записи SETTLEMENT за (start, end]. Возвращает строки для funding_log."""
    rows = []
    for it in _iter_window(start, end):
        if it.get('type') != 'SETTLEMENT':
            continue
        cf = float(it.get('change') or 0)
        if cf == 0:
            continue
        rows.append((
            it.get('symbol') or '', cf,
            float(it['feeRate']) if it.get('feeRate') else None,
            int(it.get('transactionTime') or 0) // 1000,
            it.get('id'),
        ))
    return rows


def upsert(db: StateDB, rows) -> tuple[int, int]:
    """INSERT OR IGNORE по external_id. Возвращает (новых уникальных, обработано)."""
    before = db.conn.total_changes
    db.conn.executemany(
        'INSERT OR IGNORE INTO funding_log (symbol, cash_flow, fee_rate, ts, external_id) '
        'VALUES (?, ?, ?, ?, ?)', rows)
    db.conn.commit()
    return db.conn.total_changes - before, len(rows)


def cashflow_trade(start: int, end: int) -> float:
    """Σ cashFlow записей типа TRADE (для сверки)."""
    return sum(float(it.get('cashFlow') or 0) for it in _iter_window(start, end)
               if it.get('type') == 'TRADE')


def _sum(db, sql: str, *params):
    cnt, total = db.conn.execute(sql, params).fetchone()
    return cnt, total or 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='start', required=True, help='YYYY-MM-DD')
    ap.add_argument('--to', dest='end', required=True, help='YYYY-MM-DD (не включая)')
    a = ap.parse_args()
    ts = lambda s: int(datetime.strptime(s, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    start, end = ts(a.start), ts(a.end)

    db = StateDB()
    rows = fetch_funding(start, end)
    inserted, seen = upsert(db, rows)
    cnt, total = _sum(db, 'SELECT COUNT(*), ROUND(SUM(cash_flow), 2) FROM funding_log '
                          'WHERE ts >= ? AND ts < ?', start, end)
    print(f'вставлено {inserted}, пропущено {seen - inserted}, сумма ${total:.2f} '
          f'(в funding_log {cnt} записей за период)')

    # ── Арифметика сверки ──
    n_pnl, pnl = _sum(db, 'SELECT COUNT(*), ROUND(SUM(pnl), 2) FROM trade_history '
                          'WHERE closed_at >= ? AND closed_at < ?', start, end)
    n_fee, fees = _sum(db, 'SELECT COUNT(*), ROUND(SUM(fees), 2) FROM trade_history '
                           'WHERE fees > 0 AND closed_at >= ? AND closed_at < ?', start, end)
    n_ext, _ = _sum(db, 'SELECT COUNT(*), SUM(fees) FROM trade_history '
                        'WHERE external_id IS NOT NULL AND closed_at >= ? AND closed_at < ?',
                    start, end)
    cf_trade = cashflow_trade(start, end)
    print(f'Σ closedPnl ({n_pnl} строк trade_history) = ${pnl:.2f}')
    print(f'Σ funding    ({cnt} записей)             = ${total:.2f}')
    print(f'Σ fees из БД ({n_fee} строк с fees>0)      = ${fees:.2f}')
    print(f'Σ cashFlow TRADE (биржа)          = ${cf_trade:.2f}')
    print(f'сверка: closedPnl + funding = ${pnl + total:.2f}  vs  '
          f'cashFlow TRADE = ${cf_trade:.2f}  → разница ${pnl + total - cf_trade:+.2f}')
    print(f'        fees<0 (комиссия закрытия) уже внутри closedPnl; комиссия открытия '
          f'не в closedPnl и не в funding_log (видна только в TRADE-записях).')
    if n_ext < n_pnl:
        print(f'        ⚠ не сходится по существу: fees/external_id заполнены лишь у {n_ext} '
              f'из {n_pnl} строк — это Задача 2 (backfill_from_bybit.py).')


if __name__ == '__main__':
    main()
