"""Trade Journal Analyzer.

Портирован из Vibe-Trading (HKUDS, MIT License).
Адаптирован под bybit-ws (SQLite trade_history, Bybit REST).

Анализирует историю сделок и выдаёт:
  - Профиль: holding time, win rate, PnL ratio, drawdown, топ-символы
  - 4 bias-диагностики: disposition effect, overtrading, chasing, anchoring
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Нормализованная сделка."""
    symbol: str
    side: str          # "buy" или "sell"
    quantity: float
    price: float
    fee: float = 0.0
    timestamp: float = 0.0   # Unix timestamp
    market: str = "crypto"   # всегда crypto для bybit
    order_id: str = ""


@dataclass
class RoundTrip:
    """Закрытый round-trip (buy → sell)."""
    symbol: str
    buy_ts: float
    sell_ts: float
    qty: float
    buy_price: float
    sell_price: float
    hold_hours: float
    pnl: float
    pnl_pct: float


@dataclass
class JournalProfile:
    """Профиль трейдинга."""
    total_trades: int = 0
    total_roundtrips: int = 0
    avg_hold_hours: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    top_symbols: list[dict[str, Any]] = field(default_factory=list)
    roundtrips_sample: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "total_roundtrips": self.total_roundtrips,
            "avg_hold_hours": round(self.avg_hold_hours, 1),
            "win_rate": round(self.win_rate, 3),
            "profit_loss_ratio": round(self.profit_loss_ratio, 2),
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "top_symbols": self.top_symbols,
            "roundtrips_sample": self.roundtrips_sample[:5],
        }


@dataclass
class BiasReport:
    """Один bias-диагноз."""
    name: str
    severity: str       # "low", "medium", "high"
    evidence: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "evidence": self.evidence,
            **self.metrics,
        }


# ── Helpers ─────────────────────────────────────────────────────────────────

def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def _severity(score: float, med: float, high: float) -> str:
    if score >= high:
        return "high"
    if score >= med:
        return "medium"
    return "low"


# ── FIFO matching ───────────────────────────────────────────────────────────

def pair_trades_fifo(trades: list[Trade]) -> list[RoundTrip]:
    """FIFO-матчинг buy↔sell для расчёта round-trip PnL.
    
    Поддерживает LONG (buy→sell) и SHORT (sell→buy).
    """
    long_queues: dict[str, deque] = defaultdict(deque)   # buy opens → sell closes
    short_queues: dict[str, deque] = defaultdict(deque)  # sell opens → buy closes
    roundtrips: list[RoundTrip] = []

    for t in sorted(trades, key=lambda x: x.timestamp):
        if t.side == "buy":
            # Buy может ЗАКРЫВАТЬ SHORT или ОТКРЫВАТЬ LONG
            sq = short_queues[t.symbol]
            if sq:
                # Закрываем SHORT: sell открыл → buy закрывает
                remaining = t.quantity
                while remaining > 1e-9 and sq:
                    lot = sq[0]
                    take = min(lot["qty"], remaining)
                    hold_h = (t.timestamp - lot["ts"]) / 3600.0 if t.timestamp > lot["ts"] else 0.0
                    # SHORT PnL: sell_price - buy_price (обратный знак)
                    gross = (lot["price"] - t.price) * take
                    open_fee = lot["fee"] * (take / lot["qty"]) if lot["qty"] else 0.0
                    close_fee = t.fee * (take / t.quantity) if t.quantity else 0.0
                    pnl = gross - open_fee - close_fee
                    cost = lot["price"] * take
                    pnl_pct = pnl / cost if cost else 0.0
                    roundtrips.append(RoundTrip(
                        symbol=t.symbol,
                        buy_ts=lot["ts"],   # open (sell) timestamp
                        sell_ts=t.timestamp,  # close (buy) timestamp
                        qty=take,
                        buy_price=lot["price"],
                        sell_price=t.price,
                        hold_hours=round(hold_h, 2),
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 4),
                    ))
                    lot["qty"] -= take
                    remaining -= take
                    if lot["qty"] <= 1e-9:
                        sq.popleft()
                if remaining > 1e-9:
                    # Остаток → открывает LONG
                    long_queues[t.symbol].append({
                        "ts": t.timestamp, "qty": remaining,
                        "price": t.price, "fee": t.fee * (remaining / t.quantity) if t.quantity else 0.0,
                    })
            else:
                # Нет открытых SHORT → открываем LONG
                long_queues[t.symbol].append({
                    "ts": t.timestamp, "qty": t.quantity,
                    "price": t.price, "fee": t.fee,
                })
        else:
            # Sell может ЗАКРЫВАТЬ LONG или ОТКРЫВАТЬ SHORT
            lq = long_queues[t.symbol]
            if lq:
                # Закрываем LONG: buy открыл → sell закрывает
                remaining = t.quantity
                while remaining > 1e-9 and lq:
                    lot = lq[0]
                    take = min(lot["qty"], remaining)
                    hold_h = (t.timestamp - lot["ts"]) / 3600.0 if t.timestamp > lot["ts"] else 0.0
                    gross = (t.price - lot["price"]) * take
                    buy_fee = lot["fee"] * (take / lot["qty"]) if lot["qty"] else 0.0
                    sell_fee = t.fee * (take / t.quantity) if t.quantity else 0.0
                    pnl = gross - buy_fee - sell_fee
                    cost = lot["price"] * take
                    pnl_pct = pnl / cost if cost else 0.0
                    roundtrips.append(RoundTrip(
                        symbol=t.symbol,
                        buy_ts=lot["ts"],
                        sell_ts=t.timestamp,
                        qty=take,
                        buy_price=lot["price"],
                        sell_price=t.price,
                        hold_hours=round(hold_h, 2),
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 4),
                    ))
                    lot["qty"] -= take
                    remaining -= take
                    if lot["qty"] <= 1e-9:
                        lq.popleft()
                if remaining > 1e-9:
                    # Остаток → открывает SHORT
                    short_queues[t.symbol].append({
                        "ts": t.timestamp, "qty": remaining,
                        "price": t.price, "fee": t.fee * (remaining / t.quantity) if t.quantity else 0.0,
                    })
            else:
                # Нет открытых LONG → открываем SHORT
                short_queues[t.symbol].append({
                    "ts": t.timestamp, "qty": t.quantity,
                    "price": t.price, "fee": t.fee,
                })
    return roundtrips


# ── Profile computation ─────────────────────────────────────────────────────

def compute_profile(trades: list[Trade]) -> JournalProfile:
    """Строит торговый профиль."""
    if not trades:
        return JournalProfile()

    rts = pair_trades_fifo(trades)
    if not rts:
        return JournalProfile(total_trades=len(trades))

    ts_list = [t.timestamp for t in trades]
    span_hours = max(1, (max(ts_list) - min(ts_list)) / 3600.0)

    wins = [rt for rt in rts if rt.pnl > 0]
    losses = [rt for rt in rts if rt.pnl < 0]

    win_rate = len(wins) / len(rts) if rts else 0.0
    avg_win = sum(w.pnl for w in wins) / len(wins) if wins else 0.0
    avg_loss = sum(l.pnl for l in losses) / len(losses) if losses else 0.0
    pnl_ratio = abs(avg_win / avg_loss) if avg_loss else (float("inf") if avg_win else 0.0)
    avg_hold = sum(rt.hold_hours for rt in rts) / len(rts) if rts else 0.0
    total_pnl = sum(rt.pnl for rt in rts)

    # Max drawdown from cumulative PnL
    rts_sorted = sorted(rts, key=lambda x: x.sell_ts)
    cum = 0.0
    running_max = 0.0
    max_dd = 0.0
    for rt in rts_sorted:
        cum += rt.pnl
        running_max = max(running_max, cum)
        max_dd = min(max_dd, cum - running_max)

    # Top symbols
    sym_stats: dict[str, dict[str, Any]] = {}
    for rt in rts:
        if rt.symbol not in sym_stats:
            sym_stats[rt.symbol] = {"trades": 0, "pnl": 0.0}
        sym_stats[rt.symbol]["trades"] += 1
        sym_stats[rt.symbol]["pnl"] += rt.pnl
    top_symbols = sorted(
        [{"symbol": k, "trades": v["trades"], "pnl": round(v["pnl"], 2)}
         for k, v in sym_stats.items()],
        key=lambda x: abs(float(x["pnl"])), reverse=True
    )[:10]

    sample = [
        {"symbol": rt.symbol, "buy_ts": rt.buy_ts, "sell_ts": rt.sell_ts,
         "qty": rt.qty, "buy_price": rt.buy_price, "sell_price": rt.sell_price,
         "hold_hours": rt.hold_hours, "pnl": rt.pnl, "pnl_pct": rt.pnl_pct}
        for rt in rts[:5]
    ]

    return JournalProfile(
        total_trades=len(trades),
        total_roundtrips=len(rts),
        avg_hold_hours=avg_hold,
        win_rate=win_rate,
        profit_loss_ratio=pnl_ratio if not math.isinf(pnl_ratio) else 99.0,
        total_pnl=total_pnl,
        max_drawdown=max_dd,
        top_symbols=top_symbols,
        roundtrips_sample=sample,
    )


# ── Bias diagnostics ────────────────────────────────────────────────────────

def check_disposition(rts: list[RoundTrip]) -> BiasReport:
    """Disposition effect: держим убыточные дольше прибыльных?"""
    wins = [rt for rt in rts if rt.pnl > 0]
    losses = [rt for rt in rts if rt.pnl < 0]

    if not wins or not losses:
        return BiasReport(
            name="disposition_effect",
            severity="low",
            evidence="Недостаточно прибыльных и убыточных сделок для сравнения",
        )

    win_hold = sum(w.hold_hours for w in wins) / len(wins)
    loss_hold = sum(l.hold_hours for l in losses) / len(losses)
    ratio = loss_hold / win_hold if win_hold > 0 else float("inf")

    sev = _severity(ratio, 1.2, 1.5)
    return BiasReport(
        name="disposition_effect",
        severity=sev,
        evidence=(
            f"Убыточные держатся {loss_hold:.1f}ч vs прибыльные {win_hold:.1f}ч "
            f"(ratio {ratio:.2f}). "
            + ("Классический disposition-паттерн." if sev == "high"
               else "Лёгкая тенденция держать убыточные дольше." if sev == "medium"
               else "Время удержания симметрично.")
        ),
        metrics={"ratio_loss_to_win_hold": round(ratio, 2),
                 "avg_winner_hold_hours": round(win_hold, 1),
                 "avg_loser_hold_hours": round(loss_hold, 1)},
    )


def check_overtrading(trades: list[Trade], rts: list[RoundTrip]) -> BiasReport:
    """Overtrading: в дни с кучей сделок PnL хуже чем в тихие?"""
    if len(trades) < 10:
        return BiasReport(name="overtrading", severity="low",
                          evidence="Слишком мало сделок для анализа")

    # Группируем по дням
    daily_counts: dict[str, int] = {}
    for t in trades:
        day = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_counts[day] = daily_counts.get(day, 0) + 1

    if len(daily_counts) < 4:
        return BiasReport(name="overtrading", severity="low",
                          evidence="Меньше 4 торговых дней")

    sorted_days = sorted(daily_counts.values())
    busy_cut = sorted_days[int(len(sorted_days) * 0.75)]
    quiet_cut = sorted_days[int(len(sorted_days) * 0.25)]
    busy_days = {d for d, c in daily_counts.items() if c >= busy_cut}
    quiet_days = {d for d, c in daily_counts.items() if c <= quiet_cut}

    busy_pnl = []
    quiet_pnl = []
    for rt in rts:
        day = datetime.fromtimestamp(rt.sell_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if day in busy_days:
            busy_pnl.append(rt.pnl)
        elif day in quiet_days:
            quiet_pnl.append(rt.pnl)

    if not busy_pnl or not quiet_pnl:
        return BiasReport(name="overtrading", severity="low",
                          evidence="Сделки не распределены по загруженным/тихим дням")

    busy_avg = sum(busy_pnl) / len(busy_pnl)
    quiet_avg = sum(quiet_pnl) / len(quiet_pnl)

    gap = quiet_avg - busy_avg
    base = abs(quiet_avg) if quiet_avg != 0 else 1.0
    sev = _severity(gap / base, 0.3, 1.0) if busy_avg < quiet_avg else "low"

    return BiasReport(
        name="overtrading",
        severity=sev,
        evidence=(
            f"Занятые дни (≥{busy_cut} сделок): avg PnL {busy_avg:+.1f}$; "
            f"тихие дни (≤{quiet_cut}): avg PnL {quiet_avg:+.1f}$. "
            + ("Высокая активность вредит доходности." if sev == "high"
               else "Есть просадка от занятых дней." if sev == "medium"
               else "Уровень активности не влияет на PnL.")
        ),
        metrics={"busy_day_avg_pnl": round(busy_avg, 2),
                 "quiet_day_avg_pnl": round(quiet_avg, 2),
                 "busy_day_threshold": busy_cut},
    )


def check_chasing(trades: list[Trade]) -> BiasReport:
    """Chasing momentum: покупаем после роста цены в том же символе?"""
    buys = [t for t in trades if t.side == "buy"]
    buys.sort(key=lambda x: (x.symbol, x.timestamp))

    if len(buys) < 5:
        return BiasReport(name="chasing_momentum", severity="low",
                          evidence="Слишком мало покупок для анализа")

    # Группируем покупки по символам, ищем последовательные с ростом >3%
    sym_prices: dict[str, list[float]] = defaultdict(list)
    for b in buys:
        sym_prices[b.symbol].append(b.price)

    chased = 0
    evaluated = 0
    for prices in sym_prices.values():
        for i in range(3, len(prices)):
            evaluated += 1
            if prices[i] > prices[i-3] * 1.03:
                chased += 1

    if evaluated == 0:
        return BiasReport(name="chasing_momentum", severity="low",
                          evidence="Недостаточно повторных покупок для оценки chasing")

    ratio = chased / evaluated
    sev = _severity(ratio, 0.4, 0.6)
    return BiasReport(
        name="chasing_momentum",
        severity=sev,
        evidence=(
            f"{chased}/{evaluated} покупок ({ratio:.0%}) — после роста >3% "
            f"в том же символе. "
            + ("Сильный chasing-паттерн." if sev == "high"
               else "Небольшая склонность к chasing." if sev == "medium"
               else "Нет выраженного chasing.")
        ),
        metrics={"chase_ratio": round(ratio, 3), "buys_evaluated": evaluated},
    )


def check_anchoring(trades: list[Trade]) -> BiasReport:
    """Price anchoring: залипаем в узком ценовом диапазоне?"""
    sym_groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        sym_groups[t.symbol].append(t.price)

    anchored = 0
    total = 0
    for prices in sym_groups.values():
        if len(prices) < 5:
            continue
        total += 1
        mean_p = sum(prices) / len(prices)
        variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
        cv = math.sqrt(variance) / mean_p if mean_p else 0.0
        if cv < 0.05:
            anchored += 1

    if total == 0:
        return BiasReport(name="anchoring", severity="low",
                          evidence="Нет символов с ≥5 сделками для оценки")

    ratio = anchored / total
    sev = _severity(ratio, 0.33, 0.66)
    return BiasReport(
        name="anchoring",
        severity=sev,
        evidence=(
            f"{anchored}/{total} символов ({ratio:.0%}) торгуются в узком "
            f"диапазоне (CV < 0.05). "
            + ("Сильный anchoring." if sev == "high"
               else "Умеренный anchoring." if sev == "medium"
               else "Ценовая диверсификация OK.")
        ),
        metrics={"anchored_symbols_ratio": round(ratio, 3), "symbols_evaluated": total},
    )


# ── Full analysis ───────────────────────────────────────────────────────────

def analyze(trades: list[Trade]) -> dict[str, Any]:
    """Полный анализ торговой истории.

    Returns:
        {"profile": {...}, "biases": [...], "alerts": [...]}
    """
    profile = compute_profile(trades)
    rts = pair_trades_fifo(trades)

    biases = [
        check_disposition(rts),
        check_overtrading(trades, rts),
        check_chasing(trades),
        check_anchoring(trades),
    ]

    alerts = []
    for b in biases:
        if b.severity in ("medium", "high"):
            alerts.append(f"[{b.severity.upper()}] {b.name}: {b.evidence[:120]}")

    if profile.win_rate < 0.4 and profile.total_roundtrips >= 10:
        alerts.append(f"[WARN] Низкий win rate: {profile.win_rate:.0%}")

    if profile.max_drawdown < -profile.total_pnl * 0.5 and profile.total_pnl > 0:
        alerts.append(f"[WARN] Большая просадка: ${profile.max_drawdown:.0f}")

    return {
        "profile": profile.to_dict(),
        "biases": [b.to_dict() for b in biases],
        "alerts": alerts,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
