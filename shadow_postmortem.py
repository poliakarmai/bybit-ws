"""Post-mortem shadow-кандидатов: исход (SL/TP/open) + корреляция с L2 imbalance.

Читает ~/.local/share/bybit-ws/shadow_candidates.jsonl. Для каждого кандидата
запрашивает 1h-свечи с момента входа, определяет исход шорта:
  - TP: min_low <= Middle BB (SMA20 Daily)
  - SL: max_high >= entry * 1.10 (Tier A/B SL +10%)
  - open: ни то, ни другое
Затем — сводка imbalance по исходам (для rule-based Execution Gate).

Запуск: python3 shadow_postmortem.py
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.expanduser("~/bybit-ws"))
from bybit_ws.api import bybit

DATA_DIR = os.path.expanduser("~/.local/share/bybit-ws")
LOG_FILE = os.path.join(DATA_DIR, "shadow_candidates.jsonl")


def get_bb_middle(symbol):
    """Middle BB = SMA20 (Daily)."""
    try:
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit=21')
        if not data or data.get('retCode') != 0:
            return None
        closes = [float(c[4]) for c in data['result']['list']]
        closes.reverse()
        return sum(closes) / len(closes)
    except Exception:
        return None


def get_post_entry_extremes(symbol, entry_ts):
    """(max_high, min_low) по 1h-свечам с момента входа."""
    try:
        start = int(entry_ts * 1000)
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval=60&start={start}&limit=500')
        if not data or data.get('retCode') != 0:
            return None, None
        candles = data['result']['list']
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        return max(highs), min(lows)
    except Exception:
        return None, None


def main():
    if not os.path.exists(LOG_FILE):
        print("shadow_candidates.jsonl пуст — нет данных.")
        return
    candidates = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            candidates.append(json.loads(line))

    results = []
    for c in candidates:
        sym = c['symbol']
        entry = c['entry_price']
        imb = c.get('ob_imbalance')
        bb_pct = c.get('bb_pct')
        ts = c['ts']
        sl = entry * 1.10
        tp = get_bb_middle(sym)
        max_high, min_low = get_post_entry_extremes(sym, ts)
        if max_high is None:
            outcome = 'no_data'
        elif tp and min_low <= tp:
            outcome = 'TP'
        elif max_high >= sl:
            outcome = 'SL'
        else:
            outcome = 'open'
        results.append({
            'symbol': sym, 'entry': entry, 'bb_pct': bb_pct, 'imb': imb,
            'sl': round(sl, 6), 'tp': round(tp, 6) if tp else None,
            'outcome': outcome,
        })

    print(f"Всего кандидатов: {len(results)}\n")
    for r in results:
        imb_s = f"{r['imb']:+.4f}" if r['imb'] is not None else "n/a"
        print(f"{r['symbol']:<12} entry={r['entry']:<10} bb={r['bb_pct']}% imb={imb_s} → {r['outcome']}")
    print()

    by_outcome = defaultdict(list)
    for r in results:
        if r['imb'] is not None:
            by_outcome[r['outcome']].append(r['imb'])
    print("=== L2 imbalance по исходам ===")
    for outcome in sorted(by_outcome):
        imbs = by_outcome[outcome]
        if imbs:
            avg = sum(imbs) / len(imbs)
            print(f"{outcome:<8} n={len(imbs)} avg_imb={avg:+.4f} range=[{min(imbs):+.4f}, {max(imbs):+.4f}]")


if __name__ == '__main__':
    main()
