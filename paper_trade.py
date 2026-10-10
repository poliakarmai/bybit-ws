"""
Paper Trading / Бэктестинг Bollinger Grid на исторических данных.

Использование:
    python3 -m bybit_ws.paper_trade SOLUSDT --days 30
    python3 -m bybit_ws.paper_trade BTCUSDT --days 90 --interval D

Симулирует:
- BB-скоринг (6 метрик без ML)
- Вход на Lower BB, выход по SL/TP
- Комиссия 0.055%, проскальзывание 0.05%
"""
import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bybit_ws.api import bybit
from bybit_ws.alerts import log_event

# ── Константы ──
TAKER_FEE = 0.00055    # 0.055%
SLIPPAGE = 0.0005      # 0.05%
DEFAULT_SL_PCT = 0.05  # 5%
DEFAULT_TP_ATR_MULT = 2.0
MIN_SCORE = 20  # чуть ниже боевого (25) — бэктест не наказывает за ложные входы


# ═══════════════════════════════════════════════════════════
# PaperExchange — мок Bybit API
# ═══════════════════════════════════════════════════════════

class PaperExchange:
    """Симулятор биржи на исторических данных."""

    def __init__(self, symbol: str, klines: list[dict], initial_balance: float = 1000):
        self.symbol = symbol
        self.klines = klines
        self.balance = initial_balance
        self.equity = initial_balance
        self.position: Optional[dict] = None
        self.trades: list[dict] = []
        self._commission_paid = 0.0

    def advance(self, candle: dict):
        """Проверить SL/TP существующей позиции по свече, обновить equity."""
        pos = self.position
        if not pos:
            return

        high, low, close = candle['high'], candle['low'], candle['close']
        # Проверка SL
        if pos['side'] == 'Buy':
            if low <= pos['sl']:
                self._close_position(pos['sl'], 'SL', candle['open_time'])
                return
        else:
            if high >= pos['sl']:
                self._close_position(pos['sl'], 'SL', candle['open_time'])
                return

        # Проверка TP
        if pos['tp']:
            if pos['side'] == 'Buy':
                if high >= pos['tp']:
                    self._close_position(pos['tp'], 'TP', candle['open_time'])
                    return
            else:
                if low <= pos['tp']:
                    self._close_position(pos['tp'], 'TP', candle['open_time'])
                    return

        # Обновление equity по close
        self._update_equity(close)

    def open_long(self, price: float, sl: float, tp: float | None, qty: float, entry_time: int):
        entry_fee = qty * price * (TAKER_FEE + SLIPPAGE)
        self.balance -= entry_fee
        self._commission_paid += qty * price * TAKER_FEE
        self.position = {
            'side': 'Buy', 'entry': price, 'qty': qty,
            'sl': sl, 'tp': tp, 'entry_time': entry_time
        }
        self.equity = self.balance
        return True

    def open_short(self, price: float, sl: float, tp: float | None, qty: float, entry_time: int):
        entry_fee = qty * price * (TAKER_FEE + SLIPPAGE)
        self.balance -= entry_fee
        self._commission_paid += qty * price * TAKER_FEE
        self.position = {
            'side': 'Sell', 'entry': price, 'qty': qty,
            'sl': sl, 'tp': tp, 'entry_time': entry_time
        }
        self.equity = self.balance
        return True

    def _close_position(self, exit_price: float, reason: str, exit_time: int):
        pos = self.position
        if not pos:
            return

        qty, entry = pos['qty'], pos['entry']
        if pos['side'] == 'Buy':
            gross_pnl = qty * (exit_price - entry)
        else:
            gross_pnl = qty * (entry - exit_price)

        exit_fee = qty * exit_price * TAKER_FEE
        net_pnl = gross_pnl - exit_fee

        self.balance += net_pnl
        self._commission_paid += exit_fee

        pnl_pct = (net_pnl / (qty * entry)) * 100 if qty * entry > 0 else 0
        self.trades.append({
            'side': pos['side'], 'entry': entry, 'exit': exit_price,
            'pnl': round(net_pnl, 2), 'pnl_pct': round(pnl_pct, 2),
            'reason': reason, 'entry_time': pos['entry_time'],
            'exit_time': exit_time,
        })
        self.position = None
        self.equity = self.balance

    def _update_equity(self, current_price: float):
        pos = self.position
        if not pos:
            self.equity = self.balance
            return
        qty, entry = pos['qty'], pos['entry']
        if pos['side'] == 'Buy':
            unrealized = qty * (current_price - entry)
        else:
            unrealized = qty * (entry - current_price)
        self.equity = self.balance + unrealized


# ═══════════════════════════════════════════════════════════
# BB Calculation
# ═══════════════════════════════════════════════════════════

def calc_bb(closes: list[float], period: int = 20, std_mult: float = 2.0) -> dict | None:
    """Bollinger Bands на массиве close-цен."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    current = closes[-1]
    bb_width = ((upper - lower) / sma) * 100 if sma > 0 else 0
    bb_pos = ((current - lower) / (upper - lower)) * 100 if upper != lower else 50
    return {'upper': upper, 'lower': lower, 'sma': sma, 'current': current,
            'bb_width': bb_width, 'bb_pos': bb_pos, 'std': std}


def calc_atr(klines: list[dict], idx: int, period: int = 14) -> float:
    """ATR на исторических свечах."""
    if idx < period + 1:
        return 0
    trs = []
    for i in range(idx - period, idx):
        c = klines[i]
        pc = klines[i - 1]
        tr = max(c['high'] - c['low'],
                 abs(c['high'] - pc['close']),
                 abs(c['low'] - pc['close']))
        trs.append(tr)
    return sum(trs) / period if trs else 0


# ═══════════════════════════════════════════════════════════
# BB Scoring (упрощённый — без ML и funding rate)
# ═══════════════════════════════════════════════════════════

def score_coin(bb_data: dict, volume_24h: float, closes: list[float], side: str = 'Buy') -> dict | None:
    """Упрощённый 6-метричный скоринг (без ML Gate)."""
    bb_pos = bb_data['bb_pos']
    bb_width = bb_data['bb_width']

    # Для SHORT — зеркалим BB-позицию
    if side == 'Sell':
        bb_pos = 100 - bb_pos

    # 1. BB score
    if bb_pos <= 10: bb_score = 15
    elif bb_pos <= 25: bb_score = 12
    elif bb_pos <= 40: bb_score = 8
    elif bb_pos <= 60: bb_score = 5
    elif bb_pos <= 75: bb_score = 3
    else: bb_score = 1

    if bb_pos > 80:
        return None

    # 2. Volume score
    vol = volume_24h
    if vol < 1_000_000: return None
    if vol > 500_000_000: vol_score = 10
    elif vol > 100_000_000: vol_score = 8
    elif vol > 50_000_000: vol_score = 7
    elif vol > 20_000_000: vol_score = 6
    elif vol > 10_000_000: vol_score = 5
    elif vol > 5_000_000: vol_score = 4
    else: vol_score = 2

    # 3. Down/Up days
    if side == 'Buy':
        down = sum(1 for i in range(1, min(8, len(closes))) if closes[-i] < closes[-i - 1])
    else:
        down = sum(1 for i in range(1, min(8, len(closes))) if closes[-i] > closes[-i - 1])
    if down >= 5: trend_score = 10
    elif down >= 3: trend_score = 8
    elif down >= 2: trend_score = 5
    elif down >= 1: trend_score = 3
    else: trend_score = 1

    # 4. BB Width
    if 3 <= bb_width <= 8: vola_score = 5
    elif 1 <= bb_width < 3: vola_score = 3
    elif 8 < bb_width <= 15: vola_score = 3
    else: vola_score = 1

    # 5. Quality
    quality = (bb_pos / 100) * bb_width
    if quality <= 0.5: qscore = 5
    elif quality <= 1.5: qscore = 4
    elif quality <= 3.0: qscore = 3
    elif quality <= 5.0: qscore = 2
    else: qscore = 1

    total = bb_score + vol_score + trend_score + vola_score + qscore
    return {'score': total, 'bb_pos': round(bb_pos, 1), 'bb_width': round(bb_width, 1),
            'breakdown': {'bb': bb_score, 'vol': vol_score, 'trend': trend_score,
                          'vola': vola_score, 'quality': qscore}}


# ═══════════════════════════════════════════════════════════
# Paper Engine
# ═══════════════════════════════════════════════════════════

@dataclass
class PaperResult:
    symbol: str
    days: int
    interval: str
    initial_balance: float
    final_equity: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)


def run_backtest(symbol: str, days: int = 30, interval: str = 'D',
                 initial_balance: float = 1000, risk_pct: float = 5.0,
                 rr_ratio: float = 2.0) -> PaperResult:
    """Запустить бэктест Bollinger Grid на исторических данных."""
    end_ms = int(time.time() * 1000)
    start_ms = int((time.time() - days * 86400) * 1000)

    if interval in ('60', '120', '240'):
        per_day = 1440 // int(interval)
        limit = min(days * per_day, 1000)
    else:
        limit = min(days, 200)

    all_klines = []
    current = end_ms
    while current > start_ms:
        url = (f'/v5/market/kline?category=linear&symbol={symbol}'
               f'&interval={interval}&limit={min(limit, 200)}&end={current}')
        resp = bybit('GET', url)
        if resp.get('retCode') != 0:
            log_event(f'paper_trade: API error for {symbol}: {resp.get("retMsg")}')
            break
        batch = resp['result']['list']
        if not batch:
            break
        all_klines = batch + all_klines
        oldest = int(batch[-1][0])  # Bybit отдаёт список DESC: последний элемент — самая старая свеча
        current = oldest - 1
        if len(batch) < min(limit, 200) or oldest <= start_ms:
            break

    # Дедупликация + фильтр по start_ms (точный --days, без дублей)
    seen: set = set()
    deduped = []
    for k in all_klines:
        ts = int(k[0])
        if ts < start_ms:
            continue
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append(k)
    all_klines = deduped

    if not all_klines:
        raise ValueError(f'Нет свечей для {symbol}')

    # Парсим свечи
    klines = []
    for k in all_klines:
        klines.append({
            'open_time': int(k[0]),
            'open': float(k[1]),
            'high': float(k[2]),
            'low': float(k[3]),
            'close': float(k[4]),
            'volume': float(k[5]),
            'turnover': float(k[6]),
        })
    klines.sort(key=lambda x: x['open_time'])

    # Запускаем симуляцию
    exchange = PaperExchange(symbol, klines, initial_balance)
    closes: list[float] = []
    equity_curve = [initial_balance]

    warmup = 50
    for i in range(warmup, len(klines)):
        k = klines[i]
        exchange.advance(k)

        closes.append(k['close'])

        if exchange.position:
            equity_curve.append(exchange.equity)
            continue

        bb = calc_bb(closes)
        if not bb:
            equity_curve.append(exchange.equity)
            continue

        long_score = score_coin(bb, k['turnover'], closes, 'Buy')
        short_score = score_coin(bb, k['turnover'], closes, 'Sell')

        best = None
        best_side = None
        if long_score and long_score['score'] >= MIN_SCORE:
            best, best_side = long_score, 'Buy'
        if short_score and short_score['score'] >= MIN_SCORE:
            if not best or short_score['score'] > best['score']:
                best, best_side = short_score, 'Sell'

        if not best:
            equity_curve.append(exchange.equity)
            continue

        entry = k['close']
        atr = calc_atr(klines, i)
        risk_amount = exchange.balance * (risk_pct / 100)
        sl_distance = entry * DEFAULT_SL_PCT
        qty = risk_amount / sl_distance if sl_distance > 0 else 0

        if best_side == 'Buy':
            sl = entry * (1 - DEFAULT_SL_PCT)
            tp = entry * (1 + DEFAULT_SL_PCT * rr_ratio) if atr > 0 else None
            exchange.open_long(entry, sl, tp, qty, k['open_time'])
        else:
            sl = entry * (1 + DEFAULT_SL_PCT)
            tp = entry * (1 - DEFAULT_SL_PCT * rr_ratio) if atr > 0 else None
            exchange.open_short(entry, sl, tp, qty, k['open_time'])

        equity_curve.append(exchange.equity)

    # Закрываем последнюю позицию по цене последней свечи (EOD)
    if exchange.position:
        exchange._close_position(klines[-1]['close'], 'EOD', klines[-1]['open_time'])

    # Метрики
    trades = exchange.trades
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    total_pnl = exchange.equity - initial_balance
    total_pnl_pct = (exchange.equity / initial_balance - 1) * 100

    # Max drawdown
    peak = initial_balance
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    # Sharpe
    if len(equity_curve) > 2:
        returns = [equity_curve[i] / equity_curve[i - 1] - 1
                   for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
        avg_ret = mean(returns) if returns else 0
        std_ret = stdev(returns) if len(returns) > 1 else 0.0001
        interval_minutes = 1440 if interval == 'D' else int(interval)
        sharpe = (avg_ret / std_ret) * math.sqrt(365 * (1440 // interval_minutes)) if std_ret > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    gross_profit = sum(t['pnl_pct'] for t in wins)
    gross_loss = abs(sum(t['pnl_pct'] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    avg_win = mean([t['pnl_pct'] for t in wins]) if wins else 0
    avg_loss = mean([t['pnl_pct'] for t in losses]) if losses else 0

    return PaperResult(
        symbol=symbol, days=days, interval=interval,
        initial_balance=initial_balance, final_equity=round(exchange.equity, 2),
        total_trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate=round(win_rate, 1), total_pnl=round(total_pnl, 2),
        total_pnl_pct=round(total_pnl_pct, 2),
        max_drawdown_pct=round(max_dd, 2), sharpe_ratio=round(sharpe, 2),
        profit_factor=round(profit_factor, 2),
        avg_win_pct=round(avg_win, 2), avg_loss_pct=round(avg_loss, 2),
        trades=trades, equity_curve=equity_curve,
    )


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Paper Trading — бэктест Bollinger Grid')
    parser.add_argument('symbol', help='Тикер (SOLUSDT, BTCUSDT, ...)')
    parser.add_argument('--days', type=int, default=30, help='Глубина истории')
    parser.add_argument('--interval', default='D', help='D, 240, 60')
    parser.add_argument('--balance', type=float, default=1000, help='Депозит')
    parser.add_argument('--risk', type=float, default=5.0, help='Риск %')
    parser.add_argument('--rr', type=float, default=2.0, help='Risk/Reward')
    parser.add_argument('--json', action='store_true', help='JSON вывод')
    args = parser.parse_args()

    print(f'🔄 Бэктест {args.symbol} ({args.days}д, {args.interval})...')
    start = time.time()
    result = run_backtest(
        symbol=args.symbol, days=args.days, interval=args.interval,
        initial_balance=args.balance, risk_pct=args.risk, rr_ratio=args.rr,
    )
    elapsed = time.time() - start

    if args.json:
        out = {
            'symbol': result.symbol, 'days': result.days,
            'final_equity': result.final_equity,
            'total_trades': result.total_trades,
            'win_rate': result.win_rate,
            'total_pnl_pct': result.total_pnl_pct,
            'max_drawdown_pct': result.max_drawdown_pct,
            'sharpe_ratio': result.sharpe_ratio,
            'profit_factor': result.profit_factor,
            'trades': result.trades,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f'\n{"="*60}')
        print(f'  📊 {result.symbol} | {result.days}д | {result.interval}')
        print(f'{"="*60}')
        print(f'  Старт:      ${result.initial_balance:,.2f}')
        print(f'  Финиш:      ${result.final_equity:,.2f}')
        print(f'  PnL:        {result.total_pnl:+,.2f} ({result.total_pnl_pct:+.2f}%)')
        print(f'  Сделок:     {result.total_trades} ({result.wins}W / {result.losses}L)')
        print(f'  Винрейт:    {result.win_rate}%')
        print(f'  Profit фактор: {result.profit_factor}')
        print(f'  Sharpe:     {result.sharpe_ratio}')
        print(f'  Макс. просадка: {result.max_drawdown_pct}%')
        print(f'  Средний выигрыш: {result.avg_win_pct}%')
        print(f'  Средний проигрыш: {result.avg_loss_pct}%')
        print(f'  ⏱️ {elapsed:.1f}с')

        if result.trades:
            print(f'\n{"─"*60}')
            print(f'  Последние 5 сделок:')
            for t in result.trades[-5:]:
                emoji = '✅' if t['pnl'] > 0 else '🔴'
                entry_dt = datetime.fromtimestamp(t['entry_time'] / 1000).strftime('%d.%m')
                print(f'  {emoji} {entry_dt} {t["side"]} {t["entry"]:.4f}→{t["exit"]:.4f} '
                      f'{t["pnl"]:+.2f} ({t["pnl_pct"]:+.2f}%) [{t["reason"]}]')


if __name__ == '__main__':
    main()
