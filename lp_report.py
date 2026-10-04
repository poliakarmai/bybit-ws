#!/usr/bin/env python3
"""LP-отчёт для инвесторов фонда.

Из state.db (trade_history) собирает performance-сводку:
NAV, реализованный PnL, комиссии (оценка), Net PnL, WR, PF, Sharpe,
max drawdown, топ монет, daily PnL, честная разбивка по strategy.

Комиссии: в trade_history.fees не пишутся (всегда 0) — считаем оценочно
как (entry_notional + exit_notional) × taker_rate. Bybit linear taker = 0.055%.

Запуск:
    python3 lp_report.py              # весь период
    python3 lp_report.py --days 30    # последние 30 дней
    python3 lp_report.py --pdf r.pdf  # PDF для инвесторов
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import urllib.request
from datetime import datetime, timezone

DB = os.path.expanduser("~/.local/share/bybit-ws/state.db")
RPC = "http://127.0.0.1:8766/balance"
TAKER_RATE = 0.00055  # Bybit linear USDT taker fee


def _load_balance() -> float:
    try:
        tok = sqlite3.connect(DB).execute(
            "SELECT value FROM kv_store WHERE key='rpc_auth_token'").fetchone()
        tok = tok[0] if tok else ""
        req = urllib.request.Request(RPC, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode())
        return float(j.get("equity") or j.get("balance") or 0.0)
    except Exception:
        return 0.0


def fetch_trades(days: int | None):
    conn = sqlite3.connect(DB)
    q = ("SELECT symbol, side, entry_price, exit_price, size, pnl, closed_at, "
         "hold_hours, manual, strategy FROM trade_history "
         "WHERE closed_at IS NOT NULL AND pnl IS NOT NULL")
    if days:
        q += f" AND closed_at > strftime('%s','now') - {days * 86400}"
    q += " ORDER BY closed_at"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows


def _stats(pnls: list[float]) -> dict:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "wr": 0.0, "pf": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    return {
        "n": n,
        "pnl": sum(pnls),
        "wr": len(wins) / n * 100,
        "pf": gp / gl if gl > 0 else float("inf"),
    }


def _max_drawdown(daily: dict[str, float]) -> float:
    cum, peak, mdd = 0.0, 0.0, 0.0
    for d in sorted(daily):
        cum += daily[d]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def _sharpe(daily: list[float]) -> float:
    if len(daily) < 2:
        return 0.0
    sd = statistics.stdev(daily)
    if sd <= 0:
        return 0.0
    return statistics.mean(daily) / sd * math.sqrt(365)


def _sortino(daily: list[float]) -> float:
    """Sortino ratio (downside deviation, target=0)."""
    if len(daily) < 2:
        return 0.0
    downside = [d for d in daily if d < 0]
    if not downside:
        return float("inf")
    dd = math.sqrt(sum(d * d for d in downside) / len(daily))
    if dd <= 0:
        return 0.0
    return statistics.mean(daily) / dd * math.sqrt(365)


def _expectancy(pnls: list[float]) -> float:
    """Матожидание на сделку ($)."""
    return statistics.mean(pnls) if pnls else 0.0


def _fmt_money(v: float) -> str:
    return f"${v:+,.2f}"


def report(days: int | None, nav: float | None) -> str:
    rows = fetch_trades(days)
    if not rows:
        return "Нет закрытых сделок за период."

    pnls = [r[5] for r in rows]
    holds = [r[7] for r in rows if r[7] is not None]

    # комиссии (оценка): (entry+exit) notional × taker
    entry_notional = sum((r[3] or 0) * (r[4] or 0) for r in rows)
    exit_notional = sum((r[2] or 0) * (r[4] or 0) for r in rows)
    fees_est = (entry_notional + exit_notional) * TAKER_RATE
    net_pnl = sum(pnls) - fees_est

    total = _stats(pnls)
    clean_auto = [r[5] for r in rows if (r[9] or "") == "auto" and r[8] == 0]
    clean = _stats(clean_auto)

    # разбивка по strategy
    by_strat: dict[str, list[float]] = {}
    for r in rows:
        by_strat.setdefault(r[9] or "legacy", []).append(r[5])

    # топ монет по символу
    by_sym: dict[str, list[float]] = {}
    for r in rows:
        by_sym.setdefault(r[0], []).append(r[5])
    sym_sorted = sorted(by_sym.items(), key=lambda kv: sum(kv[1]), reverse=True)

    # daily PnL
    daily: dict[str, tuple[float, int]] = {}  # date -> (pnl, count)
    for r in rows:
        d = datetime.fromtimestamp(r[6], tz=timezone.utc).strftime("%Y-%m-%d")
        p, c = daily.get(d, (0.0, 0))
        daily[d] = (p + r[5], c + 1)

    dvals = [v[0] for v in daily.values()]
    mdd = _max_drawdown({d: v[0] for d, v in daily.items()})
    sharpe = _sharpe(dvals)
    sortino = _sortino(dvals)
    annual_ret = statistics.mean(dvals) * 365 if dvals else 0.0
    calmar = annual_ret / abs(mdd) if mdd < 0 else 0.0
    expectancy = _expectancy(pnls)
    avg_hold = statistics.mean(holds) if holds else 0.0

    if nav is None:
        nav = _load_balance()

    O = []
    O.append("# LP-отчёт — bybit-ws")
    O.append(f"Период: {'весь' if not days else f'последние {days} дней'} · "
             f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if nav:
        O.append(f"**NAV (equity): ${nav:,.2f}**")
    O.append("")

    O.append("## Ключевые метрики")
    O.append("| Метрика | Значение |")
    O.append("|---|---|")
    O.append(f"| Реализованный PnL (gross) | {_fmt_money(total['pnl'])} |")
    O.append(f"| Комиссии (оценка, taker 0.055%) | {_fmt_money(-fees_est)} |")
    O.append(f"| **Net PnL** | **{_fmt_money(net_pnl)}** |")
    O.append(f"| Win rate | {total['wr']:.1f}% |")
    O.append(f"| Profit factor | {total['pf']:.2f} |")
    O.append(f"| Sharpe (annualized) | {sharpe:.2f} |")
    O.append(f"| Sortino (annualized) | {sortino:.2f} |")
    O.append(f"| Calmar (ret/MDD) | {calmar:.2f} |")
    O.append(f"| Expectancy (сделка) | {_fmt_money(expectancy)} |")
    O.append(f"| Max drawdown | {_fmt_money(mdd)} |")
    O.append(f"| Средний холд | {avg_hold:.1f} ч |")
    O.append("")

    O.append("## Чистый авто-бот (strategy=auto, manual=0)")
    O.append(f"{clean['n']} сделок · PnL **{_fmt_money(clean['pnl'])}** · "
             f"WR {clean['wr']:.1f}% · PF {clean['pf']:.2f}")
    O.append("")

    O.append("## По стратегии")
    O.append("| strategy | сделок | PnL | WR | PF |")
    O.append("|---|---|---|---|---|")
    for s in sorted(by_strat, key=lambda s: sum(by_strat[s])):
        st = _stats(by_strat[s])
        pf = f"{st['pf']:.2f}" if st["pf"] != float("inf") else "∞"
        O.append(f"| {s} | {st['n']} | {_fmt_money(st['pnl'])} | {st['wr']:.1f}% | {pf} |")
    O.append("")

    O.append("## Топ монет")
    O.append("| symbol | сделок | PnL | WR |")
    O.append("|---|---|---|---|")
    for sym, pn in sym_sorted[:5] + sym_sorted[-5:]:
        st = _stats(pn)
        O.append(f"| {sym} | {st['n']} | {_fmt_money(st['pnl'])} | {st['wr']:.1f}% |")
    O.append("")

    O.append("## Daily PnL (последние 14 дней)")
    O.append("| дата | PnL | сделок |")
    O.append("|---|---|---|")
    for d in sorted(daily)[-14:]:
        p, c = daily[d]
        O.append(f"| {d} | {_fmt_money(p)} | {c} |")
    O.append("")

    return "\n".join(O)


_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: 'DejaVu Sans', sans-serif; color: #1a1a1a; line-height: 1.5; }
h1 { color: #0b2545; border-bottom: 2px solid #0b2545; padding-bottom: 6px; }
h2 { color: #13315c; margin-top: 1.4em; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
th { background: #f0f4f8; }
"""


def md_to_pdf(md_text: str, out_path: str) -> str:
    import markdown
    from weasyprint import HTML
    body = markdown.markdown(md_text, extensions=["tables"])
    html = (f"<html><head><meta charset='utf-8'>"
            f"<style>{_CSS}</style></head><body>{body}</body></html>")
    HTML(string=html).write_pdf(out_path)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--nav", type=float, default=None)
    ap.add_argument("--pdf", type=str, default=None)
    a = ap.parse_args()
    md = report(a.days, a.nav)
    if a.pdf:
        print(f"PDF → {md_to_pdf(md, a.pdf)}")
    else:
        print(md)
