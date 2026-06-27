"""
Bollinger Grid Strategy — симуляция для backtesting (28.06.2026).

Моделирует LONG-входы при касании lower BB + confluence.
Использует те же правила что и авто-монитор: SL=-7% от lower, TP=ATR-based.
"""
import math
from .__init__ import Trade as BTTrade


def bb_grid_strategy(
    candles: list[dict],
    params: dict,
    fees: float,
    slippage: float,
) -> list[BTTrade]:
    """Bollinger Grid LONG strategy.

    candles: [{open, high, low, close, timestamp}, ...] chrono order
    params: {bb_period: 20, bb_std: 2.0, sl_mult: 0.93, tp_mult: [1.0, 2.0, 3.0], ...}
    """
    period = params.get('bb_period', 20)
    std_mult = params.get('bb_std', 2.0)
    sl_mult = params.get('sl_mult', 0.93)
    tp_levels = params.get('tp_levels', [1.0, 2.0, 3.0])
    tp_splits = params.get('tp_splits', [0.40, 0.35, 0.25])
    min_score = params.get('min_score', 20)
    max_holding = params.get('max_holding', 48)
    atr_period = params.get('atr_period', 14)

    trades = []
    in_position = None  # {entry, entry_idx, bb_lower, bb_mid, bb_upper, sl, tp1, tp2, tp3}

    for i in range(period + 1, len(candles)):
        c = candles[i]
        close = float(c.get('close', c.get('c', 0)))
        high = float(c.get('high', c.get('h', close)))
        low = float(c.get('low', c.get('l', close)))
        if close <= 0:
            continue

        # Calculate BB
        closes = [float(candles[j].get('close', candles[j].get('c', 0)))
                  for j in range(i - period, i)]
        if not closes or len(closes) < period:
            continue
        sma = sum(closes) / len(closes)
        std = math.sqrt(sum((c - sma) ** 2 for c in closes) / len(closes))
        bb_upper = sma + std_mult * std
        bb_lower = sma - std_mult * std
        bb_mid = sma

        # Calculate ATR
        atrs = []
        for j in range(i - atr_period, i):
            h = float(candles[j].get('high', candles[j].get('h', 0)))
            l = float(candles[j].get('low', candles[j].get('l', 0)))
            pc = float(candles[j-1].get('close', candles[j-1].get('c', 0))) if j > 0 else h
            tr = max(h - l, abs(h - pc), abs(l - pc))
            atrs.append(tr)
        atr = sum(atrs) / len(atrs) if atrs else 0

        if in_position is None:
            # Entry signal: price touches lower BB
            if low <= bb_lower:
                # Check confluence: score = 10 (lower touch) + MTF bonus
                score = 20 + (5 if bb_lower < bb_mid * 0.95 else 0)
                if score < min_score:
                    continue

                entry_price = bb_lower * (1 - slippage)  # entry with slippage
                sl_price = bb_lower * sl_mult

                # ATR-based TP levels
                tp_prices = []
                for k in tp_levels:
                    tp_prices.append(entry_price + k * atr)

                in_position = {
                    'entry': entry_price,
                    'entry_idx': i,
                    'bb_lower': bb_lower,
                    'sl': sl_price,
                    'tp': tp_prices,
                    'tp_splits': tp_splits,
                }
        else:
            # Check exit conditions
            pos = in_position
            exit_price = None
            exit_reason = ''

            # SL hit
            if low <= pos['sl']:
                exit_price = pos['sl'] * (1 - slippage)
                exit_reason = 'SL'

            # TP hit (check in order: TP3, TP2, TP1)
            elif high >= pos['tp'][2]:
                exit_price = pos['tp'][2] * (1 - slippage)
                pos['tp_splits'] = [0, 0, 1.0]  # all remaining at TP3
                exit_reason = 'TP3'
            elif high >= pos['tp'][1]:
                exit_price = pos['tp'][1] * (1 - slippage)
                pos['tp_splits'] = [0, 1.0, 0]
                exit_reason = 'TP2'
            elif high >= pos['tp'][0]:
                exit_price = pos['tp'][0] * (1 - slippage)
                exit_reason = 'TP1'

            # Max holding
            hours_held = (i - pos['entry_idx'])  # assume 1h candles
            if hours_held >= max_holding and not exit_price:
                exit_price = close * (1 - slippage)
                exit_reason = 'max_holding'

            if exit_price:
                # Calculate PnL with fees
                gross_pnl = (exit_price - pos['entry']) / pos['entry'] * 100
                fee_impact = fees * 2 * 100  # entry + exit fees
                pnl_pct = gross_pnl - fee_impact

                trade = BTTrade(
                    symbol='SIM',
                    side='Buy',
                    entry=pos['entry'],
                    exit=exit_price,
                    entry_time=pos['entry_idx'],
                    exit_time=i,
                    pnl=pnl_pct,  # % of position
                    pnl_pct=pnl_pct,
                )
                trades.append(trade)
                in_position = None

    return trades
