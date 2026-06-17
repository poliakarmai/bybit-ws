"""
Phase 3: Backtesting Module — историческое тестирование стратегии Bollinger Grid.

Использует PaperExchange для симуляции сделок на исторических данных.
Сохраняет результаты в JSON для ML-скоринга.

Использование:
    python3 backtest.py SYMBOLUSDT               # один символ
    python3 backtest.py --batch top20             # топ-20 по объёму
    python3 backtest.py --batch top20 --days 90   # за 90 дней
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path.home()))
sys.path.insert(0, str(Path(__file__).parent))
from bybit_ws.api import bybit
from bybit_ws.paper_api import PaperExchange

BACKTEST_DIR = Path.home() / ".local" / "share" / "bybit-ws" / "backtests"
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

# ─── BB Calculation ─────────────────────────────────────────

def calc_bb(close_prices: list[float], period: int = 20, stddev: float = 2.0):
    """Рассчитывает Bollinger Bands из списка цен закрытия."""
    if len(close_prices) < period:
        return None, None, None, None  # sma, upper, lower, bb%

    sma = np.mean(close_prices[-period:])
    std = np.std(close_prices[-period:])
    upper = sma + stddev * std
    lower = sma - stddev * std
    bb_pct = (close_prices[-1] - lower) / (upper - lower) * 100 if upper != lower else 50.0

    return sma, upper, lower, bb_pct


# ─── Kline Loading ──────────────────────────────────────────

def load_klines(symbol: str, interval: str = "D", limit: int = 200) -> list[dict]:
    """Загружает исторические свечи через Bybit API."""
    try:
        resp = bybit(
            "GET",
            f"/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}",
        )
        if isinstance(resp, dict) and resp.get("retCode") == 0:
            klines = resp["result"]["list"]
            # Bybit: [ts, open, high, low, close, volume, turnover]
            # От старых к новым
            klines.reverse()
            return [
                {
                    "ts": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "date": datetime.fromtimestamp(int(k[0]) / 1000).strftime("%Y-%m-%d"),
                }
                for k in klines
            ]
    except Exception as e:
        print(f"  ⚠️ Ошибка загрузки {symbol}: {e}")
    return []


# ─── Strategy Simulation ────────────────────────────────────

def simulate_strategy(
    symbol: str,
    klines: list[dict],
    bb_period: int = 20,
    entry_bb_threshold: float = 5.0,
    sl_factor: float = 0.93,
    margin: float = 15.0,
    leverage: float = 3.0,
) -> dict:
    """
    Симулирует торговлю на исторических данных.
    Возвращает словарь с результатами.
    """
    px = PaperExchange()
    trades = []
    equity_curve = []
    initial_balance = px.get_balance()

    close_prices = []
    highs = []
    lows = []

    for i, kline in enumerate(klines):
        close_prices.append(kline["close"])
        highs.append(kline["high"])
        lows.append(kline["low"])

        if len(close_prices) < bb_period + 1:
            equity_curve.append(px.get_balance())
            continue

        sma, upper, lower, bb_pct = calc_bb(close_prices, bb_period)
        if sma is None:
            equity_curve.append(px.get_balance())
            continue

        price = kline["close"]

        # Обновляем все mark-цены
        positions = px.fetch_positions()
        price_map = {sym: price for sym in positions}
        if price_map:
            px.update_mark_prices(price_map)

        # Проверка SL/TP/ликвидаций для открытых позиций
        for sym, pos in list(positions.items()):
            if pos["size"] <= 0:
                continue

            side = pos["side"]
            entry = pos["entry"]
            sl = pos.get("stopLoss")
            tp = pos.get("take_profit")
            liq = pos.get("liqPrice")
            pos_size = pos["size"]

            # Ликвидация
            if liq:
                if side == "Buy" and kline["low"] <= liq:
                    pnl = -pos.get("positionIM", margin)
                    px.place_order(sym, "Sell", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                    trades.append({"type": "liquidation", "symbol": sym, "entry": entry,
                                   "exit": liq, "pnl": pnl, "date": kline["date"]})
                    continue
                if side == "Sell" and kline["high"] >= liq:
                    pnl = -pos.get("positionIM", margin)
                    px.place_order(sym, "Buy", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                    trades.append({"type": "liquidation", "symbol": sym, "entry": entry,
                                   "exit": liq, "pnl": pnl, "date": kline["date"]})
                    continue

            # Stop Loss
            if sl and side == "Buy" and kline["low"] <= sl:
                pnl = pos_size * (sl - entry)
                px.place_order(sym, "Sell", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                trades.append({"type": "sl", "symbol": sym, "entry": entry, "exit": sl,
                               "pnl": pnl, "date": kline["date"]})
                continue
            if sl and side == "Sell" and kline["high"] >= sl:
                pnl = pos_size * (entry - sl)
                px.place_order(sym, "Buy", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                trades.append({"type": "sl", "symbol": sym, "entry": entry, "exit": sl,
                               "pnl": pnl, "date": kline["date"]})
                continue

            # Take Profit
            if tp and side == "Buy" and kline["high"] >= tp:
                pnl = pos_size * (tp - entry)
                px.place_order(sym, "Sell", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                trades.append({"type": "tp", "symbol": sym, "entry": entry, "exit": tp,
                               "pnl": pnl, "date": kline["date"]})
                continue
            if tp and side == "Sell" and kline["low"] <= tp:
                pnl = pos_size * (entry - tp)
                px.place_order(sym, "Buy", "Market", pos_size, reduce_only=True, position_idx=pos.get("positionIdx", 0))
                trades.append({"type": "tp", "symbol": sym, "entry": entry, "exit": tp,
                               "pnl": pnl, "date": kline["date"]})
                continue

        # Вход: BB% ниже порога и нет открытой позиции по этому символу
        positions_after = px.fetch_positions()
        if bb_pct < entry_bb_threshold and symbol not in positions_after:
            qty = (margin * leverage) / price
            sl_price = lower * sl_factor
            tp_price = sma

            px.place_order(symbol, "Buy", "Market", qty)
            px.place_stop_loss(symbol, 0, "Buy", qty, sl_price)
            px.place_take_profit(symbol, 0, "Buy", qty, tp_price)

            trades.append({"type": "entry", "symbol": symbol, "price": price,
                           "bb_pct": bb_pct, "date": kline["date"],
                           "sl": sl_price, "tp": tp_price})

        equity_curve.append(px.get_balance())

    # Итоговые метрики
    pnl_trades = [t for t in trades if t["type"] in ("sl", "tp", "liquidation")]
    winning = [t for t in pnl_trades if t["pnl"] > 0]
    losing = [t for t in pnl_trades if t["pnl"] <= 0]

    total_pnl = sum(t["pnl"] for t in pnl_trades)
    winrate = len(winning) / len(pnl_trades) * 100 if pnl_trades else 0

    # Max drawdown
    peak = initial_balance
    max_dd = 0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio
    returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            returns.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
    sharpe = (np.mean(returns) / np.std(returns) * np.sqrt(252)) if returns and np.std(returns) > 0 else 0

    profit_factor = (
        sum(t["pnl"] for t in winning) / abs(sum(t["pnl"] for t in losing))
        if losing and sum(t["pnl"] for t in losing) != 0
        else float("inf")
    )

    return {
        "symbol": symbol,
        "period": f"{klines[0]['date']} → {klines[-1]['date']}",
        "days": len(klines),
        "total_trades": len(trades),
        "entry_count": len([t for t in trades if t["type"] == "entry"]),
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "winrate": round(winrate, 1),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999,
        "final_balance": round(equity_curve[-1] if equity_curve else initial_balance, 2),
        "trades": trades[-20:],
    }


# ─── Batch Runner ───────────────────────────────────────────

def get_top_symbols(n: int = 20) -> list[str]:
    """Получает топ-N символов по объёму."""
    try:
        resp = bybit("GET", "/v5/market/tickers?category=linear")
        if isinstance(resp, dict) and resp.get("retCode") == 0:
            tickers = resp["result"]["list"]
            usdt_tickers = [
                t for t in tickers
                if t["symbol"].endswith("USDT")
                and not any(x in t["symbol"] for x in ["USDC", "USDE", "1000", "2000"])
            ]
            usdt_tickers.sort(key=lambda t: float(t.get("volume24h", 0)), reverse=True)
            return [t["symbol"] for t in usdt_tickers[:n]]
    except Exception:
        pass
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "DOGEUSDT"]


def run_backtest(symbol: str, days: int = 90) -> dict | None:
    """Запускает бэктест для одного символа."""
    print(f"  🔄 {symbol}...", end=" ", flush=True)
    klines = load_klines(symbol, "D", limit=days + 30)
    if len(klines) < 30:
        print(f"❌ недостаточно данных ({len(klines)} свечей)")
        return None

    result = simulate_strategy(symbol, klines)
    print(f"✅ PnL=${result['total_pnl']:+.2f} | WR={result['winrate']}% | DD={result['max_drawdown_pct']}%")
    return result


# ─── CLI ─────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backtest Bollinger Grid strategy")
    parser.add_argument("symbol", nargs="?", help="Symbol (e.g. BTCUSDT)")
    parser.add_argument("--batch", choices=["top10", "top20", "top30"], default=None,
                        help="Batch mode: top N by volume")
    parser.add_argument("--days", type=int, default=90, help="Days of history (default: 90)")
    parser.add_argument("--output", "-o", default=None, help="Output JSON file")
    args = parser.parse_args()

    symbols = []
    if args.batch:
        n = int(args.batch.replace("top", ""))
        symbols = get_top_symbols(n)
        print(f"📊 Бэктест топ-{n}: {', '.join(symbols[:5])}...")
    elif args.symbol:
        symbols = [args.symbol]
    else:
        parser.print_help()
        return

    results = []
    for sym in symbols:
        result = run_backtest(sym, args.days)
        if result:
            results.append(result)

    if not results:
        print("❌ Нет результатов")
        return

    # Сводка
    print(f"\n{'='*60}")
    print(f"📊 ИТОГИ БЭКТЕСТА ({len(results)} символов, {args.days} дней)")
    print(f"{'='*60}")

    total_pnl = sum(r["total_pnl"] for r in results)
    avg_winrate = np.mean([r["winrate"] for r in results])
    avg_dd = np.mean([r["max_drawdown_pct"] for r in results])
    avg_sharpe = np.mean([r["sharpe"] for r in results])

    best = max(results, key=lambda r: r["total_pnl"])
    worst = min(results, key=lambda r: r["total_pnl"])
    best_wr = max(results, key=lambda r: r["winrate"])

    print(f"💰 Общий PnL: ${total_pnl:+.2f}")
    print(f"🎯 Средний Winrate: {avg_winrate:.1f}%")
    print(f"📉 Средняя просадка: {avg_dd:.1f}%")
    print(f"📈 Средний Sharpe: {avg_sharpe:.2f}")
    print(f"🏆 Лучший: {best['symbol']} (PnL=${best['total_pnl']:+.2f}, WR={best['winrate']}%)")
    print(f"💀 Худший: {worst['symbol']} (PnL=${worst['total_pnl']:+.2f}, WR={worst['winrate']}%)")
    print(f"🎯 Винрейт: {best_wr['symbol']} ({best_wr['winrate']}%)")

    # Сохранение
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = BACKTEST_DIR / f"backtest_{ts}.json"

    with open(output_path, "w") as f:
        json.dump({"timestamp": ts, "symbols": len(results), "total_pnl": total_pnl,
                    "avg_winrate": avg_winrate, "avg_drawdown_pct": avg_dd, "avg_sharpe": avg_sharpe,
                    "results": results}, f, indent=2, ensure_ascii=False)

    print(f"\n📁 Результаты: {output_path}")


if __name__ == "__main__":
    main()
