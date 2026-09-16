"""Детерминированный backtest-движок для дерева экспериментов.

Обёртка над строительными блоками bybit_ws.paper_trade (PaperExchange, calc_bb,
calc_atr, score_coin) с двумя ключевыми отличиями от боевого paper_trade.run_backtest:

  1. **Фиксированное окно данных.** paper_trade берёт end_ms = time.time(),
     поэтому два прогона «с одинаковыми inputs» дают разные данные. Здесь end_ms
     задаётся явно (--end-date или детерминированный floor «сейчас»), а свечи
     кэшируются на диск по ключу (symbol, interval, start_ms, end_ms).

  2. **Кэш свечей.** Второй прогон с теми же inputs читает кэшированный массив
     свечей — это гарантирует *байт-в-байт* идентичные метрики (критерий
     воспроизводимости из ТЗ), даже если сеть недоступна или Bybit отдал чуть
     иной срез.

Симуляция детерминирована: чистый Python, без random, без ML, без живых ордеров.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Optional

# Пакет bybit_ws/__init__.py при импорте делает read-only GET (детект hedge-режима)
# и НЕ трогает state.db на запись. Это тот же read-путь, что и у paper_trade.
from bybit_ws.paper_trade import (  # noqa: E402
    DEFAULT_SL_PCT, MIN_SCORE, PaperExchange, calc_atr, calc_bb, score_coin,
)

REPO_ROOT = Path(__file__).resolve().parents[2]          # /home/openclaw/bybit-ws
DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws" / "experiments"
KLINE_CACHE_DIR = DATA_DIR / "klines_cache"
RUNS_DIR = DATA_DIR / "runs"

ENGINE_VERSION = "1.0"

# Параметры-дефолты (совпадают с боевыми paper_trade, чтобы baseline был сравним)
DEFAULT_PARAMS: dict[str, Any] = {
    "risk_pct": 5.0,
    "rr_ratio": 2.0,
    "sl_pct": DEFAULT_SL_PCT,
    "min_score": MIN_SCORE,
    "bb_period": 20,
    "bb_std_mult": 2.0,
}


# ── git / время ────────────────────────────────────────────────

def git_commit() -> Optional[str]:
    """Текущий HEAD коммита репозитория (для run-манифеста)."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        return out.decode().strip() or None
    except Exception:
        return None


def resolve_end_ms(end_date: Optional[str] = None) -> int:
    """Детерминированный end_ms окна данных (миллисекунды UTC).

    - end_date задан ('YYYY-MM-DD'): конец этого дня (00:00 следующего дня UTC),
      т.е. окно включает весь указанный день.
    - иначе: floor «сейчас» до часа. Кэш свечей всё равно делает прогон
      воспроизводимым при одинаковых inputs.
    """
    if end_date:
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        dt = (dt + timedelta(days=1)).replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    now = datetime.now(timezone.utc)
    floored = now.replace(minute=0, second=0, microsecond=0)
    return int(floored.timestamp() * 1000)


def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── данные ─────────────────────────────────────────────────────

def _cache_path(symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    return KLINE_CACHE_DIR / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int,
                 use_cache: bool = True) -> list[dict]:
    """Детерминированно получить свечи для окна [start_ms, end_ms).

    Возвращает отсортированные по времени dict'ы {open_time, open, high, low,
    close, volume, turnover}. Кэшируются на диск — это и есть гарантия
    воспроизводимости.
    """
    KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(symbol, interval, start_ms, end_ms)
    if use_cache and cp.exists():
        try:
            return json.loads(cp.read_text())
        except Exception:
            pass  # битый кэш → перекачать

    from bybit_ws.api import bybit

    batch_limit = 200
    all_klines: list[list] = []
    current = end_ms
    while current > start_ms:
        url = (f"/v5/market/kline?category=linear&symbol={symbol}"
               f"&interval={interval}&limit={batch_limit}&end={current}")
        resp = bybit("GET", url)
        if resp.get("retCode") != 0:
            raise RuntimeError(f"Bybit kline error for {symbol}: "
                               f"{resp.get('retMsg')} (retCode={resp.get('retCode')})")
        batch = resp["result"]["list"]
        if not batch:
            break
        all_klines = batch + all_klines
        oldest = int(batch[-1][0])  # Bybit отдаёт DESC: последний элемент — самый старый
        current = oldest - 1
        if oldest <= start_ms:
            break

    # Дедупликация + фильтр по нижней границе окна
    seen: set[int] = set()
    klines: list[dict] = []
    for k in all_klines:
        ts = int(k[0])
        if ts < start_ms or ts in seen:
            continue
        seen.add(ts)
        klines.append({
            "open_time": ts,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "turnover": float(k[6]),
        })
    klines.sort(key=lambda x: x["open_time"])

    if not klines:
        raise ValueError(f"Нет свечей для {symbol} в окне {_fmt_ms(start_ms)}..{_fmt_ms(end_ms)}")

    cp.write_text(json.dumps(klines))
    return klines


def data_hash(klines: list[dict]) -> str:
    """SHA-256 входных свечей — отпечаток данных в манифесте."""
    blob = json.dumps(klines, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


# ── симуляция ──────────────────────────────────────────────────

def simulate(klines: list[dict], symbol: str, initial_balance: float,
             params: dict) -> tuple[list[dict], list[float]]:
    """Детерминированный прогон Bollinger Grid на заданных свечах.

    Логика зеркалит paper_trade.run_backtest: вход на Lower BB по 6-метричному
    скорингу, выход по SL/TP, комиссия 0.055% + проскальзывание 0.05% (внутри
    PaperExchange). Параметры (sl_pct, rr_ratio, min_score, bb_period, ...)
    берутся из `params`.

    Возвращает (trades, equity_curve).
    """
    sl_pct = float(params.get("sl_pct", DEFAULT_SL_PCT))
    rr_ratio = float(params.get("rr_ratio", 2.0))
    risk_pct = float(params.get("risk_pct", 5.0))
    min_score = int(params.get("min_score", MIN_SCORE))
    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std_mult", 2.0))

    exchange = PaperExchange(symbol, klines, initial_balance)
    closes: list[float] = []
    equity_curve = [initial_balance]

    warmup = max(50, bb_period + 1)
    for i in range(warmup, len(klines)):
        k = klines[i]
        exchange.advance(k)
        closes.append(k["close"])

        if exchange.position:
            equity_curve.append(exchange.equity)
            continue

        bb = calc_bb(closes, period=bb_period, std_mult=bb_std)
        if not bb:
            equity_curve.append(exchange.equity)
            continue

        long_score = score_coin(bb, k["turnover"], closes, "Buy")
        short_score = score_coin(bb, k["turnover"], closes, "Sell")

        best, best_side = None, None
        if long_score and long_score["score"] >= min_score:
            best, best_side = long_score, "Buy"
        if short_score and short_score["score"] >= min_score:
            if not best or short_score["score"] > best["score"]:
                best, best_side = short_score, "Sell"

        if not best:
            equity_curve.append(exchange.equity)
            continue

        entry = k["close"]
        atr = calc_atr(klines, i)
        risk_amount = exchange.balance * (risk_pct / 100)
        sl_distance = entry * sl_pct
        qty = risk_amount / sl_distance if sl_distance > 0 else 0.0

        if best_side == "Buy":
            sl = entry * (1 - sl_pct)
            tp = entry * (1 + sl_pct * rr_ratio) if atr > 0 else None
            exchange.open_long(entry, sl, tp, qty, k["open_time"])
        else:
            sl = entry * (1 + sl_pct)
            tp = entry * (1 - sl_pct * rr_ratio) if atr > 0 else None
            exchange.open_short(entry, sl, tp, qty, k["open_time"])

        equity_curve.append(exchange.equity)

    # EOD: закрыть хвостовую позицию по цене последней свечи
    if exchange.position:
        exchange._close_position(klines[-1]["close"], "EOD", klines[-1]["open_time"])

    return exchange.trades, equity_curve


# ── метрики ────────────────────────────────────────────────────

def _finite(v: float) -> Optional[float]:
    return v if math.isfinite(v) else None


def compute_metrics(trades: list[dict], equity_curve: list[float],
                    initial_balance: float, interval: str = "D") -> dict:
    """WR, PF, avg_win, avg_loss, max_dd, n_trades (+ PnL, Sharpe, pct-версии)."""
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n_wins, n_losses = len(wins), len(losses)

    win_rate = n_wins / n * 100 if n else 0.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    avg_win = mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_loss = mean([t["pnl"] for t in losses]) if losses else 0.0
    avg_win_pct = mean([t["pnl_pct"] for t in wins]) if wins else 0.0
    avg_loss_pct = mean([t["pnl_pct"] for t in losses]) if losses else 0.0

    peak = initial_balance
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    final_equity = equity_curve[-1] if equity_curve else initial_balance
    total_pnl = final_equity - initial_balance
    total_pnl_pct = (final_equity / initial_balance - 1) * 100 if initial_balance else 0.0

    # Sharpe (годовой, по возвратам equity-кривой)
    sharpe = 0.0
    if len(equity_curve) > 2:
        rets = [equity_curve[i] / equity_curve[i - 1] - 1
                for i in range(1, len(equity_curve)) if equity_curve[i - 1] > 0]
        if len(rets) > 1:
            std_ret = stdev(rets)
            if std_ret > 0:
                interval_minutes = 1440 if interval == "D" else int(interval)
                sharpe = (mean(rets) / std_ret) * math.sqrt(365 * (1440 // interval_minutes))

    return {
        "n_trades": n,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": round(win_rate, 4),
        "profit_factor": _finite(profit_factor),
        "avg_win": round(avg_win, 6),
        "avg_loss": round(avg_loss, 6),
        "avg_win_pct": round(avg_win_pct, 4),
        "avg_loss_pct": round(avg_loss_pct, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "sharpe_ratio": round(sharpe, 4),
        "final_equity": round(final_equity, 4),
    }


# ── манифест ───────────────────────────────────────────────────

def build_manifest(params: dict, data_range: dict, commit: Optional[str],
                   kline_hash: str, timestamp_iso: str) -> dict:
    """Run-манифест: (params, data_range, git_commit, timestamp) + data_hash."""
    return {
        "engine_version": ENGINE_VERSION,
        "params": params,
        "data_range": data_range,
        "git_commit": commit,
        "timestamp": timestamp_iso,
        "data_hash": kline_hash,
    }


def write_run_file(run_id: int, record: dict) -> Path:
    """Материализовать run-запись в JSON-файл (осязаемый артефакт манифеста)."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"run_{run_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    return path
