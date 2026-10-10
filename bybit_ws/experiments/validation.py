"""Validation gate — DSR + PBO + Permutation test for experiment tree.

Формальный gate переобучения, который должен пройти вариант ДО промоушена
в live. Три независимых теста:

  1. **Deflated Sharpe Ratio (DSR)** — поправка SR на количество проб.
     Bailey & Lopez de Prado (2014). Если испытано 585 вариантов параметров,
     «лучший» SR ~2.0 может быть статистически незначим.

  2. **Probability of Backtest Overfitting (PBO)** — CSCV-метод
     Bailey, Borwein, Lopez de Prado, Zhu (2017). Делит окно на блоки,
     перебирает все IS/OOS-сплиты и считает, как часто лучший in-sample
     вариант оказывается ниже медианы out-of-sample.

  3. **Permutation test (sign-flip)** — рандомно инвертирует знаки PnL,
     проверяя H₀ «средний PnL = 0». Единственная осмысленная перестановочная
     схема для Profit Factor (обычный shuffle инвариантен к PF).

Нормальные CDF/PPF реализованы через Abramowitz & Stegun:
  - norm_cdf: формула 7.1.26 (erf-аппроксимация, |ε| < 1.5×10⁻⁷)
  - norm_ppf: формула 26.2.23 (рациональная аппроксимация, |ε| < 4.5×10⁻⁴)

Использование:
    python3 -m bybit_ws.experiments.validation --symbol SOLUSDT --days 180 \
        --end-date 2026-09-15 --n-trials 585
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine  # noqa: E402
from bybit_ws.experiments.parallel_sandbox import DEFAULT_VARIANTS  # noqa: E402


# ── нормальные функции (Abramowitz & Stegun) ──────────────────

def norm_cdf(x: float) -> float:
    """Φ(x) — кумулятивная функция стандартного нормального распределения.

    Abramowitz & Stegun, формула 7.1.26: аппроксимация erf(x) через
    рациональный полином с |ε| < 1.5×10⁻⁷.
    Φ(x) = ½(1 + erf(x/√2)).
    """
    # erf via A&S 7.1.26
    ax = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    poly = t * (0.254829592
                + t * (-0.284496736
                       + t * (1.421413741
                              + t * (-1.453152027
                                     + t * 1.061405429))))
    erf_val = 1.0 - poly * math.exp(-ax * ax)
    if x < 0:
        erf_val = -erf_val
    return 0.5 * (1.0 + erf_val)


def norm_ppf(p: float) -> float:
    """Φ⁻¹(p) — квантильная функция стандартного нормального распределения.

    Abramowitz & Stegun, формула 26.2.23: рациональная аппроксимация
    с |ε| < 4.5×10⁻⁴ для p ∈ (0, 1).
    """
    if p <= 0.0 or p >= 1.0:
        return float("-inf") if p <= 0.0 else float("inf")
    if p == 0.5:
        return 0.0
    pp = p if p < 0.5 else 1.0 - p
    t = math.sqrt(-2.0 * math.log(pp))
    # A&S 26.2.23 coefficients
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    x = t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)
    return x if p < 0.5 else -x


# ── Function 1: Deflated Sharpe Ratio ─────────────────────────

def deflated_sharpe_ratio(
    returns: list[float],
    n_trials: int = 585,
    annualization: float = 365.0,
    benchmark_sr: float = 0.0,
) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    Parameters
    ----------
    returns : list[float]
        Периодические returns (per-bar).
    n_trials : int
        Количество испытанных вариантов параметров (поправка на multiple testing).
    annualization : float
        Коэффициент годовой нормировки (365 для дневных, 365*4 для 6-часовых, …).
    benchmark_sr : float
        Эталонный SR (по умолчанию 0 — проверяем «лучше нуля с поправкой»).

    Returns
    -------
    dict с ключами sr, expected_max_sr, dsr, skew, kurt, n_trials.
    """
    T = len(returns)
    if T < 2:
        return {"sr": 0.0, "dsr": 0.0, "expected_max_sr": 0.0,
                "skew": 0.0, "kurt": 0.0, "n_trials": n_trials}
    mu = mean(returns)
    sigma = stdev(returns)  # sample stdev (T-1 denominator)
    if sigma == 0.0:
        # Constant returns: SR = ±∞ depending on sign of μ.
        # If μ > 0: perfect edge (SR = +∞, DSR = 1.0)
        # If μ < 0: worst case (SR = -∞, DSR = 0.0)
        # If μ = 0: neutral (SR = 0, DSR = 0.5)
        if mu > 0:
            return {"sr": float("inf"), "dsr": 1.0, "expected_max_sr": 0.0,
                    "skew": 0.0, "kurt": 0.0, "n_trials": n_trials}
        elif mu < 0:
            return {"sr": float("-inf"), "dsr": 0.0, "expected_max_sr": 0.0,
                    "skew": 0.0, "kurt": 0.0, "n_trials": n_trials}
        return {"sr": 0.0, "dsr": 0.5, "expected_max_sr": 0.0,
                "skew": 0.0, "kurt": 0.0, "n_trials": n_trials}

    sr = (mu / sigma) * math.sqrt(annualization)

    # Skewness & kurtosis — population denominators (1/T), но sigma sample
    m3 = sum((r - mu) ** 3 for r in returns) / T
    m4 = sum((r - mu) ** 4 for r in returns) / T
    sigma3 = sigma ** 3
    sigma4 = sigma ** 4
    g3 = m3 / sigma3 if sigma3 != 0 else 0.0
    g4 = m4 / sigma4 if sigma4 != 0 else 0.0

    # Variance of SR estimator
    v_sr = (1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr) / (T - 1)

    # Expected max SR under n_trials independent trials
    gamma_em = 0.5772156649  # Euler–Mascheroni
    e = math.e
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * e))
    sqrt_v = math.sqrt(max(v_sr, 0.0))
    expected_max_sr = sqrt_v * ((1.0 - gamma_em) * z1 + gamma_em * z2)

    # DSR = P(SR > expected_max_sr | H0: true SR = benchmark)
    denom_sq = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0.0:
        dsr = 0.5 if sr >= expected_max_sr else 0.0
    else:
        z = (sr - max(expected_max_sr, benchmark_sr)) * math.sqrt(T - 1) / math.sqrt(denom_sq)
        dsr = norm_cdf(z)

    return {
        "sr": sr,
        "expected_max_sr": expected_max_sr,
        "dsr": dsr,
        "skew": g3,
        "kurt": g4,
        "n_trials": n_trials,
    }


# ── Function 2: Probability of Backtest Overfitting (CSCV) ────

def probability_of_backtest_overfitting(
    returns_matrix: list[list[float]],
    n_blocks: int = 16,
) -> Optional[float]:
    """PBO via Combinatorially Symmetric Cross-Validation (CSCV).

    Bailey, Borwein, Lopez de Prado, Zhu (2017). Для каждого сплита
    IS/OOS (перебор C(n_blocks, n_blocks//2)):
      — находим стратегию n* с лучшим IS Sharpe;
      — ранжируем её OOS Sharpe среди всех стратегий;
      — если ранг ≤ медианы (w ≤ 0.5) → случай переобучения.
    PBO = доля случаев переобучения.

    Parameters
    ----------
    returns_matrix : list[list[float]]
        Матрица returns: [n_strategies][n_obs].
    n_blocks : int
        Количество блоков для сплита (16 → C(16,8)=12870 комбинаций).

    Returns
    -------
    float ∈ [0, 1] или None если данных недостаточно.
    """
    S = len(returns_matrix)
    if S < 2:
        return None
    T = min(len(row) for row in returns_matrix)
    if T < 2 * n_blocks:
        return None

    block_size = T // n_blocks
    # blocks[i] = list of indices in block i
    blocks = [list(range(i * block_size, (i + 1) * block_size))
              for i in range(n_blocks)]

    half = n_blocks // 2
    n_overfit = 0
    n_combos = 0

    for chosen in itertools.combinations(range(n_blocks), half):
        chosen_set = set(chosen)
        is_idx: list[int] = []
        oos_idx: list[int] = []
        for b in range(n_blocks):
            if b in chosen_set:
                is_idx.extend(blocks[b])
            else:
                oos_idx.extend(blocks[b])

        # Per-strategy IS and OOS Sharpe (mean/std, annualization cancels)
        sr_is = []
        sr_oos = []
        for i in range(S):
            row = returns_matrix[i]
            # IS
            is_rets = [row[j] for j in is_idx]
            is_mu = mean(is_rets) if is_rets else 0.0
            is_sig = stdev(is_rets) if len(is_rets) > 1 else 0.0
            sr_is.append(is_mu / is_sig if is_sig > 0 else 0.0)
            # OOS
            oos_rets = [row[j] for j in oos_idx]
            oos_mu = mean(oos_rets) if oos_rets else 0.0
            oos_sig = stdev(oos_rets) if len(oos_rets) > 1 else 0.0
            sr_oos.append(oos_mu / oos_sig if oos_sig > 0 else 0.0)

        # n* = argmax IS Sharpe
        n_star = max(range(S), key=lambda i: sr_is[i])
        # Rank of n*'s OOS Sharpe among all OOS Sharpes
        rank = sum(1 for j in range(S) if sr_oos[j] <= sr_oos[n_star])
        w = rank / S

        if w <= 0.5:
            n_overfit += 1
        n_combos += 1

    return n_overfit / n_combos if n_combos > 0 else None


# ── Function 3: Permutation test (sign-flip) ──────────────────

def permutation_test(
    trade_pnls: list[float],
    n_permutations: int = 1000,
    seed: int = 42,
) -> dict:
    """Permutation test для Profit Factor (sign-flip scheme).

    H₀: средний PnL = 0 (нет эджа). Тестовая статистика — PF.
    Для каждой пермутации случайно инвертируем знаки PnL (единственная
    осмысленная перестановочная схема для PF, т.к. обычный shuffle
    инвариантен к порядку суммирования).

    Parameters
    ----------
    trade_pnls : list[float]
        PnL каждой сделки.
    n_permutations : int
        Количество пермутаций.
    seed : int
        Seed для воспроизводимости.

    Returns
    -------
    dict с observed_pf, p_value, n, n_permutations.
    """
    n = len(trade_pnls)
    if n == 0:
        return {"observed_pf": 0.0, "p_value": 1.0, "n": 0,
                "n_permutations": n_permutations}

    def _pf(pnls: list[float]) -> float:
        gp = sum(p for p in pnls if p > 0)
        gl = sum(p for p in pnls if p < 0)
        if gl == 0.0:
            return float("inf") if gp > 0 else 0.0
        return gp / abs(gl)

    observed = _pf(trade_pnls)
    rng = random.Random(seed)
    count = 0

    for _ in range(n_permutations):
        # Sign-flip: each pnl randomly keeps or flips sign
        shuffled = [p if rng.random() < 0.5 else -p for p in trade_pnls]
        pf_i = _pf(shuffled)
        if pf_i >= observed:
            count += 1

    p_value = count / n_permutations
    return {
        "observed_pf": observed,
        "p_value": p_value,
        "n": n,
        "n_permutations": n_permutations,
    }


# ── helpers ────────────────────────────────────────────────────

def _equity_to_returns(equity_curve: list[float]) -> list[float]:
    """Конвертировать equity curve в per-bar returns."""
    rets = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            rets.append(equity_curve[i] / equity_curve[i - 1] - 1.0)
    return rets


# ── CLI ────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Validation gate — DSR + PBO + Permutation для experiment tree")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, …)")
    p.add_argument("--days", type=int, default=180, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="Интервал свечей (D, 240, 60, …)")
    p.add_argument("--end-date", default=None, help="Конец окна YYYY-MM-DD")
    p.add_argument("--n-trials", type=int, default=585,
                   help="Количество испытанных вариантов (для DSR)")
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--n-blocks", type=int, default=16, help="Блоки для PBO (CSCV)")
    p.add_argument("--n-perms", type=int, default=1000, help="Пермутации для perm-test")
    p.add_argument("--no-db", action="store_true", help="Не писать в БД (по умолчанию)")
    args = p.parse_args(argv)

    # ── данные ─────────────────────────────────────────────────
    end_ms = engine.resolve_end_ms(args.end_date)
    start_ms = end_ms - args.days * 86400 * 1000
    print(f"📡 Fetching {args.symbol} {args.interval} klines: "
          f"{engine._fmt_ms(start_ms)} .. {engine._fmt_ms(end_ms)}")
    klines = engine.fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    print(f"   {len(klines)} свечей загружено (кэш: {engine.KLINE_CACHE_DIR})")

    # ── прогон вариантов ───────────────────────────────────────
    print(f"\n🧪 Прогон {len(DEFAULT_VARIANTS)} вариантов параметров…")
    results: list[dict] = []
    for v in DEFAULT_VARIANTS:
        name = v["name"]
        params = dict(engine.DEFAULT_PARAMS)
        params.update(v.get("params", {}))
        trades, equity = engine.simulate(klines, args.symbol, args.balance, params)
        metrics = engine.compute_metrics(trades, equity, args.balance, args.interval)
        rets = _equity_to_returns(equity)
        trade_pnls = [t["pnl"] for t in trades]
        results.append({
            "name": name,
            "params": params,
            "metrics": metrics,
            "returns": rets,
            "trade_pnls": trade_pnls,
            "trades": trades,
        })

    # ── таблица ────────────────────────────────────────────────
    results.sort(key=lambda r: r["metrics"]["total_pnl"], reverse=True)
    print(f"\n{'='*105}")
    print(f"  Validation Gate: {args.symbol} | {args.days}д | {args.interval} | "
          f"end={args.end_date or 'now'}")
    print(f"{'='*105}")
    hdr = (f"{'#':>2}  {'вариант':<20} {'сделок':>6} {'WR%':>7} {'PF':>7} "
           f"{'PnL$':>10} {'Sharpe':>7} {'MaxDD%':>8}")
    print(hdr)
    print("─" * 105)
    for i, r in enumerate(results):
        m = r["metrics"]
        pf = m["profit_factor"]
        pf_s = "∞" if pf is None and m["wins"] > 0 else (f"{pf:.2f}" if pf is not None else "0.00")
        print(f"{i+1:>2}  {r['name']:<20} {m['n_trades']:>6} {m['win_rate']:>6.2f}% "
              f"{pf_s:>7} {m['total_pnl']:>+10.2f} {m['sharpe_ratio']:>7.2f} "
              f"{m['max_drawdown_pct']:>7.2f}%")
    print("─" * 105)

    # ── лучший вариант ─────────────────────────────────────────
    best = results[0]
    print(f"\n🥇 Лучший по total_pnl: {best['name']}")
    print(f"   PnL: {best['metrics']['total_pnl']:+.4f} | "
          f"WR: {best['metrics']['win_rate']:.2f}% | "
          f"PF: {best['metrics']['profit_factor']}")

    # ── DSR ────────────────────────────────────────────────────
    best_rets = best["returns"]
    if len(best_rets) < 2:
        dsr_res = {"sr": 0.0, "dsr": 0.0, "expected_max_sr": 0.0,
                   "skew": 0.0, "kurt": 0.0, "n_trials": args.n_trials}
    else:
        dsr_res = deflated_sharpe_ratio(best_rets, n_trials=args.n_trials,
                                        annualization=365.0)
    print(f"\n📊 Deflated Sharpe Ratio (n_trials={args.n_trials}):")
    print(f"   SR:              {dsr_res['sr']:.4f}")
    print(f"   Expected max SR: {dsr_res['expected_max_sr']:.4f}")
    print(f"   DSR:             {dsr_res['dsr']:.4f}")
    print(f"   Skew:            {dsr_res['skew']:.4f}")
    print(f"   Kurtosis:        {dsr_res['kurt']:.4f}")

    # ── Permutation test ───────────────────────────────────────
    best_pnls = best["trade_pnls"]
    perm_res = permutation_test(best_pnls, n_permutations=args.n_perms, seed=42)
    print(f"\n🎲 Permutation test (sign-flip, n={args.n_perms}):")
    print(f"   Observed PF:     {perm_res['observed_pf']:.4f}"
          if perm_res["observed_pf"] != float("inf") else
          "   Observed PF:     ∞")
    print(f"   p-value:         {perm_res['p_value']:.4f}")
    print(f"   n trades:        {perm_res['n']}")

    # ── PBO ────────────────────────────────────────────────────
    returns_matrix = [r["returns"] for r in results]
    pbo_val = probability_of_backtest_overfitting(returns_matrix, n_blocks=args.n_blocks)
    print(f"\n🔬 PBO (CSCV, n_blocks={args.n_blocks}, "
          f"combos=C({args.n_blocks},{args.n_blocks//2})"
          f"={math.comb(args.n_blocks, args.n_blocks//2)}):")
    if pbo_val is not None:
        print(f"   PBO:             {pbo_val:.4f}")
    else:
        print(f"   PBO:             N/A (мало данных или <2 стратегий)")

    # ── VERDICT ────────────────────────────────────────────────
    dsr_ok = dsr_res["dsr"] >= 0.95
    perm_ok = perm_res["p_value"] <= 0.05
    pbo_ok = pbo_val is not None and pbo_val <= 0.5

    pbo_disp = f"{pbo_val:.4f}" if pbo_val is not None else "N/A"

    print(f"\n{'='*105}")
    print(f"  Thresholds:  DSR ≥ 0.95 ({'✅' if dsr_ok else '❌'} {dsr_res['dsr']:.4f})  |  "
          f"p ≤ 0.05 ({'✅' if perm_ok else '❌'} {perm_res['p_value']:.4f})  |  "
          f"PBO ≤ 0.5 ({'✅' if pbo_ok else '❌'} {pbo_disp})")
    if dsr_ok and perm_ok and pbo_ok:
        print("  VERDICT: EDGE (needs walk-forward)")
    else:
        print("  VERDICT: NO EDGE")
    print(f"{'='*105}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
