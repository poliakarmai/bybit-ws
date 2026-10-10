"""Triple-Barrier Labelling for experiment tree.

Метки Лопеса де Прадо (Advances in Financial ML) для сделок:
  +1 — TP (take-profit) достигнут первым
  -1 — SL (stop-loss) достигнут первым
   0 — вертикальный (time) барьер достигнут первым

Same-bar ambiguity: в каждой свече SL проверяется ПЕРЕД TP (консервативное
допущение — если оба уровня внутри диапазона одного бара, считаем что стоп
сработал первым). Это стандартная практика для бэктестов.

Использование:
    python3 -m bybit_ws.experiments.triple_barrier \\
        --symbol SOLUSDT --days 180 --end-date 2026-09-15 --vertical-bars 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine


# ---------------------------------------------------------------------------
# Function 1: label a single trade against the OHLCV path
# ---------------------------------------------------------------------------

def label_trade(
    entry_price: float,
    entry_time_ms: int,
    side: str,
    sl: float,
    tp: Optional[float],
    vertical_bars: int,
    klines: list[dict],
) -> dict:
    """Label one trade against the candle path (triple-barrier method).

    Parameters
    ----------
    entry_price : float
        Цена входа.
    entry_time_ms : int
        Время входа (мс UTC).
    side : str
        'Buy' (long) или 'Sell' (short).
    sl : float
        Stop-loss уровень.
    tp : float | None
        Take-profit уровень. Если None — TP-барьер отсутствует (только SL + time).
    vertical_bars : int
        Количество свечей после входа, формирующих временной (вертикальный) барьер.
    klines : list[dict]
        Свечи, отсортированные по open_time (ascending). Путь ПОСЛЕ входа.

    Returns
    -------
    dict: {label: int, exit_price: float, exit_time_ms: int, hit: str, pnl_pct: float}
        label: +1 (TP first), -1 (SL first), 0 (time barrier).
        hit: 'tp' | 'sl' | 'time'.
        pnl_pct: процент PnL (rounded to 6 decimals).

    Same-bar ambiguity
    ------------------
    В каждой свече SL проверяется ПЕРЕД TP. Если оба уровня внутри диапазона
    одного бара, предполагается что стоп сработал первым (консервативно).
    """
    fallback = {
        "label": 0,
        "exit_price": entry_price,
        "exit_time_ms": entry_time_ms,
        "hit": "time",
        "pnl_pct": 0.0,
    }

    # Find first kline with open_time >= entry_time_ms
    i0 = -1
    for idx, k in enumerate(klines):
        if k["open_time"] >= entry_time_ms:
            i0 = idx
            break

    if i0 < 0:
        return fallback

    end_idx = min(i0 + vertical_bars, len(klines))
    label = 0
    hit = "time"
    exit_price = entry_price
    exit_time_ms = entry_time_ms

    for k_idx in range(i0, end_idx):
        k = klines[k_idx]
        hi = k["high"]
        lo = k["low"]

        if side == "Buy":
            # LONG: SL below entry, TP above
            # Check SL first (conservative same-bar rule)
            if lo <= sl:
                exit_price = sl
                hit = "sl"
                label = -1
                exit_time_ms = k["open_time"]
                break
            if tp is not None and hi >= tp:
                exit_price = tp
                hit = "tp"
                label = 1
                exit_time_ms = k["open_time"]
                break
        else:
            # SHORT: SL above entry, TP below
            # Check SL first (conservative same-bar rule)
            if hi >= sl:
                exit_price = sl
                hit = "sl"
                label = -1
                exit_time_ms = k["open_time"]
                break
            if tp is not None and lo <= tp:
                exit_price = tp
                hit = "tp"
                label = 1
                exit_time_ms = k["open_time"]
                break

    if hit == "time":
        # Vertical barrier: exit at close of last bar in the window
        last_idx = min(i0 + vertical_bars - 1, len(klines) - 1)
        if last_idx >= i0:
            exit_price = klines[last_idx]["close"]
            exit_time_ms = klines[last_idx]["open_time"]
        else:
            exit_price = entry_price
            exit_time_ms = entry_time_ms

    # pnl_pct
    if entry_price > 0 and exit_price > 0:
        if side == "Buy":
            pnl_pct = (exit_price / entry_price - 1.0) * 100.0
        else:
            pnl_pct = (entry_price / exit_price - 1.0) * 100.0
    else:
        pnl_pct = 0.0

    return {
        "label": label,
        "exit_price": exit_price,
        "exit_time_ms": exit_time_ms,
        "hit": hit,
        "pnl_pct": round(pnl_pct, 6),
    }


# ---------------------------------------------------------------------------
# Function 2: label a batch of trades
# ---------------------------------------------------------------------------

def label_trades(
    trades: list[dict],
    klines: list[dict],
    vertical_bars: int = 30,
    sl_pct: float = 0.05,
    rr_ratio: float = 2.0,
) -> list[dict]:
    """Label a batch of trades from engine.simulate with triple-barrier method.

    Parameters
    ----------
    trades : list[dict]
        Сделки от engine.simulate. Каждая имеет ключи:
        side, entry, exit, pnl, pnl_pct, reason, entry_time, exit_time.
    klines : list[dict]
        Полный список свечей (open_time, open, high, low, close, volume, turnover).
    vertical_bars : int
        Количество свечей для time-барьера.
    sl_pct : float
        Процент стоп-лосса (для вывода SL/TP из entry).
    rr_ratio : float
        Risk/Reward ratio (для вывода TP из entry и sl_pct).

    Returns
    -------
    list[dict]: каждая сделка + поля {label, hit, tb_exit_price, tb_pnl_pct}.
    Trades where SL/TP cannot be determined are skipped (with a note in stderr).
    """
    results: list[dict] = []

    for t in trades:
        entry_price = t.get("entry")
        entry_time_ms = t.get("entry_time")
        side = t.get("side")

        if entry_price is None or entry_time_ms is None or side is None:
            print(f"  ⚠️  Skipping trade (missing fields): {t}", file=sys.stderr)
            continue

        # Derive SL/TP using same formulas as engine.simulate / paper_trade
        if side == "Buy":
            sl = entry_price * (1.0 - sl_pct)
            tp = entry_price * (1.0 + sl_pct * rr_ratio)
        elif side == "Sell":
            sl = entry_price * (1.0 + sl_pct)
            tp = entry_price * (1.0 - sl_pct * rr_ratio)
        else:
            print(f"  ⚠️  Skipping trade (unknown side '{side}'): {t}", file=sys.stderr)
            continue

        lbl = label_trade(
            entry_price=entry_price,
            entry_time_ms=entry_time_ms,
            side=side,
            sl=sl,
            tp=tp,
            vertical_bars=vertical_bars,
            klines=klines,
        )

        enriched = dict(t)
        enriched["label"] = lbl["label"]
        enriched["hit"] = lbl["hit"]
        enriched["tb_exit_price"] = lbl["exit_price"]
        enriched["tb_pnl_pct"] = lbl["pnl_pct"]
        results.append(enriched)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Triple-Barrier Labelling — López de Prado labels for experiment tree")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, …)")
    p.add_argument("--days", type=int, default=180, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="Интервал свечей (D, 240, 60, …)")
    p.add_argument("--end-date", default=None, help="Конец окна YYYY-MM-DD")
    p.add_argument("--balance", type=float, default=1000.0, help="Начальный депозит")
    p.add_argument("--vertical-bars", type=int, default=30,
                   help="Вертикальный барьер (количество свечей после входа)")
    args = p.parse_args(argv)

    # ---- Fetch klines ----
    end_ms = engine.resolve_end_ms(args.end_date)
    start_ms = end_ms - args.days * 86400 * 1000
    print(f"📡 Fetching {args.symbol} {args.interval} klines: "
          f"{engine._fmt_ms(start_ms)} .. {engine._fmt_ms(end_ms)}")
    klines = engine.fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    print(f"   {len(klines)} свечей загружено")

    # ---- Run two param variants ----
    variants = [
        ("default", dict(engine.DEFAULT_PARAMS)),
        ("variant_rr2_sl5", {
            **engine.DEFAULT_PARAMS,
            "rr_ratio": 2.0,
            "sl_pct": 0.05,
            "min_score": 20,
        }),
    ]

    for name, params in variants:
        print(f"\n{'='*90}")
        print(f"  Variant: {name}")
        print(f"  Params: sl_pct={params['sl_pct']}, rr_ratio={params['rr_ratio']}, "
              f"min_score={params['min_score']}")
        print(f"{'='*90}")

        trades, equity = engine.simulate(klines, args.symbol, args.balance, params)
        if not trades:
            print("   Нет сделок для labeling.")
            continue

        # Derive sl_pct / rr_ratio from the params used in simulate
        sl_pct = float(params.get("sl_pct", 0.05))
        rr_ratio = float(params.get("rr_ratio", 2.0))

        labelled = label_trades(
            trades, klines,
            vertical_bars=args.vertical_bars,
            sl_pct=sl_pct,
            rr_ratio=rr_ratio,
        )

        # ---- Aggregate label stats ----
        n_total = len(labelled)
        n_tp = sum(1 for t in labelled if t["label"] == 1)
        n_sl = sum(1 for t in labelled if t["label"] == -1)
        n_time = sum(1 for t in labelled if t["label"] == 0)

        pnl_tp = [t["tb_pnl_pct"] for t in labelled if t["label"] == 1]
        pnl_sl = [t["tb_pnl_pct"] for t in labelled if t["label"] == -1]
        pnl_time = [t["tb_pnl_pct"] for t in labelled if t["label"] == 0]

        avg_tp = mean(pnl_tp) if pnl_tp else 0.0
        avg_sl = mean(pnl_sl) if pnl_sl else 0.0
        avg_time = mean(pnl_time) if pnl_time else 0.0

        print(f"\n  Triple-Barrier Labels (vertical_bars={args.vertical_bars}):")
        print(f"  Total trades:  {n_total}")
        print(f"  label +1 (TP): {n_tp:>4}   avg pnl_pct: {avg_tp:>+8.4f}%")
        print(f"  label -1 (SL): {n_sl:>4}   avg pnl_pct: {avg_sl:>+8.4f}%")
        print(f"  label  0 (tm): {n_time:>4}   avg pnl_pct: {avg_time:>+8.4f}%")

        # ---- Cross-check: actual exit reasons vs TB labels ----
        reason_counts: dict[str, int] = {}
        for t in labelled:
            r = t.get("reason", "?")
            reason_counts[r] = reason_counts.get(r, 0) + 1

        print(f"\n  Cross-check: actual exit reasons from engine:")
        for r, cnt in sorted(reason_counts.items()):
            print(f"    {r:<6}: {cnt}")

        # Agreement analysis
        agree = 0
        disagree = 0
        for t in labelled:
            actual = t.get("reason", "")
            tb_hit = t["hit"]
            if actual == "TP" and tb_hit == "tp":
                agree += 1
            elif actual == "SL" and tb_hit == "sl":
                agree += 1
            elif actual == "EOD" and tb_hit == "time":
                agree += 1
            else:
                disagree += 1

        print(f"\n  Label vs actual exit agreement: "
              f"{agree}/{n_total} ({agree/n_total*100:.1f}%), "
              f"disagree: {disagree}")

        # ---- Sample: first 5 labelled trades ----
        print(f"\n  First 5 labelled trades:")
        print(f"  {'side':<5} {'entry':>10} {'tb_exit':>10} {'label':>6} "
              f"{'hit':<5} {'tb_pnl%':>10} {'actual':<6}")
        print(f"  {'─'*60}")
        for t in labelled[:5]:
            print(f"  {t['side']:<5} {t['entry']:>10.4f} "
                  f"{t['tb_exit_price']:>10.4f} {t['label']:>+6d} "
                  f"{t['hit']:<5} {t['tb_pnl_pct']:>+10.4f}% "
                  f"{t.get('reason', '?'):<6}")

    # ---- Synthetic sanity checks ----
    print(f"\n{'='*90}")
    print(f"  Synthetic sanity checks")
    print(f"{'='*90}")
    _run_sanity_checks()

    return 0


def _run_sanity_checks():
    """Two hardcoded sanity checks printed to stdout."""

    # Case 1: LONG, entry=100, sl=95, tp=110, vertical_bars=2
    # First kline low=94 → SL hit → label=-1, hit='sl'
    klines_1 = [
        {"open_time": 1000, "open": 100.0, "high": 103.0, "low": 94.0,
         "close": 96.0, "volume": 100, "turnover": 10000},
        {"open_time": 2000, "open": 96.0, "high": 102.0, "low": 95.0,
         "close": 101.0, "volume": 100, "turnover": 10000},
        {"open_time": 3000, "open": 101.0, "high": 108.0, "low": 99.0,
         "close": 107.0, "volume": 100, "turnover": 10000},
    ]
    r1 = label_trade(
        entry_price=100.0, entry_time_ms=1000, side="Buy",
        sl=95.0, tp=110.0, vertical_bars=2, klines=klines_1,
    )
    print(f"\n  Case 1 (LONG, SL in first bar):")
    print(f"    label={r1['label']}, hit='{r1['hit']}', "
          f"exit_price={r1['exit_price']}, pnl_pct={r1['pnl_pct']}")
    assert r1["label"] == -1, f"Expected -1, got {r1['label']}"
    assert r1["hit"] == "sl", f"Expected 'sl', got {r1['hit']}"
    print(f"    ✅ PASS")

    # Case 2: LONG, entry=100, sl=95, tp=110, vertical_bars=2
    # No barrier touched in 2 bars → label=0, hit='time'
    klines_2 = [
        {"open_time": 1000, "open": 100.0, "high": 103.0, "low": 97.0,
         "close": 101.0, "volume": 100, "turnover": 10000},
        {"open_time": 2000, "open": 101.0, "high": 104.0, "low": 98.0,
         "close": 102.0, "volume": 100, "turnover": 10000},
        {"open_time": 3000, "open": 102.0, "high": 112.0, "low": 93.0,
         "close": 110.0, "volume": 100, "turnover": 10000},
    ]
    r2 = label_trade(
        entry_price=100.0, entry_time_ms=1000, side="Buy",
        sl=95.0, tp=110.0, vertical_bars=2, klines=klines_2,
    )
    print(f"\n  Case 2 (LONG, no barrier in 2 bars → time):")
    print(f"    label={r2['label']}, hit='{r2['hit']}', "
          f"exit_price={r2['exit_price']}, pnl_pct={r2['pnl_pct']}")
    assert r2["label"] == 0, f"Expected 0, got {r2['label']}"
    assert r2["hit"] == "time", f"Expected 'time', got {r2['hit']}"
    # exit_price should be close of klines[i0 + vertical_bars - 1] = klines[0+2-1] = klines[1].close = 102.0
    assert r2["exit_price"] == 102.0, f"Expected 102.0, got {r2['exit_price']}"
    print(f"    ✅ PASS")


if __name__ == "__main__":
    raise SystemExit(main())
