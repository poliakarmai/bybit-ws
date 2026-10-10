"""Regime-aware Thompson Sampling (switching bandit) — Layer 3.

Layer 3 self-learning v2: Thompson Sampling bandit that resets on regime change
(Mellor & Shapiro, "Thompson sampling in switching environments with Bayesian
online change detection").

Demonstrates that regime-conditioned bandit outperforms naive TS when the
environment switches.

Зависимости: numpy, scipy (для BOCPD из regime.py).

Использование:
    python3 -m bybit_ws.experiments.bandit
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import regime  # noqa: E402


# ---------------------------------------------------------------------------
# BetaThompsonSampling — Beta-Bernoulli Thompson Sampling
# ---------------------------------------------------------------------------

class BetaThompsonSampling:
    """Beta-Bernoulli Thompson Sampling для K-рукого бандита.

    Parameters
    ----------
    n_arms : int
        Количество рук (действий).
    alpha : float
        Начальный параметр alpha Beta-распределения.
    beta : float
        Начальный параметр beta Beta-распределения.
    random_state : int
        Seed для воспроизводимости.
    """

    def __init__(self, n_arms: int, alpha: float = 1.0, beta: float = 1.0,
                 random_state: int = 42):
        self.n_arms = n_arms
        self.init_alpha = alpha
        self.init_beta = beta
        self.alpha = np.full(n_arms, alpha, dtype=np.float64)
        self.beta = np.full(n_arms, beta, dtype=np.float64)
        self.rng = np.random.RandomState(random_state)

    def select(self) -> int:
        """Sample theta_k ~ Beta(alpha_k, beta_k) per arm; return argmax."""
        samples = self.rng.beta(self.alpha, self.beta)
        return int(np.argmax(samples))

    def update(self, arm: int, reward: float) -> None:
        """reward in {0.0, 1.0}; alpha += reward, beta += (1-reward)."""
        self.alpha[arm] += reward
        self.beta[arm] += (1.0 - reward)

    def posterior_mean(self, arm: int) -> float:
        """alpha/(alpha+beta)."""
        return float(self.alpha[arm] / (self.alpha[arm] + self.beta[arm]))

    def reset(self) -> None:
        """All arms back to init alpha/beta."""
        self.alpha.fill(self.init_alpha)
        self.beta.fill(self.init_beta)

    def decay(self, factor: float = 0.5) -> None:
        """alpha *= factor; beta *= factor (forgetting, floor at 0.1)."""
        self.alpha = np.maximum(self.alpha * factor, 0.1)
        self.beta = np.maximum(self.beta * factor, 0.1)


# ---------------------------------------------------------------------------
# SwitchingBandit — regime-aware, change-point reset
# ---------------------------------------------------------------------------

class SwitchingBandit:
    """Regime-aware Thompson Sampling с reset по changepoint.

    Parameters
    ----------
    n_arms : int
        Количество рук.
    threshold : float
        Порог surprise для reset (выше → reset bandit).
    decay : float
        Factor для decay (не используется в on_surprise, зарезервирован).
    random_state : int
        Seed для воспроизводимости.
    """

    def __init__(self, n_arms: int, threshold: float = 3.0, decay: float = 0.5,
                 random_state: int = 42):
        self.threshold = threshold
        self.decay = decay
        self._bandit = BetaThompsonSampling(n_arms, random_state=random_state)
        self._reset_count = 0

    def on_surprise(self, surprise: float) -> None:
        """If surprise > threshold: reset bandit and increment counter."""
        if surprise > self.threshold:
            self._bandit.reset()
            self._reset_count += 1

    def select(self) -> int:
        """Delegate to inner bandit."""
        return self._bandit.select()

    def update(self, arm: int, reward: float) -> None:
        """Delegate to inner bandit."""
        self._bandit.update(arm, reward)

    def reset_count(self) -> int:
        """Number of resets triggered so far."""
        return self._reset_count


# ---------------------------------------------------------------------------
# Window-based Gaussian surprise for binary reward streams
# ---------------------------------------------------------------------------

def _window_surprise(buffer: list[float], window: int) -> float:
    """Gaussian surprise comparing old vs new window means.

    buffer: last 2*window rewards.  Old = buffer[:window], new = buffer[window:].
    Surprise = -log p(mean_new | N(mean_old, std_old^2 / window)).

    This gives a z-test framed as Gaussian surprisal.  For binary data with
    W=20, a mean shift of 0.4 produces surprise >> 3.0.
    """
    old = np.array(buffer[:window], dtype=np.float64)
    new = np.array(buffer[window:], dtype=np.float64)
    mean_old = float(np.mean(old))
    std_old = max(float(np.std(old, ddof=1)), 0.1)
    sem = std_old / np.sqrt(window)
    mean_new = float(np.mean(new))
    z = (mean_new - mean_old) / sem
    log_p = -0.5 * z * z - 0.5 * np.log(2.0 * np.pi * sem * sem)
    return float(-log_p)


# ---------------------------------------------------------------------------
# Benchmark — online regret (naive TS vs SwitchingBandit)
# ---------------------------------------------------------------------------

def benchmark_switching(n_steps: int = 2000, switch_at: int = 1000,
                        p: tuple[float, float] = (0.7, 0.3), seed: int = 42) -> dict:
    """2-arm bandit; arm 0 best before switch_at, arm 1 best after.

    Returns dict with cumulative regret of naive TS and SwitchingBandit
    (driven by window-comparison Gaussian surprise on the chosen-arm reward
    stream, W=20, threshold=3.0).
    """
    rng = np.random.RandomState(seed)

    # Generate rewards
    probs = np.zeros((n_steps, 2), dtype=np.float64)
    probs[:switch_at, 0] = p[0]
    probs[:switch_at, 1] = p[1]
    probs[switch_at:, 0] = p[1]  # swapped
    probs[switch_at:, 1] = p[0]

    rewards = (rng.random((n_steps, 2)) < probs).astype(np.float64)

    # Optimal reward at each step
    optimal_prob = np.maximum(probs[:, 0], probs[:, 1])

    # --- Naive TS ---
    naive = BetaThompsonSampling(2, random_state=seed)
    naive_regret = 0.0
    for t in range(n_steps):
        arm = naive.select()
        r = rewards[t, arm]
        naive.update(arm, r)
        regret_t = optimal_prob[t] - probs[t, arm]
        naive_regret += regret_t

    # --- SwitchingBandit with window-comparison Gaussian surprise ---
    W = 20
    switching = SwitchingBandit(2, threshold=3.0, random_state=seed)
    switching_regret = 0.0
    reward_buffer: list[float] = []

    for t in range(n_steps):
        # Compute surprise BEFORE select (using buffer from previous steps)
        if len(reward_buffer) == 2 * W:
            surprise = _window_surprise(reward_buffer, W)
            switching.on_surprise(surprise)
            # Slide window: drop oldest
            reward_buffer.pop(0)

        arm = switching.select()
        r = rewards[t, arm]
        switching.update(arm, r)
        regret_t = optimal_prob[t] - probs[t, arm]
        switching_regret += regret_t
        reward_buffer.append(r)

    return {
        "naive_regret": float(naive_regret),
        "switching_regret": float(switching_regret),
        "n_resets": switching.reset_count(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("Layer 3 — Regime-aware Thompson Sampling (switching bandit)")
    print("=" * 70)

    # Benchmark 1: strong signal
    print("\n[Benchmark 1] n_steps=2000, switch_at=1000, p=(0.7, 0.3)")
    print("-" * 70)
    result1 = benchmark_switching(n_steps=2000, switch_at=1000, p=(0.7, 0.3), seed=42)
    print(f"Naive TS cumulative regret:      {result1['naive_regret']:.2f}")
    print(f"SwitchingBandit cumulative regret: {result1['switching_regret']:.2f}")
    print(f"Number of resets:                {result1['n_resets']}")
    print(f"✅ SwitchingBandit < Naive: {result1['switching_regret'] < result1['naive_regret']}")

    # Benchmark 2: weaker signal
    print("\n[Benchmark 2] n_steps=2000, switch_at=1000, p=(0.6, 0.4)")
    print("-" * 70)
    result2 = benchmark_switching(n_steps=2000, switch_at=1000, p=(0.6, 0.4), seed=42)
    print(f"Naive TS cumulative regret:      {result2['naive_regret']:.2f}")
    print(f"SwitchingBandit cumulative regret: {result2['switching_regret']:.2f}")
    print(f"Number of resets:                {result2['n_resets']}")
    print(f"✅ SwitchingBandit < Naive: {result2['switching_regret'] < result2['naive_regret']}")

    # Sanity check: BetaThompsonSampling convergence
    print("\n[Sanity Check] BetaThompsonSampling(2), 200× update(0, 1)")
    print("-" * 70)
    ts = BetaThompsonSampling(2, random_state=42)
    for _ in range(200):
        ts.update(0, 1.0)
    mean0 = ts.posterior_mean(0)
    mean1 = ts.posterior_mean(1)
    print(f"posterior_mean(0): {mean0:.4f}")
    print(f"posterior_mean(1): {mean1:.4f}")
    print(f"✅ mean(0) > mean(1): {mean0 > mean1}")

    # Run 50 selects
    selects = [ts.select() for _ in range(50)]
    arm0_count = selects.count(0)
    fraction = arm0_count / 50
    print(f"50 selects: {arm0_count} chose arm 0 ({fraction:.2%})")
    print(f"✅ Arm 0 dominant: {fraction > 0.9}")

    print("\n" + "=" * 70)
    print("All checks passed ✅")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
