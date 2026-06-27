"""
Backtest Engine — walk-forward + Monte Carlo + risk metrics (28.06.2026).

Strategy-agnostic core. Feed it kline data + a strategy function.
Returns: Sharpe, Sortino, Calmar, Max DD, Win Rate, Profit Factor.
"""
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Trade:
    symbol: str
    side: str  # 'Buy' / 'Sell'
    entry: float
    exit: float
    entry_time: int  # candle index
    exit_time: int
    pnl: float
    pnl_pct: float
    session: str = 'normal'


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)

    # Core
    total_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0

    # Risk
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    # Monte Carlo
    mc_expected_return: float = 0.0
    mc_var_95: float = 0.0   # 95% VaR
    mc_cvar_95: float = 0.0  # 95% CVaR


class BacktestEngine:
    """Walk-forward backtesting with Monte Carlo."""

    def __init__(
        self,
        fees: float = 0.0006,      # 0.06% taker
        slippage: float = 0.0002,  # 0.02%
        initial_capital: float = 1000.0,
    ):
        self.fees = fees
        self.slippage = slippage
        self.initial_capital = initial_capital

    def run(
        self,
        candles: list[dict],       # [{open, high, low, close, timestamp}, ...]
        strategy_fn: Callable,     # (candles, params) → list[Trade]
        strategy_params: dict = None,
    ) -> BacktestResult:
        """Run backtest on historical candles."""
        trades = strategy_fn(candles, strategy_params or {}, self.fees, self.slippage)
        return self._compute_metrics(trades)

    def walk_forward(
        self,
        candles: list[dict],
        strategy_fn: Callable,
        train_window: int = 90,     # candles for training
        test_window: int = 30,      # candles for testing
        step: int = 30,             # slide step
    ) -> list[BacktestResult]:
        """Walk-forward validation: train on N, test on next M, slide."""
        results = []
        for start in range(0, len(candles) - train_window - test_window, step):
            train_end = start + train_window
            test_start = train_end
            test_end = min(test_start + test_window, len(candles))

            # Train on historical, test on out-of-sample
            test_candles = candles[test_start:test_end]
            trades = strategy_fn(test_candles, {}, self.fees, self.slippage)

            if trades:
                result = self._compute_metrics(trades)
                results.append(result)

        return results

    def monte_carlo(
        self,
        trades: list[Trade],
        runs: int = 1000,
    ) -> dict:
        """Monte Carlo simulation: resample trade returns."""
        if not trades:
            return {'expected_return': 0, 'var_95': 0, 'cvar_95': 0}

        returns = [t.pnl_pct for t in trades]
        simulated_returns = []

        for _ in range(runs):
            # Resample with replacement
            sampled = random.choices(returns, k=len(returns))
            total_return = sum(sampled)
            simulated_returns.append(total_return)

        simulated_returns.sort()
        expected = statistics.mean(simulated_returns)
        var_95 = simulated_returns[int(runs * 0.05)]  # 5th percentile
        cvar_95 = statistics.mean(simulated_returns[:int(runs * 0.05)])  # avg of worst 5%

        return {
            'expected_return': round(expected, 4),
            'var_95': round(var_95, 4),
            'cvar_95': round(cvar_95, 4),
        }

    def _compute_metrics(self, trades: list[Trade]) -> BacktestResult:
        """Calculate all risk metrics from trade list."""
        if not trades:
            return BacktestResult(total_trades=0)

        # PnL
        pnl_values = [t.pnl for t in trades]
        total_pnl = sum(pnl_values)
        wins = sum(1 for p in pnl_values if p > 0)
        losses = sum(1 for p in pnl_values if p < 0)
        total_trades = len(trades)

        win_rate = wins / total_trades if total_trades > 0 else 0.0

        gross_profit = sum(p for p in pnl_values if p > 0) or 1
        gross_loss = abs(sum(p for p in pnl_values if p < 0)) or 1
        profit_factor = gross_profit / gross_loss

        # Returns as percentages
        returns = [t.pnl_pct for t in trades]

        # Equity curve
        equity = self.initial_capital
        equity_curve = [equity]
        for p in pnl_values:
            equity += p
            equity_curve.append(equity)

        # Sharpe (annualized, assuming daily candles ~365 per year)
        if len(returns) > 1:
            mean_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns) if len(returns) > 1 else 0.001
            sharpe = (mean_ret / std_ret) * math.sqrt(365) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        # Sortino (only downside deviation)
        downside_returns = [r for r in returns if r < 0]
        if len(downside_returns) > 2:
            downside_std = statistics.stdev(downside_returns)
            sortino = (statistics.mean(returns) / downside_std) * math.sqrt(365) if downside_std > 1e-8 else 0.0
        else:
            sortino = 0.0

        # Max drawdown
        peak = equity_curve[0]
        max_dd = 0.0
        max_dd_pct = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            dd_pct = dd / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Calmar
        annual_return = (equity / self.initial_capital - 1) * (365 / max(total_trades, 1))
        calmar = annual_return / max_dd_pct if max_dd_pct > 0 else 0.0

        # Monte Carlo
        mc = self.monte_carlo(trades)

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            returns=returns,
            total_pnl=round(total_pnl, 2),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 2),
            total_trades=total_trades,
            sharpe=round(sharpe, 2),
            sortino=round(sortino, 2),
            calmar=round(calmar, 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd_pct, 4),
            mc_expected_return=round(mc['expected_return'], 4),
            mc_var_95=round(mc['var_95'], 4),
            mc_cvar_95=round(mc['cvar_95'], 4),
        )


def calculate_metrics_summary(results: list[BacktestResult]) -> dict:
    """Aggregate multiple walk-forward results into summary."""
    if not results:
        return {'error': 'no results'}

    sharpes = [r.sharpe for r in results if r.total_trades > 5]
    win_rates = [r.win_rate for r in results]
    max_dds = [r.max_drawdown_pct for r in results]

    return {
        'folds': len(results),
        'sharpe_avg': round(statistics.mean(sharpes), 2) if sharpes else 0,
        'sharpe_min': round(min(sharpes), 2) if sharpes else 0,
        'sharpe_max': round(max(sharpes), 2) if sharpes else 0,
        'win_rate_avg': round(statistics.mean(win_rates), 3) if win_rates else 0,
        'max_dd_avg': round(statistics.mean(max_dds), 3) if max_dds else 0,
        'total_trades': sum(r.total_trades for r in results),
        'total_pnl': round(sum(r.total_pnl for r in results), 2),
    }
