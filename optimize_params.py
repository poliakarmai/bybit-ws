"""
Фаза 5.2: Авто-подбор параметров стратегии
Grid-search по BB-периоду, порогам входа, SL/TP для каждого тикера.

Использует существующий walk_forward из backtest.py.
Сохраняет оптимальные параметры в per_symbol_config.json.
"""

import json
import math
import os
import sys
import time
from pathlib import Path

from backtest import fetch_klines, calc_bb, walk_forward

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
OPTIMAL_CONFIG = DATA_DIR / "per_symbol_optimal.json"

# ─── Параметры для оптимизации ───

BB_PERIODS = [10, 15, 20, 25, 30]        # BB-период
BB_STD_MULTS = [2.0]                      # std-множитель (оставляем 2)
ENTRY_BB_THRESHOLDS = [15, 20, 25, 30]   # BB% порог входа
ENTRY_DISCOUNTS = [0.95, 0.97, 0.98]     # дисконт от lower
SL_DISCOUNTS = [0.90, 0.93, 0.95]        # SL ниже lower (0.90 = −10%)
TP_MODE = "middle"                        # TP1 = middle, TP2 = upper (оптимально)


def optimize_single(symbol: str, interval: str = "D", candles: int = 200) -> dict:
    """
    Grid-search оптимальных параметров для одного тикера.
    Критерий: max(win_rate × avg_pnl × trades). Баланс качества и количества.
    """
    klines = fetch_klines(symbol, interval, candles)
    if len(klines) < 30:
        return {"error": f"insufficient data: {len(klines)} candles"}

    closes = [k["close"] for k in klines]

    best_score = -999
    best_config = None
    total_combos = len(BB_PERIODS) * len(ENTRY_BB_THRESHOLDS) * len(ENTRY_DISCOUNTS) * len(SL_DISCOUNTS)
    tested = 0

    for period in BB_PERIODS:
        for bb_threshold in ENTRY_BB_THRESHOLDS:
            for entry_disc in ENTRY_DISCOUNTS:
                for sl_disc in SL_DISCOUNTS:
                    tested += 1
                    result = backtest_params(
                        klines, closes, period=period,
                        bb_threshold=bb_threshold,
                        entry_discount=entry_disc,
                        sl_discount=sl_disc,
                    )

                    if result["trades"] < 3:
                        continue  # мало сделок — ненадёжно

                    # Композитный скор: win_rate × avg_pnl × sqrt(trades)
                    score = result["win_rate"] * max(result["avg_pnl"], 0.1) * math.sqrt(result["trades"])

                    if score > best_score:
                        best_score = score
                        best_config = {
                            "symbol": symbol,
                            "period": period,
                            "bb_threshold": bb_threshold,
                            "entry_discount": entry_disc,
                            "sl_discount": sl_disc,
                            **result,
                        }

    if best_config is None:
        return {"error": "no valid config found"}

    print(f"  {symbol}: tested={tested}/{total_combos} best_score={best_score:.1f} "
          f"p={best_config['period']} thr={best_config['bb_threshold']}% "
          f"entry={best_config['entry_discount']} sl={best_config['sl_discount']} "
          f"WR={best_config['win_rate']:.0f}% PnL={best_config['avg_pnl']:.1f}% "
          f"T={best_config['trades']}")

    return best_config


def backtest_params(klines, closes, period=20, bb_threshold=25,
                    entry_discount=0.97, sl_discount=0.93) -> dict:
    """Бэктест с заданными параметрами. Возвращает статистику."""
    trades = []
    in_position = False
    entry_price = 0
    tp1_price = 0
    tp2_price = 0
    sl_price = 0
    entry_idx = 0

    for i in range(period, len(klines)):
        k = klines[i]

        # BB с кастомным периодом
        bb = _calc_bb_period(closes[: i + 1], period)
        if not bb:
            continue

        if not in_position:
            if bb["pos"] < bb_threshold and bb["width"] > 1:
                entry_price = bb["lower"] * entry_discount
                tp1_price = bb["middle"]
                tp2_price = bb["upper"]
                sl_price = bb["lower"] * sl_discount
                in_position = True
                entry_idx = i
        else:
            if k["low"] <= sl_price:
                pnl = (sl_price / entry_price - 1) * 100
                trades.append({"pnl": round(pnl, 2), "outcome": "SL", "bars": i - entry_idx})
                in_position = False
            elif k["high"] >= tp2_price:
                pnl = (tp2_price / entry_price - 1) * 100
                trades.append({"pnl": round(pnl, 2), "outcome": "TP2", "bars": i - entry_idx})
                in_position = False
            elif k["high"] >= tp1_price:
                tp1_pnl = (tp1_price / entry_price - 1) * 100 * 0.2
                found = False
                for j in range(i + 1, len(klines)):
                    kj = klines[j]
                    if kj["low"] <= sl_price:
                        sl_pnl = (sl_price / entry_price - 1) * 100 * 0.8
                        trades.append({"pnl": round(tp1_pnl + sl_pnl, 2), "outcome": "TP1+SL", "bars": j - entry_idx})
                        found = True
                        break
                    if kj["high"] >= tp2_price:
                        tp2_pnl = (tp2_price / entry_price - 1) * 100 * 0.8
                        trades.append({"pnl": round(tp1_pnl + tp2_pnl, 2), "outcome": "TP1+TP2", "bars": j - entry_idx})
                        found = True
                        break
                if not found:
                    trades.append({"pnl": round(tp1_pnl, 2), "outcome": "TP1", "bars": i - entry_idx})
                in_position = False

    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0}

    wins = [t for t in trades if "TP" in t["outcome"]]
    wr = len(wins) / n * 100
    pnls = [t["pnl"] for t in trades]
    avg_pnl = sum(pnls) / n

    return {
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "max_win": round(max(pnls), 2),
        "max_loss": round(min(pnls), 2),
    }


def _calc_bb_period(closes: list[float], period: int = 20) -> dict:
    """BB с кастомным периодом."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return {
        "lower": sma - 2 * std,
        "middle": sma,
        "upper": sma + 2 * std,
        "width": (4 * std / sma * 100) if sma > 0 else 0,
        "pos": ((closes[-1] - (sma - 2 * std)) / (4 * std) * 100) if std > 0 else 50,
    }


def optimize_all(symbols: list[str]) -> dict:
    """Оптимизация для списка тикеров."""
    results = {}
    for i, sym in enumerate(symbols):
        print(f"[{i+1}/{len(symbols)}] {sym}...")
        try:
            r = optimize_single(sym)
            if "error" not in r:
                results[sym] = {
                    "period": r["period"],
                    "bb_threshold": r["bb_threshold"],
                    "entry_discount": r["entry_discount"],
                    "sl_discount": r["sl_discount"],
                    "win_rate": r["win_rate"],
                    "avg_pnl": r["avg_pnl"],
                    "trades": r["trades"],
                }
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
        time.sleep(0.3)  # rate limit

    # Сохраняем
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OPTIMAL_CONFIG, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n✅ Сохранено {len(results)} тикеров → {OPTIMAL_CONFIG}")
    return results


# ─── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Фаза 5.2: Оптимизатор параметров")
    parser.add_argument("symbol", nargs="?", default=None, help="Один тикер или 'all'")
    parser.add_argument("--tf", default="D")
    parser.add_argument("--candles", type=int, default=200)
    args = parser.parse_args()

    if args.symbol is None or args.symbol == "all":
        AUTO_ENTRY_WATCH = [
            'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
            'HYPEUSDT','NEARUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
            'AAVEUSDT','AVAXUSDT','DOTUSDT','INJUSDT','ONDOUSDT','ARBUSDT',
            'ENAUSDT','FETUSDT','APTUSDT','ATOMUSDT','RUNUSDT',
        ]
        results = optimize_all(AUTO_ENTRY_WATCH)  # все 24 тикера
    else:
        r = optimize_single(args.symbol, args.tf, args.candles)
        if "error" in r:
            print(f"❌ {r['error']}", file=sys.stderr)
        else:
            print(json.dumps({k: v for k, v in r.items()
                             if k not in ("trades_detail",)}, indent=2))
