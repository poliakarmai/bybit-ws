"""Атрибуция сделок: ручная или авто — по данным биржи + сигналам входа.

Улики по порядку силы:
  1. orderLinkId ордера открытия: наш префикс (ae_/as_/re_/dca_/bb_/mr_/fe_/pd_)
     → авто; пустой → вход из приложения (ручной факт);
  2. нет истории ордера (Bybit хранит 7 дней) → decide_manual(): плечо вне
     авто-набора {3,10} + отсутствие entry_diag/решения судьи → ручная.

Матчинг ордера к сделке: сторона входа + qty (в пределах 0.5% или 1 контракта) +
ближайшее время. Без qty матч ловил соседние ордера по горячим тикерам.

    python3 scripts_origin_audit.py            # только посчитать
    python3 scripts_origin_audit.py --apply    # записать manual=1/0
"""

import os
import sqlite3
import sys
import time
from collections import Counter

from bybit_ws.api import fetch_order_history
from bybit_ws.manual_positions import AUTO_LINK_PREFIXES, decide_manual

DB = os.path.expanduser('~/.local/share/bybit-ws/state.db')
DAYS = 7


def _match(orders, qty, entry_price, since_ms):
    """Ордер входа = тот, чья цена совпала с entry_price (допуск 0.2%) и qty сходится.

    Сторону НЕ фильтруем: в closed-pnl поле side — сторона закрытия, а в истории
    ордеров это путается с открытием (JCT 27.10: 'Sell' 0.008106 и был входом).
    Цена входа — единственный однозначный якорь, время — тай-брейк.
    """
    cands = []
    for o in orders:
        try:
            oq = float(o.get('qty') or 0)
            op = float(o.get('avgPrice') or o.get('price') or 0)
        except (TypeError, ValueError):
            continue
        if qty and abs(oq - qty) > max(qty * 0.005, 1.0):
            continue
        if entry_price and (not op or abs(op - entry_price) / entry_price > 0.002):
            continue
        cands.append(o)
    if not cands:
        return None
    return min(cands, key=lambda o: abs(int(o.get('createdTime', 0) or 0) - since_ms))


def main(apply=False):
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "select id, symbol, side, pnl, size, entry_price, entry_at, leverage, coalesce(manual,0) manual "
        f"from trade_history where closed_at > strftime('%s','now')-{DAYS}*86400 "
        "and entry_at is not null")]
    verdict = []
    for r in rows:
        since = int(r['entry_at']) * 1000
        entry_side = 'Buy' if r['side'] == 'Sell' else 'Sell'
        orders = fetch_order_history(r['symbol'], since - 120_000, end_ms=since + 900_000)
        o = _match(orders, float(r['size'] or 0), float(r['entry_price'] or 0), since)
        if o is None:
            manual, why = decide_manual(r['symbol'], entry_side, r['entry_at'], r['leverage'])
            src = 'сигналы'
        else:
            link = o.get('orderLinkId') or ''
            manual = not link.startswith(AUTO_LINK_PREFIXES)
            why = [f'link={link or "пуст"}']
            src = 'ордер'
        verdict.append((r, manual, src, why))
        time.sleep(0.08)

    for src in ('ордер', 'сигналы'):
        sub = [(r, m) for r, m, s, _ in verdict if s == src]
        if not sub:
            continue
        print(f'{src}: {len(sub)} сделок, PnL ${sum(r["pnl"] for r, _ in sub):+.2f}, '
              f'ручных {sum(m for _, m in sub)}')
    print('по символам (ручные):',
          dict(Counter(r['symbol'] for r, m, _, _ in verdict if m).most_common(12)))
    mism = [(r, m, why) for r, m, _, why in verdict if bool(r['manual']) != bool(m)]
    print(f'\nрасходится с базой: {len(mism)} — {sum(r["pnl"] for r, _, _ in mism):+.2f}')
    for r, m, why in mism[:15]:
        print(f"  {r['symbol']:10s} pnl {r['pnl']:+8.2f} база manual={r['manual']} → {int(m)}  {why}")
    if not apply:
        print(f'\n[dry-run] изменить {len(mism)} строк. Повторить с --apply.')
        return
    db.executemany("update trade_history set manual=? where id=?",
                   [(int(m), r['id']) for r, m, _, _ in verdict])
    db.commit()
    print(f'\nобновлено: {len(verdict)}')


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
