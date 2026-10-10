"""Meta-Labeling (Layer 4) — secondary model that filters BB-grid signals.

Bollinger Grid остаётся primary signal generator. LogReg-модель оценивает
P(win | features at entry) и фильтрует сигналы по вероятности успеха.
Это реализация идеи López de Prado «edge lives in the FILTER, not the entry
point»: meta-labeling не генерирует сигналы, а решает — брать или пропустить.

Pipeline:
  1. engine.simulate → trades (primary signals).
  2. build_features → 6 фичей на момент входа.
  3. triple_barrier.label_trade → label ∈ {+1, -1, 0} → binary: +1 → 1, else → 0.
  4. LogisticRegression (numpy + scipy.optimize L-BFGS-B).
  5. Фильтрация тестовых сделок по P(win) ≥ 0.5 → сравнение PF.

RSI: Wilder's smoothing (exponential moving average, α = 1/14).
Optimizer: scipy.optimize.minimize, method='L-BFGS-B', start at zeros.

Использование:
    python3 -m bybit_ws.experiments.meta_label \\
        --symbol SOLUSDT --days 180 --end-date 2026-09-15 --train-frac 0.7
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine
from bybit_ws.experiments.triple_barrier import label_trade


# ──────────────────────────────────────────────────────────────────────────────
# Feature builder
# ──────────────────────────────────────────────────────────────────────────────

def build_features(
    klines: list[dict],
    entry_idx: int,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> list[float]:
    """Features at bar entry_idx (needs >= bb_period bars of history).

    Returns list[float] of length 6.  All features computed from
    klines[0..entry_idx] inclusive using closes = [k['close']].

    RSI uses Wilder's smoothing (EMA with α = 1/14).

    Features
    --------
    1. pct_b      — (close - lower_bb) / (upper_bb - lower_bb); 0.5 if bands collapse.
    2. rsi        — 14-period RSI (Wilder), 0..100.
    3. realized_vol — std of last 14 log-returns.
    4. momentum_5 — log(close[t] / close[t-5]).
    5. vol_ratio  — close[t] / close[t-20] - 1  (20-bar return).
    6. dist_from_lower — (close - lower_bb) / lower_bb.
    """
    zeros = [0.0] * 6

    if entry_idx < bb_period or entry_idx >= len(klines):
        return zeros

    closes = [klines[i]["close"] for i in range(entry_idx + 1)]
    if len(closes) < max(bb_period, 21):
        return zeros

    close = closes[-1]

    # ── Bollinger Bands (population std, same as paper_trade.calc_bb) ──
    window = closes[-bb_period:]
    sma = sum(window) / bb_period
    variance = sum((x - sma) ** 2 for x in window) / bb_period
    std = math.sqrt(variance)
    upper_bb = sma + bb_std * std
    lower_bb = sma - bb_std * std

    band_range = upper_bb - lower_bb
    pct_b = (close - lower_bb) / band_range if band_range > 0 else 0.5
    dist_from_lower = (close - lower_bb) / lower_bb if lower_bb > 0 else 0.0

    # ── RSI-14 (Wilder's smoothing) ──
    rsi = _rsi_wilder(closes, period=14)

    # ── Realized vol: std of last 14 log-returns ──
    realized_vol = 0.0
    if len(closes) >= 15:
        log_rets = [
            math.log(closes[-(i)] / closes[-(i + 1)])
            for i in range(1, 15)
            if closes[-(i + 1)] > 0
        ]
        if len(log_rets) >= 2:
            realized_vol = float(np.std(log_rets))

    # ── Momentum 5-bar ──
    momentum_5 = 0.0
    if len(closes) >= 6 and closes[-6] > 0:
        momentum_5 = math.log(close / closes[-6])

    # ── Volume ratio (20-bar return) ──
    vol_ratio = 0.0
    if len(closes) >= 21 and closes[-21] > 0:
        vol_ratio = close / closes[-21] - 1.0

    result = [pct_b, rsi, realized_vol, momentum_5, vol_ratio, dist_from_lower]

    # NaN guard
    for i, v in enumerate(result):
        if not math.isfinite(v):
            result[i] = 0.0

    return result


def _rsi_wilder(closes: list[float], period: int = 14) -> float:
    """14-period RSI with Wilder's smoothing (α = 1/period).

    Returns value in 0..100.  Returns 50.0 if insufficient data.
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(deltas) < period:
        return 50.0

    # Seed: simple average of first `period` deltas
    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder EMA for remaining deltas
    alpha = 1.0 / period
    for d in deltas[period:]:
        g = max(d, 0.0)
        l = max(-d, 0.0)
        avg_gain = avg_gain * (1.0 - alpha) + g * alpha
        avg_loss = avg_loss * (1.0 - alpha) + l * alpha

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# ──────────────────────────────────────────────────────────────────────────────
# Logistic Regression (numpy + scipy, no sklearn)
# ──────────────────────────────────────────────────────────────────────────────

class LogisticRegression:
    """Binary logistic regression with L2 regularization.

    Standardizes features with train mean/std, adds intercept column.
    Optimizes ``-mean(y log p + (1-y) log(1-p)) + 0.5 * l2 * ||w||^2``
    via scipy L-BFGS-B starting at zeros.
    """

    def __init__(self):
        self.w: Optional[np.ndarray] = None
        self._mean: Optional[np.ndarray] = None
        self._std: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray, y: np.ndarray, l2: float = 1e-3,
            max_iter: int = 200) -> "LogisticRegression":
        """Fit model.  X: (N, F), y: (N,) in {0, 1}."""
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64).ravel()
        n, f = X.shape

        # Standardize
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0)
        self._std[self._std < 1e-12] = 1.0  # avoid div-by-zero
        Xs = (X - self._mean) / self._std

        # Add intercept column
        Xa = np.hstack([Xs, np.ones((n, 1))])  # (N, F+1)

        def loss_fn(w):
            z = Xa @ w
            # Numerically stable log-loss
            p = _sigmoid(z)
            p = np.clip(p, 1e-15, 1.0 - 1e-15)
            nll = -np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
            reg = 0.5 * l2 * np.dot(w, w)
            return nll + reg

        w0 = np.zeros(Xa.shape[1])
        result = minimize(loss_fn, w0, method="L-BFGS-B",
                          options={"maxiter": max_iter})
        self.w = result.x
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return P(y=1 | X) for each row.  X: (N, F)."""
        X = np.asarray(X, dtype=np.float64)
        Xs = (X - self._mean) / self._std
        Xa = np.hstack([Xs, np.ones((X.shape[0], 1))])
        return _sigmoid(Xa @ self.w)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    """Numerically stable sigmoid."""
    pos = z >= 0
    neg = ~pos
    out = np.empty_like(z)
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[neg])
    out[neg] = ez / (1.0 + ez)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _profit_factor(pnls: list[float]) -> float:
    """Gross profit / |gross loss|.  inf if no losses and profit > 0."""
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    if gl == 0.0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


def _find_kline_index(klines: list[dict], entry_time_ms: int) -> int:
    """Find kline index matching entry_time by open_time.  Returns -1 if not found."""
    for idx, k in enumerate(klines):
        if k["open_time"] == entry_time_ms:
            return idx
    # Fallback: find nearest kline with open_time <= entry_time
    best = -1
    for idx, k in enumerate(klines):
        if k["open_time"] <= entry_time_ms:
            best = idx
        else:
            break
    return best


# ──────────────────────────────────────────────────────────────────────────────
# Sanity check
# ──────────────────────────────────────────────────────────────────────────────

def _sanity_check():
    """Synthetic separable dataset: x ∈ [-3, 3], y = 1 if x > 0 else 0."""
    rng = np.random.RandomState(42)
    n = 200
    x_vals = rng.uniform(-3, 3, n)
    y_vals = (x_vals > 0).astype(np.float64)
    X = x_vals.reshape(-1, 1)

    model = LogisticRegression()
    model.fit(X, y_vals, l2=1e-3)
    probs = model.predict_proba(X)
    preds = (probs >= 0.5).astype(int)
    acc = float(np.mean(preds == y_vals))
    w = float(model.w[0])  # weight for x (after standardization)
    print(f"  Sanity: accuracy={acc:.4f}, weight_x={w:.4f}")
    assert acc >= 0.95, f"Sanity check failed: acc={acc:.4f} < 0.95"
    assert w > 0, f"Sanity check failed: weight should be positive, got {w:.4f}"
    print("  ✅ Sanity check PASSED")


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Meta-Labeling (Layer 4) — LogReg filter for BB-grid signals")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, …)")
    p.add_argument("--days", type=int, default=180, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="Интервал свечей")
    p.add_argument("--end-date", default=None, help="Конец окна YYYY-MM-DD")
    p.add_argument("--balance", type=float, default=1000.0, help="Начальный депозит")
    p.add_argument("--train-frac", type=float, default=0.7,
                   help="Доля данных для обучения (остальное — тест)")
    p.add_argument("--vertical-bars", type=int, default=30,
                   help="Вертикальный барьер для triple-barrier (свечей)")
    args = p.parse_args(argv)

    # ── 1. Fetch klines + simulate ──
    end_ms = engine.resolve_end_ms(args.end_date)
    start_ms = end_ms - args.days * 86400 * 1000
    print(f"📡 Fetching {args.symbol} {args.interval} klines: "
          f"{engine._fmt_ms(start_ms)} .. {engine._fmt_ms(end_ms)}")
    klines = engine.fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    print(f"   {len(klines)} свечей загружено")

    params = dict(engine.DEFAULT_PARAMS)
    params.update({"rr_ratio": 2.0, "sl_pct": 0.05, "min_score": 20})

    print(f"\n🔧 Simulate: sl_pct={params['sl_pct']}, rr_ratio={params['rr_ratio']}, "
          f"min_score={params['min_score']}")
    trades, equity = engine.simulate(klines, args.symbol, args.balance, params)
    print(f"   {len(trades)} сделок от primary signal (BB grid)")

    if not trades:
        print("   Нет сделок — нечего фильтровать.")
        return 0

    sl_pct = float(params["sl_pct"])
    rr_ratio = float(params["rr_ratio"])

    # ── 2. Build dataset ──
    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    pnl_rows: list[float] = []
    trade_refs: list[dict] = []  # keep reference to original trade

    for t in trades:
        entry_time_ms = t.get("entry_time")
        entry_price = t.get("entry")
        side = t.get("side")
        if entry_time_ms is None or entry_price is None or side is None:
            continue

        idx = _find_kline_index(klines, entry_time_ms)
        if idx < 0:
            continue

        try:
            feats = build_features(klines, idx, bb_period=params.get("bb_period", 20),
                                   bb_std=params.get("bb_std_mult", 2.0))
        except Exception:
            continue

        if any(not math.isfinite(v) for v in feats):
            continue

        # Reconstruct SL/TP from entry + params (same as triple_barrier.label_trades)
        if side == "Buy":
            sl = entry_price * (1.0 - sl_pct)
            tp = entry_price * (1.0 + sl_pct * rr_ratio)
        elif side == "Sell":
            sl = entry_price * (1.0 + sl_pct)
            tp = entry_price * (1.0 - sl_pct * rr_ratio)
        else:
            continue

        try:
            lbl_result = label_trade(
                entry_price=entry_price,
                entry_time_ms=entry_time_ms,
                side=side,
                sl=sl,
                tp=tp,
                vertical_bars=args.vertical_bars,
                klines=klines,
            )
        except Exception:
            continue

        # Map label: +1 (TP) → 1, else → 0
        label_binary = 1 if lbl_result["label"] == 1 else 0

        X_rows.append(feats)
        y_rows.append(label_binary)
        pnl_rows.append(t["pnl"])
        trade_refs.append(t)

    n_dataset = len(X_rows)
    print(f"\n📊 Dataset: {n_dataset} сделок с фичами и метками (из {len(trades)} total)")

    if n_dataset == 0:
        print("   Нет данных для обучения.")
        return 0

    # ── 3. Chronological split ──
    split = int(n_dataset * args.train_frac)
    n_train = split
    n_test = n_dataset - split

    if n_train < 20 or n_test < 5:
        print(f"   not enough data: train={n_train} (need ≥20), test={n_test} (need ≥5)")
        return 0

    X = np.array(X_rows)
    y = np.array(y_rows)

    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    pnl_test = pnl_rows[split:]

    print(f"   Feature count: {X.shape[1]}")
    print(f"   Train: {n_train}, Test: {n_test}")
    print(f"   Train positive rate: {y_train.mean():.3f}")

    # ── 4. Train + evaluate ──
    model = LogisticRegression()
    model.fit(X_train, y_train)

    probs = model.predict_proba(X_test)
    preds = (probs >= 0.5).astype(int)

    accuracy = float(np.mean(preds == y_test))
    kept_mask = probs >= 0.5
    n_kept = int(kept_mask.sum())
    kept_frac = n_kept / n_test if n_test > 0 else 0.0

    pf_no_filter = _profit_factor(pnl_test)
    pnl_filtered = [pnl_test[i] for i in range(n_test) if kept_mask[i]]
    pf_filtered = _profit_factor(pnl_filtered) if pnl_filtered else 0.0

    # Mean predicted proba for TP vs non-TP
    tp_mask = y_test == 1
    if tp_mask.any():
        mean_proba_tp = float(probs[tp_mask].mean())
    else:
        mean_proba_tp = float("nan")
    non_tp_mask = y_test == 0
    if non_tp_mask.any():
        mean_proba_non_tp = float(probs[non_tp_mask].mean())
    else:
        mean_proba_non_tp = float("nan")

    meta_helps = pf_filtered > pf_no_filter

    # ── 5. Report ──
    print(f"\n{'='*70}")
    print(f"  META-LABELING RESULTS: {args.symbol} | {args.days}d | "
          f"end={args.end_date or 'now'}")
    print(f"{'='*70}")
    print(f"  Dataset size:     {n_dataset}")
    print(f"  Feature count:    {X.shape[1]}")
    print(f"  Train / Test:     {n_train} / {n_test}")
    print(f"  PF (no filter):   {_fmt_pf(pf_no_filter)}")
    print(f"  PF (filtered):    {_fmt_pf(pf_filtered)}")
    print(f"  Kept fraction:    {kept_frac:.2%} ({n_kept}/{n_test})")
    print(f"  Accuracy (0.5):   {accuracy:.4f}")
    print(f"  Mean P(TP):       {mean_proba_tp:.4f}  (TP-labelled rows)")
    print(f"  Mean P(non-TP):   {mean_proba_non_tp:.4f}  (non-TP rows)")
    print(f"  META-LABEL HELPS: {meta_helps}")
    print(f"{'='*70}")

    # ── Sanity checks ──
    print(f"\n{'='*70}")
    print(f"  Synthetic sanity check")
    print(f"{'='*70}")
    _sanity_check()

    return 0


def _fmt_pf(v: float) -> str:
    if v == float("inf"):
        return "∞"
    return f"{v:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
