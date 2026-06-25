"""
Backtest v2.0 — walk-forward бэктест Bollinger Grid на исторических данных.

Проходит по историческим свечам, вычисляет BB на каждом шаге,
симулирует вход при BB < 25% и отслеживает исход.

Считает: win rate, avg PnL, max drawdown, Sharpe.
"""

import json
import re
import math
import os
import subprocess
import sys
import time

BYBIT_CLI = os.path.expanduser("~/.local/bin/bybit")


def fetch_klines(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    """Загружает klines через bybit REST API. Возвращает старые→новые."""
    try:
        if not re.fullmatch(r'^[A-Z0-9]+$', symbol):
            raise ValueError(f'Invalid symbol: {symbol}')
        r = subprocess.run(
            [BYBIT_CLI, "raw", "GET",
             f"/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        raw_list = data.get("result", {}).get("list", [])
        if not raw_list:
            return []
        klines = []
        for item in raw_list:
            if not isinstance(item, list) or len(item) < 5:
                continue
            try:
                klines.append({
                    "open": float(item[1]), "high": float(item[2]),
                    "low": float(item[3]), "close": float(item[4]),
                })
            except (ValueError, TypeError):
                continue
        klines.reverse()
        return klines
    except Exception as e:
        print(f"[backtest] fetch_klines error: {e}", file=sys.stderr)
    return []


def calc_bb(closes: list[float]) -> dict:
    """BB(20, 2) на списке закрытий."""
    if len(closes) < 20:
        return None
    window = closes[-20:]
    sma = sum(window) / 20
    variance = sum((x - sma) ** 2 for x in window) / 20
    std = math.sqrt(variance)
    return {
        "lower": sma - 2 * std,
        "middle": sma,
        "upper": sma + 2 * std,
        "width": (4 * std / sma * 100) if sma > 0 else 0,
        "pos": ((closes[-1] - (sma - 2 * std)) / (4 * std) * 100) if std > 0 else 50,
    }


def walk_forward(symbol: str, interval: str = "D", candles: int = 200) -> dict:
    """
    Walk-forward бэктест: проходит по истории, входит при BB < 25%,
    отслеживает исход. Один вход за раз (ждём исхода перед новым).
    """
    klines = fetch_klines(symbol, interval, candles)
    if len(klines) < 30:
        return {"error": f"insufficient data: {len(klines)} candles"}

    trades = []
    in_position = False
    entry_price = 0
    tp1_price = 0
    tp2_price = 0
    sl_price = 0
    entry_idx = 0

    closes = [k["close"] for k in klines]

    for i in range(20, len(klines)):
        k = klines[i]
        bb = calc_bb(closes[: i + 1])
        if not bb:
            continue

        if not in_position:
            # Ищем точку входа: BB < 25%
            if bb["pos"] < 25 and bb["width"] > 1:
                entry_price = bb["lower"] * 0.97  # −3% ниже Lower
                tp1_price = bb["middle"]
                tp2_price = bb["upper"]
                sl_price = bb["lower"] * 0.93  # −7% от Lower
                in_position = True
                entry_idx = i
        else:
            # Проверяем исход
            if k["low"] <= sl_price:
                pnl = (sl_price / entry_price - 1) * 100
                trades.append({"entry": entry_price, "exit": sl_price, "pnl": round(pnl, 2),
                              "outcome": "SL", "bars": i - entry_idx})
                in_position = False
            elif k["high"] >= tp2_price:
                pnl = (tp2_price / entry_price - 1) * 100
                trades.append({"entry": entry_price, "exit": tp2_price, "pnl": round(pnl, 2),
                              "outcome": "TP2", "bars": i - entry_idx})
                in_position = False
            elif k["high"] >= tp1_price:
                # Partial TP: 20% на TP1, остальное к TP2 или SL
                tp1_pnl = (tp1_price / entry_price - 1) * 100 * 0.2
                # Ищем что дальше
                found = False
                for j in range(i + 1, len(klines)):
                    kj = klines[j]
                    if kj["low"] <= sl_price:
                        sl_pnl = (sl_price / entry_price - 1) * 100 * 0.8
                        total = tp1_pnl + sl_pnl
                        trades.append({"entry": entry_price, "exit": sl_price, "pnl": round(total, 2),
                                      "outcome": "TP1+SL", "bars": j - entry_idx})
                        found = True
                        break
                    if kj["high"] >= tp2_price:
                        tp2_pnl = (tp2_price / entry_price - 1) * 100 * 0.8
                        total = tp1_pnl + tp2_pnl
                        trades.append({"entry": entry_price, "exit": tp2_price, "pnl": round(total, 2),
                                      "outcome": "TP1+TP2", "bars": j - entry_idx})
                        found = True
                        break
                if not found:
                    trades.append({"entry": entry_price, "exit": tp1_price, "pnl": round(tp1_pnl, 2),
                                  "outcome": "TP1", "bars": i - entry_idx})
                in_position = False

    # Статистика
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0, "avg_pnl": 0, "max_win": 0, "max_loss": 0, "sharpe": 0}

    wins = [t for t in trades if t["outcome"] in ("TP1", "TP2", "TP1+TP2")]
    losses = [t for t in trades if t["outcome"] in ("SL", "TP1+SL")]
    wr = len(wins) / n * 100

    pnls = [t["pnl"] for t in trades]
    avg_pnl = sum(pnls) / n
    max_win = max(pnls)
    max_loss = min(pnls)

    if n > 1:
        variance = sum((x - avg_pnl) ** 2 for x in pnls) / n
        std = math.sqrt(variance)
        sharpe = avg_pnl / std if std > 0 else 0
    else:
        sharpe = 0

    return {
        "symbol": symbol,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "avg_pnl": round(avg_pnl, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "sharpe": round(sharpe, 2),
        "trades_detail": trades,
    }


def batch_walk_forward(symbols: list[str], interval: str = "D") -> list[dict]:
    """Массовый walk-forward по списку символов."""
    results = []
    for sym in symbols:
        r = walk_forward(sym, interval)
        if "error" not in r:
            results.append(r)
        time.sleep(0.5)
    return results


# ─── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Walk-forward backtest")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    parser.add_argument("--tf", default="D")
    parser.add_argument("--candles", type=int, default=200)
    parser.add_argument("--batch", nargs="*", help="Multiple symbols")
    args = parser.parse_args()

    if args.batch:
        results = batch_walk_forward(args.batch, args.tf)
        print(json.dumps([{k: v for k, v in r.items() if k != "trades_detail"}
                         for r in results], indent=2))
    else:
        result = walk_forward(args.symbol, args.tf, args.candles)
        if "trades_detail" in result:
            print(json.dumps({k: v for k, v in result.items() if k != "trades_detail"}, indent=2))
            print(f"\nTrades ({len(result['trades_detail'])}):")
            for t in result["trades_detail"]:
                print(f"  {t['outcome']:8s} entry=${t['entry']:.2f} pnl={t['pnl']:+.2f}% bars={t['bars']}")
        else:
            print(json.dumps(result, indent=2))
