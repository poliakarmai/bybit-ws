"""
backtest.py — Backtesting framework (Фаза 7: Monte Carlo + Sharpe + Sortino + Calmar).

Работает на реальной истории сделок из state.db.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
STATE_DB = DATA_DIR / "state.db"
DEFAULT_OUTPUT = DATA_DIR / "backtest_report.json"

# Risk-free rate (annual, ~5% US T-bills proxy for crypto)
RISK_FREE_RATE = 0.05


# ═══════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════

def load_trade_history(db_path: str | Path = STATE_DB) -> list[dict[str, Any]]:
    """Загрузить завершённые сделки из trade_history."""
    db = Path(db_path)
    if not db.exists():
        return []

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_history WHERE closed_at IS NOT NULL "
        "AND entry_price > 0 AND exit_price > 0 "
        "ORDER BY closed_at"
    ).fetchall()
    conn.close()

    trades = []
    for r in rows:
        try:
            entry = float(r["entry_price"])
            exit_p = float(r["exit_price"])
            size = float(r["size"])
            pnl = float(r["pnl"] or 0)
            fees = float(r["fees"] or 0) if "fees" in r.keys() else 0.0
            side = r["side"] if "side" in r.keys() else "Buy"
            closed_at = r["closed_at"] if "closed_at" in r.keys() else ""

            # Normalize PnL direction for SHORT
            if side == "Sell":
                pnl = (entry - exit_p) * size - fees
            else:
                pnl = (exit_p - entry) * size - fees

            trades.append({
                "symbol": r["symbol"],
                "side": side,
                "entry_price": entry,
                "exit_price": exit_p,
                "size": size,
                "pnl": pnl,
                "pnl_pct": pnl / (entry * size) * 100 if entry * size > 0 else 0,
                "strategy": r["strategy"] if "strategy" in r.keys() else "",
                "closed_at": closed_at,
            })
        except (TypeError, ValueError, KeyError):
            continue

    return trades


# ═══════════════════════════════════════════════════
# Core metrics
# ═══════════════════════════════════════════════════

def _returns_from_trades(trades: list[dict]) -> list[float]:
    """Extract PnL returns as % of initial capital assumption."""
    if not trades:
        return []
    # Use median entry notional as capital proxy
    notions = [t["entry_price"] * t["size"] for t in trades]
    capital = float(np.median(notions)) * 3 if notions else 100.0
    return [t["pnl"] / capital for t in trades]


def sharpe_ratio(returns: list[float], risk_free: float = RISK_FREE_RATE,
                 periods_per_year: int = 365) -> float:
    """Annualized Sharpe ratio."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean_ret = float(np.mean(arr))
    std_ret = float(np.std(arr, ddof=1))
    if std_ret == 0:
        return 0.0
    daily_rf = risk_free / periods_per_year
    return (mean_ret - daily_rf) / std_ret * math.sqrt(periods_per_year)


def sortino_ratio(returns: list[float], risk_free: float = RISK_FREE_RATE,
                  periods_per_year: int = 365) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    mean_ret = float(np.mean(arr))
    daily_rf = risk_free / periods_per_year

    downside = arr[arr < daily_rf]
    if len(downside) < 2:
        return 0.0
    downside_std = float(np.std(downside, ddof=1))
    if downside_std == 0:
        return 0.0

    return (mean_ret - daily_rf) / downside_std * math.sqrt(periods_per_year)


def calmar_ratio(returns: list[float], periods_per_year: int = 365) -> float:
    """Calmar ratio = annualized return / max drawdown."""
    if len(returns) < 2:
        return 0.0

    arr = np.array(returns)
    annual_return = float(np.mean(arr)) * periods_per_year

    # Max drawdown
    cumulative = np.cumsum(arr)
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak
    max_dd = float(np.min(drawdown))

    if max_dd == 0:
        return 0.0
    return abs(annual_return / max_dd)


def max_drawdown(returns: list[float]) -> float:
    """Maximum drawdown as % of peak."""
    if not returns:
        return 0.0
    cumulative = np.cumsum(returns)
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak
    return float(np.min(drawdown))


def win_rate(trades: list[dict]) -> float:
    """Win rate (PnL > 0)."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return wins / len(trades)


def profit_factor(trades: list[dict]) -> float:
    """Profit factor = gross profit / gross loss."""
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    if gross_loss == 0:
        return float('inf') if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def expectancy(trades: list[dict]) -> float:
    """Average profit per trade ($)."""
    if not trades:
        return 0.0
    return sum(t["pnl"] for t in trades) / len(trades)


def avg_rr_ratio(trades: list[dict]) -> float:
    """Average R:R (avg win / avg loss)."""
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
    if not losses or not wins:
        return 0.0
    return float(np.mean(wins) / np.mean(losses))


# ═══════════════════════════════════════════════════
# Monte Carlo
# ═══════════════════════════════════════════════════

def monte_carlo(trades: list[dict], simulations: int = 10000,
                confidence: float = 0.95) -> dict[str, Any]:
    """Monte Carlo simulation: reshuffle trade sequence.

    Returns:
        {
            "simulations": N,
            "confidence": 0.95,
            "mean_final_pnl": ...,
            "var_95": ...,
            "cvar_95": ...,
            "max_drawdown_95": ...,
            "ruin_probability": ...,
            "best_case": ...,
            "worst_case": ...,
        }
    """
    if len(trades) < 5:
        return {"error": "Need 5+ trades for Monte Carlo"}

    pnls = [t["pnl"] for t in trades]
    n = len(pnls)

    final_pnls = []
    max_drawdowns = []

    for _ in range(simulations):
        shuffled = random.sample(pnls, n)
        cumulative = np.cumsum(shuffled)
        final_pnls.append(float(cumulative[-1]))

        peak = np.maximum.accumulate(cumulative)
        dd = cumulative - peak
        max_drawdowns.append(float(np.min(dd)))

    final_pnls = np.array(final_pnls)
    max_drawdowns = np.array(max_drawdowns)

    # VaR and CVaR
    var_idx = int((1 - confidence) * simulations)
    sorted_pnls = np.sort(final_pnls)
    var_95 = float(sorted_pnls[var_idx])
    cvar_95 = float(np.mean(sorted_pnls[:var_idx + 1]))

    # Ruin probability: final PnL wipes 50%+ of initial capital
    initial_capital = abs(float(np.min(np.cumsum(pnls)))) + 100.0
    ruin = float(np.mean(final_pnls < -0.5 * initial_capital))

    return {
        "simulations": simulations,
        "confidence": confidence,
        "mean_final_pnl": round(float(np.mean(final_pnls)), 2),
        "median_final_pnl": round(float(np.median(final_pnls)), 2),
        "std_final_pnl": round(float(np.std(final_pnls)), 2),
        "var_95": round(var_95, 2),
        "cvar_95": round(cvar_95, 2),
        "max_drawdown_95": round(float(np.percentile(max_drawdowns, 95)), 2),
        "ruin_probability": round(ruin, 4),
        "best_case": round(float(np.max(final_pnls)), 2),
        "worst_case": round(float(np.min(final_pnls)), 2),
    }


# ═══════════════════════════════════════════════════
# Full report
# ═══════════════════════════════════════════════════

def generate_report(trades: list[dict] | None = None,
                    db_path: str | Path = STATE_DB,
                    output_path: str | Path = DEFAULT_OUTPUT,
                    monte_carlo_sims: int = 10000) -> dict[str, Any]:
    """Generate full backtesting report.

    Args:
        trades: Pre-loaded trades (or None to load from DB).
        db_path: Path to state.db.
        output_path: Where to save JSON report.
        monte_carlo_sims: Number of Monte Carlo simulations.

    Returns:
        Dict with all metrics ready for display and JSON serialization.
    """
    if trades is None:
        trades = load_trade_history(db_path)

    if not trades:
        return {"error": "No trades found"}

    returns = _returns_from_trades(trades)
    total_pnl = sum(t["pnl"] for t in trades)
    n_trades = len(trades)

    report = {
        "generated_at": datetime.now().isoformat(),
        "source": str(db_path),
        "summary": {
            "total_trades": n_trades,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate(trades), 4),
            "profit_factor": round(profit_factor(trades), 2),
            "expectancy": round(expectancy(trades), 2),
            "avg_win": round(float(np.mean([t["pnl"] for t in trades if t["pnl"] > 0])), 2)
                       if any(t["pnl"] > 0 for t in trades) else 0,
            "avg_loss": round(float(np.mean([abs(t["pnl"]) for t in trades if t["pnl"] < 0])), 2)
                        if any(t["pnl"] < 0 for t in trades) else 0,
            "avg_rr_ratio": round(avg_rr_ratio(trades), 2),
            "max_drawdown": round(max_drawdown(returns), 4),
        },
        "ratios": {
            "sharpe": round(sharpe_ratio(returns), 3),
            "sortino": round(sortino_ratio(returns), 3),
            "calmar": round(calmar_ratio(returns), 3),
        },
    }

    # Per-symbol breakdown
    symbols = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in symbols:
            symbols[sym] = []
        symbols[sym].append(t)

    symbol_stats = {}
    for sym, sym_trades in sorted(symbols.items()):
        sym_pnl = sum(t["pnl"] for t in sym_trades)
        sym_wr = win_rate(sym_trades)
        symbol_stats[sym] = {
            "trades": len(sym_trades),
            "pnl": round(sym_pnl, 2),
            "win_rate": round(sym_wr, 3),
            "expectancy": round(expectancy(sym_trades), 2),
        }
    report["symbols"] = symbol_stats

    # Monte Carlo
    mc = monte_carlo(trades, simulations=monte_carlo_sims)
    report["monte_carlo"] = mc

    # Interpretation
    sharpe = report["ratios"]["sharpe"]
    sortino = report["ratios"]["sortino"]
    calmar = report["ratios"]["calmar"]
    wr = report["summary"]["win_rate"]
    pf = report["summary"]["profit_factor"]

    verdicts = []
    if sharpe > 1.0:
        verdicts.append("✅ Sharpe > 1.0 — good risk-adjusted return")
    elif sharpe > 0.5:
        verdicts.append("🟡 Sharpe > 0.5 — acceptable")
    else:
        verdicts.append("🔴 Sharpe < 0.5 — below threshold")

    if sortino > 1.5:
        verdicts.append("✅ Sortino > 1.5 — strong upside vs downside")
    elif sortino > 0.8:
        verdicts.append("🟡 Sortino > 0.8 — moderate")
    else:
        verdicts.append("🔴 Sortino < 0.8 — weak")

    if calmar > 0.5:
        verdicts.append("✅ Calmar > 0.5 — good return per unit of drawdown")
    else:
        verdicts.append("🟡 Calmar < 0.5 — low return per drawdown")

    if wr > 0.50:
        verdicts.append("✅ Win rate > 50%")
    elif wr > 0.40:
        verdicts.append("🟡 Win rate 40-50%")
    else:
        verdicts.append("🔴 Win rate < 40%")

    if pf > 1.5:
        verdicts.append("✅ Profit Factor > 1.5")
    elif pf > 1.2:
        verdicts.append("🟡 Profit Factor 1.2-1.5")
    else:
        verdicts.append("🔴 Profit Factor < 1.2")

    if mc.get("ruin_probability", 1) < 0.05:
        verdicts.append("✅ Ruin probability < 5%")
    else:
        verdicts.append(f'🟡 Ruin probability = {mc.get("ruin_probability", 0):.1%}')

    report["verdicts"] = verdicts
    report["overall"] = "✅ STRONG" if all(v.startswith("✅") for v in verdicts[:3]) else \
                        "🔴 WEAK" if sum(1 for v in verdicts if v.startswith("🔴")) >= 3 else \
                        "🟡 MODERATE"

    # Save
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(report, f, indent=2, default=str)

    return report


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

def print_report(report: dict[str, Any]):
    """Pretty-print backtesting report."""
    if "error" in report:
        print(f'❌ {report["error"]}')
        return

    s = report["summary"]
    r = report["ratios"]
    mc = report.get("monte_carlo", {})

    print("=" * 60)
    print("  BACKTEST REPORT")
    print("=" * 60)
    print(f'  Trades:       {s["total_trades"]}')
    print(f'  Total PnL:    ${s["total_pnl"]:+.2f}')
    print(f'  Win Rate:     {s["win_rate"]:.1%}')
    print(f'  Profit Factor:{s["profit_factor"]:.2f}')
    print(f'  Expectancy:   ${s["expectancy"]:+.2f}/trade')
    print(f'  Avg Win:      ${s["avg_win"]:.2f}')
    print(f'  Avg Loss:     ${s["avg_loss"]:.2f}')
    print(f'  Avg R:R:      {s["avg_rr_ratio"]:.2f}')
    print(f'  Max Drawdown: {s["max_drawdown"]:.4f}')
    print()
    print(f'  Sharpe:       {r["sharpe"]:.3f}')
    print(f'  Sortino:      {r["sortino"]:.3f}')
    print(f'  Calmar:       {r["calmar"]:.3f}')
    print()

    if mc and "error" not in mc:
        print("  ── Monte Carlo ──")
        print(f'  Sims:         {mc["simulations"]}')
        print(f'  Mean PnL:     ${mc["mean_final_pnl"]:+.2f}')
        print(f'  VaR 95%:      ${mc["var_95"]:+.2f}')
        print(f'  CVaR 95%:     ${mc["cvar_95"]:+.2f}')
        print(f'  Max DD 95%:   ${mc["max_drawdown_95"]:+.2f}')
        print(f'  Ruin Prob:    {mc["ruin_probability"]:.2%}')
        print(f'  Best Case:    ${mc["best_case"]:+.2f}')
        print(f'  Worst Case:   ${mc["worst_case"]:+.2f}')
        print()

    print(f'  Overall:      {report["overall"]}')
    print()
    for v in report.get("verdicts", []):
        print(f'  {v}')
    print("=" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backtesting framework")
    parser.add_argument("--db", default=str(STATE_DB), help="Path to state.db")
    parser.add_argument("--mc-sims", type=int, default=10000,
                        help="Monte Carlo simulations (default: 10000)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--symbols", action="store_true", help="Show per-symbol breakdown")
    args = parser.parse_args()

    trades = load_trade_history(args.db)
    report = generate_report(trades, db_path=args.db, monte_carlo_sims=args.mc_sims)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
        if args.symbols and "symbols" in report:
            print("\n  Per-Symbol:")
            for sym, stats in sorted(report["symbols"].items()):
                print(f'    {sym:12s}  {stats["trades"]:3d} trades  '
                      f'${stats["pnl"]:+7.2f}  WR={stats["win_rate"]:.1%}')
