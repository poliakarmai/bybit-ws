"""
Position Sizing — динамический расчёт маржи от % депозита.

Принцип: депозит = страховка на кросс-марже.
Входим малой долей, остальное страхует от ликвидации.

Формула:
  риск-бюджет = депозит × risk_pct
  база = риск-бюджет / max_positions
  маржа = база × score_multiplier
"""

import os, json, time, math
from .alerts import log_event
from .api import bybit

# === Константы ===
DEFAULT_RISK_PCT = 0.20       # 20% депозита в риске (LONG/SHORT 3x)
X10_RISK_PCT = 0.05           # 5% депозита в риске (x10 стратегии)
MAX_POSITIONS = 5             # макс одновременных позиций
MIN_MARGIN = 5.0              # пол: Bybit $5 минимум
MAX_POSITION_SHARE = 0.40     # потолок: не более 40% бюджета на одну позицию
MIN_DEPOSIT = 30.0            # мин депозит для торговли

DEPOSIT_CACHE = {"value": None, "ts": 0}
DEPOSIT_CACHE_TTL = 120       # кеш на 2 минуты


def _score_multiplier(score: float) -> float:
    """Множитель маржи от скора (уверенность → больше маржа)."""
    if score >= 8.5:
        return 1.4
    elif score >= 7.5:
        return 1.15
    elif score >= 6.5:
        return 1.0
    elif score >= 5.5:
        return 0.75
    return 0  # ниже 5.5 — не входим


def get_deposit() -> float:
    """Текущий USDT баланс с Bybit (с кешем на 2 мин)."""
    now = time.time()
    if (DEPOSIT_CACHE["value"] is not None and
            now - DEPOSIT_CACHE["ts"] < DEPOSIT_CACHE_TTL):
        return DEPOSIT_CACHE["value"]

    try:
        r = bybit("GET",
            "/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT")
        if r and r.get("retCode") == 0:
            for coin in r["result"]["list"][0].get("coin", []):
                if coin["coin"] == "USDT":
                    balance = float(coin.get("walletBalance", 0))
                    DEPOSIT_CACHE["value"] = balance
                    DEPOSIT_CACHE["ts"] = now
                    return balance
    except Exception as e:
        log_event(f"get_deposit error: {e}")

    # Fallback: вернуть кеш даже просроченный
    if DEPOSIT_CACHE["value"] is not None:
        return DEPOSIT_CACHE["value"]
    return 0


def calculate_margin(score: float, risk_pct: float | None = None,
                     max_positions: int | None = None) -> float:
    """
    Рассчитать маржу на позицию от % депозита.

    score:       5.5–10.0
    risk_pct:    % депозита в риске (None → DEFAULT_RISK_PCT)
    max_positions: макс позиций (None → MAX_POSITIONS)

    Returns: маржа в USDT (0 = не входить)
    """
    if risk_pct is None:
        risk_pct = DEFAULT_RISK_PCT
    if max_positions is None:
        max_positions = MAX_POSITIONS

    mult = _score_multiplier(score)
    if mult <= 0:
        return 0

    deposit = get_deposit()
    if deposit < MIN_DEPOSIT:
        log_event(f"💸 Депозит ${deposit:.1f} < ${MIN_DEPOSIT} — входы заблокированы")
        return 0

    risk_budget = deposit * risk_pct
    base = risk_budget / max_positions
    margin = round(base * mult, 1)

    # Пол
    margin = max(margin, MIN_MARGIN)

    # Потолок
    cap = risk_budget * MAX_POSITION_SHARE
    margin = min(margin, cap)

    return margin


def margin_for_strategy(strategy: str, score: float = 5.5) -> float:
    """
    Маржа под конкретную стратегию.

    strategy: 'long' | 'short' | 'reentry' | 'scalp' | 'mean_revert' | 'funding' | 'pump' | 'dca'
    score:    оценка (для long/short из scoring, для x10 — дефолт 5.5)
    """
    strategy_risk = {
        'long': DEFAULT_RISK_PCT,
        'short': DEFAULT_RISK_PCT,
        'reentry': DEFAULT_RISK_PCT,
        'dca': DEFAULT_RISK_PCT * 0.5,     # DCA — половинный риск
        'pump': DEFAULT_RISK_PCT * 0.3,     # Памп-шорты — минимальный риск
        'scalp': X10_RISK_PCT,
        'mean_revert': X10_RISK_PCT,
        'funding': X10_RISK_PCT,
    }

    risk_pct = strategy_risk.get(strategy, DEFAULT_RISK_PCT)
    return calculate_margin(score, risk_pct=risk_pct)


def format_margin_info(score: float, strategy: str = 'long') -> str:
    """Инфо-строка для логов: 'маржа $15.0 (депо $1000 × 20% / 5 × 1.15)'."""
    deposit = get_deposit()
    margin = margin_for_strategy(strategy, score)
    risk_pct = DEFAULT_RISK_PCT if strategy in ('long', 'short', 'reentry') else X10_RISK_PCT
    return (f"${margin:.1f} (депо ${deposit:.0f} × {risk_pct*100:.0f}% / "
            f"{MAX_POSITIONS} × {_score_multiplier(score):.2f})")
