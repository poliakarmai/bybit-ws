#!/usr/bin/env python3
"""Post-hoc: would a "stretch against the trade" gate have improved the real trades?

stretch = move already run against the trade at entry, in daily-ATR units:
  Buy : (day_open - entry_price) / atr14
  Sell: (entry_price - day_open) / atr14
Gate: skip trade if stretch > k.  Caveman-simple: one fetch+cache loop, one stats pass.

  python3 posthoc_stretch.py            # full run
  python3 posthoc_stretch.py --selftest # offline sanity check on synthetic data
"""
import json
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from collections import Counter
import urllib.request

DB = "/home/openclaw/.local/share/bybit-ws/state.db"
CACHE = os.path.expanduser("~/.cache/bybit-ws-daily-kline.json")
API = "https://api.bybit.com/v5/market/kline"
KS = (0.5, 1.0, 1.5, 2.0, 3.0)
DAY = 86_400_000
WARM = 20  # daily candles of history to keep before the earliest entry we need


# ---------- data ----------
def load_trades():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id,symbol,side,entry_price,pnl,entry_at FROM trade_history "
        "WHERE manual=0 AND entry_at IS NOT NULL AND entry_price IS NOT NULL "
        "AND pnl IS NOT NULL ORDER BY entry_at"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.load(r)
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def fetch_daily(symbol, start_ms, end_ms, cache):
    """All daily candles [start,end). Bybit returns newest-first; paginate backwards."""
    have = cache.setdefault(symbol, [])
    # из JSON-кэша open_time приходит строкой — приводим, иначе сравнение падает
    lo = min((int(c[0]) for c in have), default=None)
    hi = max((int(c[0]) for c in have), default=None)
    if lo is not None and hi is not None and lo <= start_ms and hi >= end_ms - DAY:
        return have
    end = end_ms
    for _ in range(20):  # ponytail: 20 pages = ~55y of daily candles, way past need
        url = f"{API}?category=linear&symbol={symbol}&interval=D&limit=1000&end={end}"
        d = _get(url)
        if not d or d.get("retCode") != 0:
            break
        lst = d["result"]["list"]
        if not lst:
            break
        have.extend(lst)
        oldest = int(lst[-1][0])
        if len(lst) < 1000 or oldest <= start_ms:
            break
        end = oldest
        time.sleep(0.15)  # rate limit
    uniq = {c[0]: c for c in have}
    cache[symbol] = sorted(uniq.values(), key=lambda c: int(c[0]))
    return cache[symbol]


def day_features(candles, entry_at):
    """(day_open, atr14) for the daily candle containing entry_at; None if unavailable."""
    ts = entry_at * 1000
    idx = None
    for i, c in enumerate(candles):
        if int(c[0]) - 2 * 3600_000 <= ts < int(c[0]) + DAY - 2 * 3600_000:  # fixed UTC+2 day boundary of Bybit daily kline
            idx = i
            break
    if idx is None or idx < 15:  # first TR needs a previous close
        return None
    o, h, l = float(candles[idx][1]), float(candles[idx][2]), float(candles[idx][3])
    trs = []
    for i in range(idx - 13, idx + 1):
        pc = float(candles[i - 1][4])
        hh, ll = float(candles[i][2]), float(candles[i][3])
        trs.append(max(hh - ll, abs(hh - pc), abs(ll - pc)))
    atr = sum(trs) / 14
    return o, atr


# ---------- stats ----------
def maxdd(pnls):
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def metrics(ts):
    n = len(ts)
    if not n:
        return dict(n=0, wr=0, pnl=0, pf=0, avg=0, dd=0)
    p = [t["pnl"] for t in ts]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    return dict(
        n=n,
        wr=100 * len(wins) / n,
        pnl=sum(p),
        pf=sum(wins) / abs(sum(losses)) if losses and sum(losses) else math.inf,
        avg=sum(p) / n,
        dd=maxdd(p),
    )


def perm_test(a, b, iters=20000, seed=42):
    """Two-sided permutation test on difference of mean PnL between groups a (kept) and b (gated)."""
    if len(a) < 2 or len(b) < 2:
        return 1.0
    obs = abs(statistics.fmean(a) - statistics.fmean(b))
    pool, n_a = a + b, len(a)
    rnd = random.Random(seed)
    hits = 0
    for _ in range(iters):
        rnd.shuffle(pool)
        if abs(statistics.fmean(pool[:n_a]) - statistics.fmean(pool[n_a:])) >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def fmt(m):
    pf = "inf" if m["pf"] == math.inf else f"{m['pf']:.2f}"
    return (f"{m['n']:>4} {m['wr']:>5.1f}% {m['pnl']:>+9.2f} {pf:>6} {m['dd']:>9.2f}")


HDR = "   n    WR      PnL     PF    MaxDD  "


def table(title, trades):
    print(f"\n### {title}")
    print(f"{'k':<5}{HDR}")
    base = metrics(trades)
    print(f"{'—':<5}{fmt(base)}")
    for k in KS:
        kept = [t for t in trades if not (t["stretch"] > k)]
        print(f"{k:<5}{fmt(metrics(kept))}")


# ---------- main ----------
def run():
    trades = load_trades()
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    # fetch only what's needed: WARM days before earliest entry .. today
    start = min(t["entry_at"] for t in trades) * 1000 - WARM * DAY
    end = int(time.time() * 1000)
    symbols = sorted({t["symbol"] for t in trades})
    for s in symbols:
        fetch_daily(s, start, end, cache)
    json.dump(cache, open(CACHE, "w"))

    ok, skipped = [], []
    for t in trades:
        f = day_features(cache.get(t["symbol"], []), t["entry_at"])
        if not f or f[1] <= 0:
            skipped.append(t)
            continue
        dopen, atr = f
        raw = dopen - t["entry_price"] if t["side"] == "Buy" else t["entry_price"] - dopen
        t = dict(t, atr=atr, stretch=raw / atr)
        ok.append(t)

    print(f"DB: {len(trades)} авто-сделок | с данными свечей: {len(ok)} | пропущено: {len(skipped)}")
    if skipped:
        
        print("  причины пропуска:", Counter(s["symbol"] for s in skipped).most_common(8))
    print(f"период: {time.strftime('%Y-%m-%d', time.gmtime(min(t['entry_at'] for t in ok)))}"
          f" .. {time.strftime('%Y-%m-%d', time.gmtime(max(t['entry_at'] for t in ok)))}")
    st = sorted(t["stretch"] for t in ok)
    print(f"stretch (ATR14): медиана {statistics.median(st):+.2f}, "
          f"p25 {st[len(st)//4]:+.2f}, p75 {st[3*len(st)//4]:+.2f}")

    table("ALL", ok)
    for side in ("Buy", "Sell"):
        table(f"side={side}", [t for t in ok if t["side"] == side])
    
    cnt = Counter(t["symbol"] for t in ok)
    for sym, c in cnt.most_common():
        if c >= 20:
            table(f"symbol={sym} ({c})", [t for t in ok if t["symbol"] == sym])

    print("\n### Что гейт режет (ALL)")
    print(f"{'k':<5}{'отсечено':>9}{'из них убыт.':>13}{'из них плюс':>12}{'$/отсечено':>12}{'p(perm)':>9}")
    base = metrics(ok)
    for k in KS:
        gated = [t for t in ok if t["stretch"] > k]
        kept = [t for t in ok if t["stretch"] <= k]
        if not gated:
            print(f"{k:<5}{0:>9}")
            continue
        p = perm_test([t["pnl"] for t in kept], [t["pnl"] for t in gated])
        print(f"{k:<5}{len(gated):>9}{sum(t['pnl']<=0 for t in gated):>13}"
              f"{sum(t['pnl']>0 for t in gated):>12}{sum(t['pnl'] for t in gated):>+12.2f}{p:>9.3f}")
    print(f"база (без гейта): PnL {base['pnl']:+.2f}, средний {base['avg']:+.2f}")


def selftest():
    """Synthetic: gated trades are ALL losers -> gate must strictly help; no gate -> identical."""
    random.seed(0)
    ts = []
    for i in range(200):
        bad = i % 4 == 0
        ts.append(dict(side="Buy", pnl=-5.0 if bad else 1.0, stretch=0.8 if bad else 0.1))
    m0, mk = metrics(ts), metrics([t for t in ts if t["stretch"] <= 0.5])
    assert m0["n"] == 200 and m0["wr"] == 75.0, m0
    assert mk["n"] == 150 and mk["pnl"] > m0["pnl"] and mk["wr"] == 100.0, mk
    assert abs(maxdd([1, 1, -5, 1]) - -5.0) < 1e-9
    # day_features: Bybit daily candles open 00:00 UTC+2
    T0 = 1792195200000 - 14 * DAY
    cs = [[str(T0 + i * DAY), "10", "12", "8", "10", "0", "0"] for i in range(20)]
    o = day_features(cs, (T0 + 5 * DAY + 12 * 3600_000) // 1000)
    assert o is not None and o[0] == 10.0 and abs(o[1] - 4.0) < 1e-9, o
    o2 = day_features(cs, (T0 + 5 * DAY - 3600_000) // 1000)  # 23:00 UTC prev label day = candle 4
    assert o2 is not None and o2[0] == 10.0, o2
    assert day_features(cs, (T0 - 40 * 3600_000) // 1000) is None  # before warm-up
    a, b = [1.0] * 30 + [10.0], [-9.0] * 30  # clear separation
    assert perm_test(a, b) < 0.01
    print("selftest OK")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else run()
