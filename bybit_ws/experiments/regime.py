"""Regime detection — Gaussian HMM + BOCPD for experiment tree.

Layer 2 self-learning v2: сегментация рыночного режима, чтобы bandit обучался
внутри стационарных сегментов.

  1. **GaussianHMM** — Baum-Welch EM (Rabiner 1989, scaled forward-backward)
     с диагональной ковариацией. Viterbi-декодирование в log-домене.

  2. **BOCPD** — Bayesian Online Changepoint Detection (Adams & MacKay 2007)
     с Normal-Inverse-Gamma сопряжённым априорным. Предиктив — Student-t.

Зависимости: numpy, scipy (scipy.stats.t для Student-t PDF).

Использование:
    python3 -m bybit_ws.experiments.regime \\
        --symbol SOLUSDT --days 180 --end-date 2026-09-15 --n-states 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import t as student_t

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine  # noqa: E402


# ---------------------------------------------------------------------------
# GaussianHMM — диагональная ковариация, scaled forward-backward, Viterbi
# ---------------------------------------------------------------------------

class GaussianHMM:
    """Gaussian HMM с диагональной ковариацией (K состояний, D признаков).

    Parameters
    ----------
    n_states : int
        Количество скрытых состояний.
    n_iter : int
        Максимум итераций Baum-Welch EM.
    tol : float
        Порог сходимости по изменению log-likelihood.
    random_state : int
        Seed для инициализации.
    """

    def __init__(self, n_states: int = 3, n_iter: int = 100,
                 tol: float = 1e-4, random_state: int = 42):
        self.n_states = n_states
        self.n_iter = n_iter
        self.tol = tol
        self.random_state = random_state
        self.pi: np.ndarray = np.empty(0)
        self.A: np.ndarray = np.empty((0, 0))
        self.means: np.ndarray = np.empty((0, 0))
        self.covars: np.ndarray = np.empty((0, 0))

    # ── Gaussian density (log-domain) ──────────────────────────

    @staticmethod
    def _log_gaussian(X: np.ndarray, means: np.ndarray,
                      covars: np.ndarray) -> np.ndarray:
        """Log N(X; means[k], diag(covars[k])) for each state k.

        Parameters
        ----------
        X : (T, D)
        means : (K, D)
        covars : (K, D) — diagonal variances

        Returns
        -------
        log_prob : (T, K)
        """
        T, D = X.shape
        K = means.shape[0]
        log_prob = np.zeros((T, K))
        for k in range(K):
            diff = X - means[k]
            mahal = np.sum(diff ** 2 / covars[k], axis=1)
            norm_const = np.sum(np.log(covars[k]) + np.log(2 * np.pi))
            log_prob[:, k] = -0.5 * (mahal + norm_const)
        return log_prob

    # ── Forward (scaled) ───────────────────────────────────────

    def _forward(self, X: np.ndarray):
        """Scaled forward probabilities.

        Returns
        -------
        alpha : (T, K) scaled forward probs
        scales : (T,) scale factors c_t (P(O|λ) = prod_t c_t)
        """
        T = X.shape[0]
        K = self.n_states
        log_B = self._log_gaussian(X, self.means, self.covars)
        B = np.exp(log_B)

        alpha = np.zeros((T, K))
        scales = np.zeros(T)

        alpha[0] = self.pi * B[0]
        scales[0] = alpha[0].sum()
        if scales[0] > 0:
            alpha[0] /= scales[0]

        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ self.A) * B[t]
            scales[t] = alpha[t].sum()
            if scales[t] > 0:
                alpha[t] /= scales[t]

        return alpha, scales

    # ── Backward (scaled) ──────────────────────────────────────

    def _backward(self, X: np.ndarray, scales: np.ndarray) -> np.ndarray:
        """Scaled backward probabilities."""
        T = X.shape[0]
        K = self.n_states
        log_B = self._log_gaussian(X, self.means, self.covars)
        B = np.exp(log_B)

        beta = np.zeros((T, K))
        beta[T - 1] = 1.0

        for t in range(T - 2, -1, -1):
            tmp = B[t + 1] * beta[t + 1]
            beta[t] = self.A @ tmp
            if scales[t + 1] > 0:
                beta[t] /= scales[t + 1]

        return beta

    # ── Score (log-likelihood) ─────────────────────────────────

    def score(self, X: np.ndarray) -> float:
        """Log-likelihood log P(X | model) via scaled forward.

        Rabiner (1989): P(O|λ) = prod_t c_t, поэтому
        log P(O|λ) = sum_t log(c_t).
        """
        _, scales = self._forward(X)
        log_lik = np.sum(np.log(np.maximum(scales, 1e-300)))
        return float(log_lik)

    # ── Fit (Baum-Welch EM) ────────────────────────────────────

    def fit(self, X: np.ndarray) -> "GaussianHMM":
        """Baum-Welch EM. X: (T, D)."""
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        T, D = X.shape
        K = self.n_states
        rng = np.random.RandomState(self.random_state)

        # Init: pi uniform
        self.pi = np.full(K, 1.0 / K)

        # Init: A = 0.9*I + 0.1/K
        self.A = np.full((K, K), 0.1 / K)
        np.fill_diagonal(self.A, 0.9 + 0.1 / K)
        self.A /= self.A.sum(axis=1, keepdims=True)

        # Init: means from K distinct percentile-based points
        percentiles = np.linspace(0, 100, K + 2)[1:-1]
        self.means = np.zeros((K, D))
        for d in range(D):
            self.means[:, d] = np.percentile(X[:, d], percentiles)

        # Init: covars = global variance (with floor)
        global_var = np.var(X, axis=0) + 1e-6
        self.covars = np.tile(global_var, (K, 1))

        prev_ll = -np.inf

        for _iteration in range(self.n_iter):
            # E-step
            alpha, scales = self._forward(X)
            beta = self._backward(X, scales)

            ll = self.score(X)
            if abs(ll - prev_ll) < self.tol:
                break
            prev_ll = ll

            # Gamma: state posterior
            gamma = alpha * beta
            gamma_sum = gamma.sum(axis=1, keepdims=True)
            gamma_sum = np.maximum(gamma_sum, 1e-300)
            gamma /= gamma_sum

            # Xi: transition posterior
            log_B = self._log_gaussian(X, self.means, self.covars)
            B = np.exp(log_B)

            xi = np.zeros((T - 1, K, K))
            for t in range(T - 1):
                for i in range(K):
                    xi[t, i, :] = (alpha[t, i] * self.A[i, :]
                                   * B[t + 1, :] * beta[t + 1, :])
                xi_sum = xi[t].sum()
                if xi_sum > 0:
                    xi[t] /= xi_sum

            # M-step
            self.pi = gamma[0].copy()
            pi_sum = self.pi.sum()
            if pi_sum > 0:
                self.pi /= pi_sum

            gamma_sum_xi = gamma[:-1].sum(axis=0)
            for i in range(K):
                if gamma_sum_xi[i] > 1e-300:
                    self.A[i] = xi[:, i, :].sum(axis=0) / gamma_sum_xi[i]
                a_sum = self.A[i].sum()
                if a_sum > 0:
                    self.A[i] /= a_sum

            gamma_total = gamma.sum(axis=0)
            for i in range(K):
                if gamma_total[i] > 1e-300:
                    self.means[i] = (gamma[:, i:i + 1].T @ X)[0] / gamma_total[i]

            for i in range(K):
                if gamma_total[i] > 1e-300:
                    diff = X - self.means[i]
                    self.covars[i] = (gamma[:, i:i + 1] * diff ** 2).sum(axis=0) / gamma_total[i]
            self.covars = np.maximum(self.covars, 1e-6)

        return self

    # ── Viterbi ────────────────────────────────────────────────

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Viterbi most-likely state sequence. X: (T, D). Returns (T,) int."""
        X = np.asarray(X, dtype=np.float64)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        T = X.shape[0]
        K = self.n_states

        log_pi = np.log(np.maximum(self.pi, 1e-300))
        log_A = np.log(np.maximum(self.A, 1e-300))
        log_B = self._log_gaussian(X, self.means, self.covars)

        delta = np.zeros((T, K))
        psi = np.zeros((T, K), dtype=int)

        delta[0] = log_pi + log_B[0]

        for t in range(1, T):
            for j in range(K):
                tmp = delta[t - 1] + log_A[:, j]
                psi[t, j] = int(np.argmax(tmp))
                delta[t, j] = tmp[psi[t, j]] + log_B[t, j]

        states = np.zeros(T, dtype=int)
        states[T - 1] = int(np.argmax(delta[T - 1]))
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]

        return states


# ---------------------------------------------------------------------------
# BOCPD — Bayesian Online Changepoint Detection (Adams & MacKay 2007)
# ---------------------------------------------------------------------------

def bocpd(observations: np.ndarray, hazard: float = 0.01,
          mu0: float = 0.0, kappa0: float = 1.0,
          alpha0: float = 1.0, beta0: float = 1.0) -> dict:
    """Bayesian Online Changepoint Detection с NIG-сопряжённым априорным.

    Parameters
    ----------
    observations : 1D np.ndarray
        Временной ряд (например, log-returns).
    hazard : float
        H (вероятность changepoint на каждом шаге). λ = 1/hazard — ожидаемая
        длина сегмента.
    mu0, kappa0, alpha0, beta0 : float
        Параметры NIG априорного для нового сегмента.

    Returns
    -------
    dict с ключами:
        changepoint_prob : (T,) — predictive surprise = -log p(x_t | x_{<t}).
            Высокие значения → наблюдение удивительно → вероятный changepoint.
        run_length : (T,) — argmax run-length posterior
        run_length_posterior : list[np.ndarray] — полные posterior per timestep

    Notes
    -----
    С геометрическим hazard нормализованная P(r_t=0 | x_{1:t}) тождественно
    равна H (математическое тождество), поэтому она бесполезна как сигнал.
    Вместо этого используем predictive probability p(x_t | x_{<t}):
    резкое падение означает что наблюдение не объясняется текущим режимом.
    Берём -log для удобства (surprisal в битах).
    """
    obs = np.asarray(observations, dtype=np.float64).ravel()
    T = len(obs)

    changepoint_prob = np.zeros(T)
    run_length = np.zeros(T, dtype=int)
    run_length_posterior: list[np.ndarray] = []

    mu_arr = np.array([mu0])
    kappa_arr = np.array([kappa0])
    alpha_arr = np.array([alpha0])
    beta_arr = np.array([beta0])

    R = np.array([1.0])

    for t_idx in range(T):
        x = obs[t_idx]
        n_runs = len(R)

        # 1. Predictive: Student-t for each current run-length
        dof = 2.0 * alpha_arr
        var_pred = beta_arr * (kappa_arr + 1.0) / (alpha_arr * kappa_arr)
        sigma_pred = np.sqrt(np.maximum(var_pred, 1e-300))
        prob_pred = student_t.pdf(x, dof, loc=mu_arr, scale=sigma_pred)

        # Predictive density p(x_t | x_{<t}) = sum_r R[r] * p(x_t | r)
        predictive = float(np.sum(R * prob_pred))
        # Surprisal: -log(p). Higher = more surprising = more likely changepoint.
        changepoint_prob[t_idx] = -np.log(max(predictive, 1e-300))

        # 2. Growth probabilities
        growth = R * prob_pred * (1.0 - hazard)

        # 3. Changepoint probability
        cp_mass = np.sum(R * prob_pred) * hazard

        # 4. New run-length posterior
        new_R = np.zeros(n_runs + 1)
        new_R[0] = cp_mass
        new_R[1:] = growth

        total = new_R.sum()
        if total > 0:
            new_R /= total

        # 5. Update NIG params for growth paths (r -> r+1)
        kappa_new = kappa_arr + 1.0
        mu_new = (kappa_arr * mu_arr + x) / kappa_new
        alpha_new = alpha_arr + 0.5
        beta_new = beta_arr + (kappa_arr * (x - mu_arr) ** 2) / (2.0 * kappa_new)

        # Changepoint path (r=0): reset to prior, then update with x
        kappa_cp = kappa0 + 1.0
        mu_cp = (kappa0 * mu0 + x) / kappa_cp
        alpha_cp = alpha0 + 0.5
        beta_cp = beta0 + (kappa0 * (x - mu0) ** 2) / (2.0 * kappa_cp)

        mu_arr = np.concatenate([[mu_cp], mu_new])
        kappa_arr = np.concatenate([[kappa_cp], kappa_new])
        alpha_arr = np.concatenate([[alpha_cp], alpha_new])
        beta_arr = np.concatenate([[beta_cp], beta_new])

        R = new_R
        run_length[t_idx] = int(np.argmax(R))
        run_length_posterior.append(R.copy())

    return {
        "changepoint_prob": changepoint_prob,
        "run_length": run_length,
        "run_length_posterior": run_length_posterior,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_ms_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Regime detection — Gaussian HMM + BOCPD for experiment tree")
    p.add_argument("--symbol", required=True, help="Тикер (SOLUSDT, BTCUSDT, …)")
    p.add_argument("--days", type=int, default=180, help="Глубина истории (дней)")
    p.add_argument("--interval", default="D", help="Интервал свечей (D, 240, 60, …)")
    p.add_argument("--end-date", default=None, help="Конец окна YYYY-MM-DD")
    p.add_argument("--n-states", type=int, default=3, help="Количество HMM-состояний")
    args = p.parse_args(argv)

    # ── данные ─────────────────────────────────────────────────
    end_ms = engine.resolve_end_ms(args.end_date)
    start_ms = end_ms - args.days * 86400 * 1000
    print(f"📡 Fetching {args.symbol} {args.interval} klines: "
          f"{engine._fmt_ms(start_ms)} .. {engine._fmt_ms(end_ms)}")
    klines = engine.fetch_klines(args.symbol, args.interval, start_ms, end_ms)
    print(f"   {len(klines)} свечей загружено (кэш: {engine.KLINE_CACHE_DIR})")

    # ── фичи ───────────────────────────────────────────────────
    closes = np.array([k["close"] for k in klines], dtype=np.float64)
    open_times = [k["open_time"] for k in klines]

    log_close = np.log(closes)
    log_return = np.diff(log_close)  # shape (T-1,)

    # realized_vol = rolling std of log_return over 14 bars
    window = 14
    realized_vol = np.full(len(log_return), np.nan)
    for i in range(window - 1, len(log_return)):
        realized_vol[i] = np.std(log_return[i - window + 1:i + 1], ddof=1)
    first_valid = realized_vol[window - 1]
    for i in range(window - 1):
        realized_vol[i] = first_valid

    X = np.column_stack([log_return, realized_vol])
    feat_times = open_times[1:]

    print(f"\n🧮 Features: log_return + realized_vol (14-bar rolling std)")
    print(f"   X shape: {X.shape}")

    # ── HMM ────────────────────────────────────────────────────
    model = GaussianHMM(n_states=args.n_states)
    model.fit(X)
    states = model.predict(X)
    ll = model.score(X)

    print(f"\n🔀 Gaussian HMM ({args.n_states} states, log-lik={ll:.4f})")
    print(f"{'='*80}")

    state_stats = []
    for s in range(args.n_states):
        mask = states == s
        count = int(mask.sum())
        if count > 0:
            mean_lr = float(np.mean(log_return[mask]))
            mean_rv = float(np.mean(realized_vol[mask]))
        else:
            mean_lr = 0.0
            mean_rv = 0.0
        state_stats.append({"state": s, "count": count,
                            "mean_lr": mean_lr, "mean_rv": mean_rv})

    sorted_states = sorted(state_stats, key=lambda x: x["mean_lr"], reverse=True)
    if args.n_states >= 3:
        labels = ["up", "range", "down"] + [f"s{i}" for i in range(3, args.n_states)]
    elif args.n_states == 2:
        labels = ["up", "down"]
    else:
        labels = ["single"]

    state_label_map = {}
    for rank, ss in enumerate(sorted_states):
        lbl = labels[rank] if rank < len(labels) else f"s{rank}"
        state_label_map[ss["state"]] = lbl

    print(f"  {'state':>5}  {'label':<6}  {'bars':>5}  {'mean_log_ret':>13}  {'mean_vol':>10}")
    print(f"  {'─'*50}")
    for ss in sorted_states:
        lbl = state_label_map[ss["state"]]
        print(f"  {ss['state']:>5}  {lbl:<6}  {ss['count']:>5}  "
              f"{ss['mean_lr']:>+13.4f}  {ss['mean_rv']:>10.4f}")

    print(f"\n  Transition matrix A:")
    for i in range(args.n_states):
        row = "  ".join(f"{model.A[i, j]:.4f}" for j in range(args.n_states))
        print(f"    [{row}]")

    # ── BOCPD ──────────────────────────────────────────────────
    print(f"\n📊 BOCPD (hazard=0.01, NIG prior: μ₀=0, κ₀=1, α₀=1, β₀=1)")
    print(f"{'='*80}")
    bocpd_result = bocpd(log_return, hazard=0.01)

    cp_prob = bocpd_result["changepoint_prob"]
    rl = bocpd_result["run_length"]

    top5_idx = np.argsort(cp_prob)[::-1][:5]
    print(f"  Top 5 changepoints (by predictive surprise, -log p):")
    print(f"  {'rank':>4}  {'idx':>5}  {'date':<12}  {'surprise':>9}  {'log_ret':>9}")
    print(f"  {'─'*47}")
    for rank, idx in enumerate(top5_idx):
        date_str = _fmt_ms_date(feat_times[idx]) if idx < len(feat_times) else "N/A"
        print(f"  {rank + 1:>4}  {idx:>5}  {date_str:<12}  "
              f"{cp_prob[idx]:>9.4f}  {log_return[idx]:>+9.4f}")

    mean_rl = float(np.mean(rl))
    print(f"\n  Mean run-length: {mean_rl:.4f}")
    print(f"  Max run-length:  {int(np.max(rl))}")

    # ── sanity checks ──────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  Synthetic sanity checks")
    print(f"{'='*80}")
    _run_sanity_checks()

    return 0


def _run_sanity_checks():
    """HMM + BOCPD on a synthetic 2-regime series."""
    rng = np.random.RandomState(42)
    seg1 = rng.normal(0, 1, 200)
    seg2 = rng.normal(5, 1, 200)
    series = np.concatenate([seg1, seg2])
    X_synth = series.reshape(-1, 1)

    # ── HMM sanity ─────────────────────────────────────────────
    print(f"\n  HMM on 2-regime series (200×N(0,1) + 200×N(5,1)):")
    hmm = GaussianHMM(n_states=2, n_iter=100, random_state=42)
    hmm.fit(X_synth)
    states = hmm.predict(X_synth)

    learned_means = sorted(hmm.means.flatten())
    print(f"    Learned means: [{learned_means[0]:.4f}, {learned_means[1]:.4f}]")
    print(f"    Expected:      [~0, ~5]")

    state_before = states[199]
    state_after = states[200]
    flip_idx = -1
    for i in range(200, 400):
        if states[i] != state_before:
            flip_idx = i
            break

    print(f"    State at idx 199: {state_before}, at idx 200: {state_after}")
    print(f"    First flip after 200: index {flip_idx}")
    assert abs(learned_means[0]) < 1.0, f"Mean ~0 too far: {learned_means[0]}"
    assert abs(learned_means[1] - 5.0) < 1.0, f"Mean ~5 too far: {learned_means[1]}"
    assert flip_idx >= 195 and flip_idx <= 210, f"Flip too far from 200: {flip_idx}"
    print(f"    ✅ PASS")

    # ── BOCPD sanity ───────────────────────────────────────────
    print(f"\n  BOCPD on same series:")
    result = bocpd(series, hazard=0.01)
    cp_prob = result["changepoint_prob"]
    peak_idx = int(np.argmax(cp_prob))
    print(f"    Changepoint_prob argmax (surprise peak): index {peak_idx}")
    print(f"    Expected: ~200")
    assert abs(peak_idx - 200) < 15, f"BOCPD peak too far from 200: {peak_idx}"
    print(f"    ✅ PASS")


if __name__ == "__main__":
    raise SystemExit(main())
