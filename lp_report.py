#!/usr/bin/env python3
"""LP-отчёт для инвесторов фонда.

Из state.db (trade_history) собирает performance-сводку:
реализованный PnL, WR, PF, Sharpe, max drawdown, daily PnL,
честная разбивка по strategy (auto / historical / imported / legacy / x10)
и «чистый авто» (strategy=auto И manual=0 — то, что реально торгует бот).

Текущий NAV/equity — из RPC /balance (токен читается из kv_store).

Запуск:
    python3 lp_report.py              # весь период
    python3 lp_report.py --days 30    # последние 30 дней
    python3 lp_report.py --nav 164.89 # подставить equity вручную
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


def _load_balance() -> float:
    """Текущий equity из RPC /balance (Bearer-токен из kv_store)."""
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
    q = ("SELECT pnl, fees, closed_at, hold_hours, manual, strategy FROM trade_history "
         "WHERE closed_at IS NOT NULL AND pnl IS NOT NULL")
    if days:
        q += f" AND closed_at > strftime('%s','now') - {days * 86400}"
    q += " ORDER BY closed_at"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows


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


def report(days: int | None, nav: float | None) -> str:
    rows = fetch_trades(days)
    if not rows:
        return "Нет закрытых сделок за период."

    pnls = [r[0] for r in rows]
    fees = sum(r[1] or 0 for r in rows)
    holds = [r[3] for r in rows if r[3] is not None]

    total = _stats(pnls)

    # «чистый авто»: strategy=auto И manual=0
    clean_auto = [r[0] for r in rows if (r[5] or "") == "auto" and r[4] == 0]
    clean = _stats(clean_auto)

    # разбивка по strategy
    by_strat: dict[str, list[float]] = {}
    for pnl, f, ts, h, m, s in rows:
        by_strat.setdefault(s or "legacy", []).append(pnl)

    # daily PnL
    daily: dict[str, float] = {}
    for pnl, f, ts, h, m, s in rows:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0.0) + pnl

    mdd = _max_drawdown(daily)
    sharpe = _sharpe(list(daily.values()))
    avg_hold = statistics.mean(holds) if holds else 0.0

    if nav is None:
        nav = _load_balance()

    out = []
    out.append("# LP-отчёт — bybit-ws")
    out.append(f"Период: {'весь' if not days else f'последние {days} дней'}")
    out.append(f"Сгенерировано: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if nav:
        out.append(f"Текущий NAV (equity): **${nav:.2f}**")
    out.append("")
    out.append("## Итог по счёту")
    out.append(f"- Закрытых сделок: **{total['n']}**")
    out.append(f"- Реализованный PnL: **${total['pnl']:+.2f}** (комиссии ${fees:.2f})")
    out.append(f"- Win rate: **{total['wr']:.1f}%** · Profit factor: **{total['pf']:.2f}**")
    out.append(f"- Средний холд: {avg_hold:.1f} ч")
    out.append("")
    out.append("## Чистый авто-бот (strategy=auto, manual=0)")
    out.append(f"- {clean['n']} сделок · PnL **${clean['pnl']:+.2f}** · WR {clean['wr']:.1f}% · PF {clean['pf']:.2f}")
    out.append("")
    out.append("## Разбивка по strategy")
    out.append("| strategy | сделок | PnL |")
    out.append("|---|---|---|")
    for s in sorted(by_strat, key=lambda s: sum(by_strat[s])):
        p = sum(by_strat[s])
        out.append(f"| {s} | {len(by_strat[s])} | ${p:+.2f} |")
    out.append("")
    out.append("## Risk")
    out.append(f"- Max drawdown (по daily PnL): **${mdd:.2f}**")
    out.append(f"- Sharpe (annualized, daily): **{sharpe:.2f}**")
    out.append(f"- Лучший/худший день: ${max(daily.values()):+.2f} / ${min(daily.values()):+.2f}")
    out.append("")
    out.append("## Daily PnL (последние 14 дней)")
    out.append("```")
    mx = max(abs(v) for v in daily.values()) or 1
    for d in sorted(daily)[-14:]:
        bar = "█" * max(1, int(abs(daily[d]) / mx * 20))
        sign = "+" if daily[d] >= 0 else "-"
        out.append(f"{d}  {daily[d]:+8.2f}  {sign}{bar}")
    out.append("```")
    return "\n".join(out)


_CSS = """
@page { size: A4; margin: 2cm; }
body { font-family: 'DejaVu Sans', sans-serif; color: #1a1a1a; line-height: 1.5; }
h1 { color: #0b2545; border-bottom: 2px solid #0b2545; padding-bottom: 6px; }
h2 { color: #13315c; margin-top: 1.4em; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
th, td { border: 1px solid #ccc; padding: 6px 10px; text-align: left; }
th { background: #f0f4f8; }
pre { background: #f8f9fa; border: 1px solid #e0e0e0; padding: 10px; font-size: 11px; white-space: pre-wrap; }
"""


def md_to_pdf(md_text: str, out_path: str) -> str:
    """Markdown → PDF (markdown → HTML → weasyprint)."""
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
