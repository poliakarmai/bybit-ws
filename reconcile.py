#!/usr/bin/env python3
"""Сверка денег: Bybit transaction-log ↔ state.db. Этап 1 аудита. ТОЛЬКО ЧТЕНИЕ.

`state.db` открывается через `mode=ro` — записать в него физически нельзя.
Единственный выход — JSON-отчёт и stdout.

Семантика полей Bybit (замер по сырому ответу 2026-10-23):
  TRADE:      change = cashFlow - fee;  fee > 0 = заплатили; feeRate = fee / notional
              cashFlow != 0 → исполнение ЗАКРЫВАЕТ (cashFlow = грязный PnL сделки)
              cashFlow == 0 → исполнение ОТКРЫВАЕТ (только комиссия)
              ⇒ closedPnl (колонка trade_history.pnl) = change закрывающего исполнения
              ⇒ деньги по сделкам = Σ change(TRADE) = Σ pnl - Σ fee(открытие)
  SETTLEMENT: cashFlow == 0, сумма фандинга в change, feeRate = ставка фандинга
  maker/taker: в каждом символе ровно 2 тарифа, младший = maker (0.00036 против
              taker 0.001 — это в точности отношение maker/taker, 4/11).

Запуск:
    python3 reconcile.py --days 30
    python3 reconcile.py --days 30 --json      # JSON в stdout
    python3 reconcile.py --json --offline      # без сети: прочитать сохранённый отчёт
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/openclaw')

WINDOW = 6 * 86400          # лимит биржи 7 дней, 6 — с запасом (retCode 10001)
MAX_PAGES = 500
DB_PATH = os.path.expanduser('~/.local/share/bybit-ws/state.db')
OUT_DIR = os.path.dirname(DB_PATH)


def _f(x) -> float:
    return float(x or 0)


# ── биржа ──────────────────────────────────────────────────────────────

def iter_log(start: int, end: int, fetch=None):
    """Все записи transaction-log за [start, end). Окно ≤6 дней, внутри окна — курсор.

    Окно: лимит биржи 7 дней, больше не даёт (retCode 10001) → берём 6 дней и идём
    назад целыми окнами, чтобы не было щелей между ними. Внутри окна Bybit отдаёт
    максимум `limit` записей на страницу (в активных 6 днях их ~350), поэтому
    страницы добираются по `nextPageCursor`. Дедуп по `id` — на случай стыка окон.

    ⚠ `funding_backfill.py::_iter_window` для этого не годится: он сдвигает границу
    окна до секунды (`int(transactionTime)//1000`) и теряет записи той же секунды с
    ms > 0 — на 30 днях недосчитывалось 2518 контрактов SEIUSDT (проверено 2026-10-23).
    """
    if fetch is None:
        from bybit_ws.api import bybit as _bybit
        fetch = _bybit
    start_ms, cur_end = int(start) * 1000, int(end) * 1000
    seen = set()
    for _ in range(MAX_PAGES):
        cur_start = max(start_ms, cur_end - WINDOW * 1000)
        q = (f'accountType=UNIFIED&category=linear&startTime={cur_start}'
             f'&endTime={cur_end}&limit=50')
        cursor = ''
        while True:
            j = fetch('GET', '/v5/account/transaction-log?' + q
                      + (f'&cursor={cursor}' if cursor else ''))
            if not j or j.get('retCode') != 0:
                raise RuntimeError(f'transaction-log: {j and j.get("retMsg")}')
            res = j.get('result') or {}
            page = res.get('list') or []
            for it in page:
                if it.get('id') in seen:
                    continue
                seen.add(it['id'])
                yield it
            cursor = res.get('nextPageCursor') or ''
            if not cursor or not page:
                break
        if cur_start <= start_ms:
            return
        cur_end = cur_start - 1
    raise RuntimeError('transaction-log: слишком много окон, период не сходится')


def collect(records) -> dict:
    """Агрегат по записям transaction-log. Чистая функция — тестируется офлайн."""
    records = list(records)          # может прийти генератор — вычитываем один раз
    tr = [r for r in records if r.get('type') == 'TRADE']
    st = [r for r in records if r.get('type') == 'SETTLEMENT']

    # тарифная сетка по символу: младший тариф = maker, старший = taker
    rates = {}
    for r in tr:
        rates.setdefault(r.get('symbol') or '', set()).add(_f(r.get('feeRate')))

    out = {'n_trade': len(tr), 'n_open': 0, 'n_close': 0,
           'fee': 0.0, 'fee_open': 0.0, 'fee_close': 0.0,
           'cashflow_trade': 0.0, 'change_trade': 0.0, 'change_open': 0.0,
           'change_close': 0.0, 'n_settle': len(st), 'change_settle': 0.0,
           'maker_n': 0, 'taker_n': 0, 'amb_n': 0,
           'maker_fee': 0.0, 'taker_fee': 0.0, 'symbols': {}}

    for r in tr:
        sym = r.get('symbol') or ''
        fee, cf, chg = _f(r.get('fee')), _f(r.get('cashFlow')), _f(r.get('change'))
        is_open = cf == 0
        s = out['symbols'].setdefault(
            sym, {'n_trade': 0, 'n_open': 0, 'n_close': 0, 'fee': 0.0, 'change': 0.0,
                  'change_close': 0.0})
        s['n_trade'] += 1
        s['fee'] += fee
        s['change'] += chg
        out['fee'] += fee
        out['cashflow_trade'] += cf
        out['change_trade'] += chg
        if is_open:
            out['n_open'] += 1
            out['fee_open'] += fee
            out['change_open'] += chg
            s['n_open'] += 1
        else:
            out['n_close'] += 1
            out['fee_close'] += fee
            out['change_close'] += chg
            s['n_close'] += 1
            s['change_close'] += chg
        rs = rates[sym]
        fr = _f(r.get('feeRate'))
        if len(rs) < 2:
            out['amb_n'] += 1                       # один тариф в символе — maker/taker не различить
        elif fr < max(rs):
            out['maker_n'] += 1
            out['maker_fee'] += fee
        else:
            out['taker_n'] += 1
            out['taker_fee'] += fee

    for r in st:
        out['change_settle'] += _f(r.get('change'))
    return out


# ── БД (read-only) ─────────────────────────────────────────────────────

def read_db(start: int, end: int, path: str = DB_PATH) -> dict:
    """Что БД думает о тех же деньгах. mode=ro: запись невозможна."""
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    dv = conn.execute('PRAGMA data_version').fetchone()[0]
    rows = conn.execute("""
        SELECT symbol, COALESCE(strategy, '(null)'), COALESCE(manual, 0),
               COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(fees), 0),
               SUM(CASE WHEN fees IS NULL OR fees = 0 THEN 1 ELSE 0 END)
        FROM trade_history WHERE closed_at >= ? AND closed_at < ?
        GROUP BY 1, 2, 3""", (start, end)).fetchall()
    fund = conn.execute("""
        SELECT symbol, COUNT(*), COALESCE(SUM(cash_flow), 0)
        FROM funding_log WHERE ts >= ? AND ts < ? GROUP BY 1""", (start, end)).fetchall()
    dv2 = conn.execute('PRAGMA data_version').fetchone()[0]
    total_changes = conn.total_changes
    conn.close()

    d = {'n_trade': 0, 'pnl': 0.0, 'fees': 0.0, 'no_fees': 0,
         'n_fund': 0, 'funding': 0.0,
         'manual': {0: {'n': 0, 'pnl': 0.0, 'fees': 0.0},
                    1: {'n': 0, 'pnl': 0.0, 'fees': 0.0}},
         'symbols': {}, 'strategies': {},
         'ro': {'data_version_before': dv, 'data_version_after': dv2,
                'total_changes': total_changes}}
    for sym, strat, man, n, pnl, fees, nofee in rows:
        man = 1 if man else 0
        d['n_trade'] += n
        d['pnl'] += pnl
        d['fees'] += fees
        d['no_fees'] += nofee or 0
        d['manual'][man]['n'] += n
        d['manual'][man]['pnl'] += pnl
        d['manual'][man]['fees'] += fees
        s = d['symbols'].setdefault(sym, {'n': 0, 'pnl': 0.0, 'fees': 0.0,
                                          'pnl_m0': 0.0, 'pnl_m1': 0.0,
                                          'n_m0': 0, 'n_m1': 0})
        s['n'] += n
        s['pnl'] += pnl
        s['fees'] += fees
        s[f'n_m{man}'] += n
        s[f'pnl_m{man}'] += pnl
        k = (strat, man)
        g = d['strategies'].setdefault(k, {'n': 0, 'pnl': 0.0, 'fees': 0.0})
        g['n'] += n
        g['pnl'] += pnl
        g['fees'] += fees
    for sym, n, cf in fund:
        d['n_fund'] += n
        d['funding'] += cf
        d['symbols'].setdefault(sym, {'n': 0, 'pnl': 0.0, 'fees': 0.0,
                                      'pnl_m0': 0.0, 'pnl_m1': 0.0,
                                      'n_m0': 0, 'n_m1': 0})['funding'] = cf
    return d


# ── отчёт ──────────────────────────────────────────────────────────────

def build(days: int, start: int, end: int, ex: dict, db: dict) -> dict:
    fee_diff = ex['fee'] - db['fees']
    fund_diff = ex['change_settle'] - db['funding']
    # identity: деньги по сделкам = Σ pnl − комиссии открытия (закрывающие уже внутри pnl)
    expect_change = db['pnl'] - ex['fee_open']
    ident_diff = ex['change_trade'] - expect_change
    by_symbol = []
    seen = set(db['symbols']) | set(ex['symbols'])
    for sym in sorted(seen):
        e = ex['symbols'].get(sym, {})
        b = db['symbols'].get(sym, {})
        by_symbol.append({
            'symbol': sym,
            'n_trade_ex': e.get('n_trade', 0),
            'n_trade_db': b.get('n', 0),
            'fee_ex': round(e.get('fee', 0.0), 4),
            'fee_db': round(b.get('fees', 0.0), 4),
            'fee_diff': round(e.get('fee', 0.0) - b.get('fees', 0.0), 4),
            'pnl_ex_close': round(e.get('change_close', 0.0), 4),
            'pnl_ident_diff': round(e.get('change_close', 0.0) - b.get('pnl', 0.0), 4),
            'pnl_db': round(b.get('pnl', 0.0), 4),
            'pnl_db_manual0': round(b.get('pnl_m0', 0.0), 4),
            'pnl_db_manual1': round(b.get('pnl_m1', 0.0), 4),
            'n_db_manual0': b.get('n_m0', 0),
            'n_db_manual1': b.get('n_m1', 0),
        })
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'days': days,
        'window': {'start': start, 'end': end,
                   'start_iso': datetime.fromtimestamp(start, timezone.utc).isoformat(timespec='seconds'),
                   'end_iso': datetime.fromtimestamp(end, timezone.utc).isoformat(timespec='seconds')},
        'exchange': {k: (round(v, 6) if isinstance(v, float) else v)
                     for k, v in ex.items() if k != 'symbols'},
        'db': {'n_trade': db['n_trade'], 'pnl': round(db['pnl'], 6), 'fees': round(db['fees'], 6),
               'no_fees_rows': db['no_fees'], 'n_fund': db['n_fund'],
               'funding': round(db['funding'], 6),
               'manual0': {k: round(v, 6) for k, v in db['manual'][0].items()},
               'manual1': {k: round(v, 6) for k, v in db['manual'][1].items()},
               'read_only': db['ro']},
        'diff': {'fee': round(fee_diff, 6), 'funding': round(fund_diff, 6),
                 'trade_money_expected': round(expect_change, 6),
                 'trade_money_actual': round(ex['change_trade'], 6),
                 'identity': round(ident_diff, 6)},
        'by_symbol': by_symbol,
        'by_strategy': [
            {'strategy': k[0], 'manual': k[1], 'n': v['n'],
             'pnl': round(v['pnl'], 4), 'fees': round(v['fees'], 4)}
            for k, v in sorted(db['strategies'].items())],
    }


def render(r: dict) -> str:
    ex, db, d = r['exchange'], r['db'], r['diff']
    L = []
    w = r['window']
    L.append(f'═══ СВЕРКА bybit ↔ state.db · {r["days"]} дней · '
             f'{w["start_iso"][:10]} → {w["end_iso"][:10]} ═══')
    L.append(f'записей transaction-log: TRADE {ex["n_trade"]} '
             f'(открытие {ex["n_open"]} / закрытие {ex["n_close"]}), '
             f'SETTLEMENT {ex["n_settle"]}')
    L.append('')
    L.append(f'{"ПОТОК":34} {"БИРЖА":>12} {"БД":>12} {"diff":>9}')
    L.append('-' * 70)
    L.append(f'{"комиссии Σ fee (TRADE)":34} {ex["fee"]:12.2f} {db["fees"]:12.2f} {d["fee"]:+9.2f}')
    L.append(f'{"  ├ открывающие":34} {ex["fee_open"]:12.2f} {"—":>12} {"":>9}')
    L.append(f'{"  └ закрывающие":34} {ex["fee_close"]:12.2f} {"—":>12} {"":>9}')
    L.append(f'{"фандинг Σ change (SETTLEMENT)":34} {ex["change_settle"]:12.2f} '
             f'{db["funding"]:12.2f} {d["funding"]:+9.2f}')
    L.append(f'{"деньги по сделкам Σ change":34} {ex["change_trade"]:12.2f} '
             f'{d["trade_money_expected"]:12.2f} {d["identity"]:+9.2f}')
    L.append(f'{"  (БД: Σ pnl − комиссии откр.)":34} {"":>12} {"":>12} {"":>9}')
    L.append(f'{"Σ cashFlow TRADE (грязный PnL)":34} {ex["cashflow_trade"]:12.2f} '
             f'{"—":>12} {"":>9}')
    L.append(f'{"Σ pnl (closedPnl)":34} {"—":>12} {db["pnl"]:12.2f} {"":>9}')
    L.append(f'{"Σ change закрывающих (= pnl)":34} {ex["change_close"]:12.2f} '
             f'{db["pnl"]:12.2f} {ex["change_close"] - db["pnl"]:+9.2f}')
    L.append('')
    n_mt = ex['maker_n'] + ex['taker_n']
    share = 100.0 * ex['maker_n'] / n_mt if n_mt else 0.0
    L.append(f'maker/taker по feeRate: maker {ex["maker_n"]} ({share:.1f}%), '
             f'taker {ex["taker_n"]}, неоднозначно {ex["amb_n"]} · '
             f'комиссия maker ${ex["maker_fee"]:.2f} / taker ${ex["taker_fee"]:.2f}')
    L.append('')
    L.append('── по strategy (биржа стратегий не знает; колонки manual раздельные) ──')
    L.append(f'{"strategy":14} {"manual=0: n / pnl / fees":34} {"manual=1: n / pnl / fees":34}')
    strategies = sorted({s['strategy'] for s in r['by_strategy']})
    for st in strategies:
        cells = []
        for man in (0, 1):
            row = next((s for s in r['by_strategy']
                        if s['strategy'] == st and s['manual'] == man), None)
            cells.append(f'{row["n"]:4} / {row["pnl"]:+9.2f} / {row["fees"]:7.2f}'
                         if row else '   — /         — /       —')
        L.append(f'{st:14} {cells[0]:34} {cells[1]:34}')
    tot = []
    for man in (0, 1):
        g = db[f'manual{man}']
        tot.append(f'{g["n"]:4} / {g["pnl"]:+9.2f} / {g["fees"]:7.2f}')
    L.append(f'{"ИТОГО":14} {tot[0]:34} {tot[1]:34}')
    L.append('')
    L.append('── по symbol (биржа: исполнения в окне · БД: сделки по closed_at) ──')
    L.append(f'{"symbol":13} {"n ex/db":>9} {"fee ex":>8} {"fee db":>8} {"diff":>7} '
             f'{"pnl ex":>9} {"pnl db":>9} {"diff":>8} {"pnl m0":>9} {"pnl m1":>9}')
    for s in sorted(r['by_symbol'],
                    key=lambda x: -max(abs(x['fee_diff']), abs(x.get('pnl_ident_diff', 0.0)))):
        L.append(f'{s["symbol"]:13} {s["n_trade_ex"]:4}/{s["n_trade_db"]:<4} '
                 f'{s["fee_ex"]:8.2f} {s["fee_db"]:8.2f} {s["fee_diff"]:+7.2f} '
                 f'{s.get("pnl_ex_close", 0.0):+9.2f} {s["pnl_db"]:+9.2f} '
                 f'{s.get("pnl_ident_diff", 0.0):+8.2f} '
                 f'{s["pnl_db_manual0"]:+9.2f} {s["pnl_db_manual1"]:+9.2f}')
    big = [s for s in r['by_symbol'] if abs(s['fee_diff']) > 1.0]
    big_pnl = [s for s in r['by_symbol'] if abs(s.get('pnl_ident_diff', 0.0)) > 1.0]
    L.append('')
    if big:
        L.append('⚠ комиссии расходятся > $1 по символам:')
        for s in big:
            L.append(f'   {s["symbol"]:12} биржа ${s["fee_ex"]:.2f} vs БД ${s["fee_db"]:.2f} '
                     f'→ {s["fee_diff"]:+.2f} (исполнений на бирже {s["n_trade_ex"]}, '
                     f'сделок в БД {s["n_trade_db"]})')
    else:
        L.append('комиссии по символам: расхождений > $1 нет')
    if big_pnl:
        L.append('⚠ pnl расходятся > $1 по символам (биржа: Σ change закрытий):')
        for s in big_pnl:
            L.append(f'   {s["symbol"]:12} биржа ${s.get("pnl_ex_close", 0.0):.2f} vs '
                     f'БД ${s["pnl_db"]:.2f} → {s.get("pnl_ident_diff", 0.0):+.2f} '
                     f'(сделок в БД {s["n_trade_db"]})')
    if abs(d['fee']) > 1.0:
        L.append(f'⚠ ИТОГО комиссии: биржа ${ex["fee"]:.2f} vs БД ${db["fees"]:.2f} '
                 f'→ {d["fee"]:+.2f}')
    if abs(d['funding']) > 1.0:
        L.append(f'⚠ ИТОГО фандинг: биржа ${ex["change_settle"]:.2f} vs БД ${db["funding"]:.2f} '
                 f'→ {d["funding"]:+.2f}')
    if abs(d['identity']) > 1.0:
        L.append(f'⚠ identity не сходится: Σ change ${d["trade_money_actual"]:.2f} vs '
                 f'Σ pnl − fee(откр.) ${d["trade_money_expected"]:.2f} '
                 f'→ {d["identity"]:+.2f}')
    L.append('')
    ro = db['read_only']
    ok = ro['data_version_before'] == ro['data_version_after']
    L.append(f'state.db: read-only · PRAGMA data_version '
             f'{ro["data_version_before"]} → {ro["data_version_after"]} '
             f'({"не изменён" if ok else "⚠ ИЗМЕНЁН ДРУГИМ ПРОЦЕССОМ"}), '
             f'total_changes={ro["total_changes"]}')
    if db['no_fees_rows']:
        L.append(f'⚠ в БД {db["no_fees_rows"]} строк без fees — Σ fees занижен на них')
    L.append('границы: комиссия открытия исполнения попадает в окно по своей дате, '
             'а сделка — по closed_at; на краях окна Σ не обязана совпасть.')
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description='Сверка денег Bybit ↔ state.db (read-only)')
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--json', action='store_true', help='JSON в stdout')
    ap.add_argument('--offline', action='store_true',
                    help='без сети: прочитать сохранённый отчёт')
    a = ap.parse_args()
    out_path = os.path.join(OUT_DIR, f'reconcile-{datetime.now(timezone.utc):%Y-%m-%d}-{a.days}d.json')

    if a.offline:
        if not os.path.exists(out_path):
            sys.exit(f'нет сохранённого отчёта: {out_path}')
        rep = json.load(open(out_path))
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else render(rep))
        return

    end = int(datetime.now(timezone.utc).timestamp())
    start = end - a.days * 86400
    ex = collect(iter_log(start, end))
    db = read_db(start, end)
    rep = build(a.days, start, end, ex, db)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else render(rep))
    if not a.json:
        print(f'JSON-отчёт: {out_path}')


if __name__ == '__main__':
    main()
