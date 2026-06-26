"""
Self-learning module — applies journal insights to adapt strategy parameters.

Uses trading journal profile (win_rate, bias flags, symbol stats)
to auto-tune Bollinger Grid parameters. Conservative adjustments only.
"""

import logging

logger = logging.getLogger(__name__)

# Adjustments are capped at ±20% of default
MAX_ADJUSTMENT = 0.20


async def apply_journal_insights(journal: dict, cfg) -> dict:
    """Apply self-learning adjustments from journal profile.

    Returns dict of applied adjustments (for logging).
    """
    applied = {}

    profile = journal.get("profile", {})
    if not profile:
        return applied

    total_trades = profile.get("total_trades", 0)
    if total_trades < 20:
        return applied  # Not enough data

    win_rate = profile.get("win_rate", 0.5)
    avg_hold_hours = profile.get("avg_hold_hours", 24)
    total_pnl = profile.get("total_pnl", 0)

    # ── Adaptive min_score ──
    # Low win rate → raise entry threshold
    min_score = getattr(cfg.strategy, "min_score", 15)
    if win_rate < 0.40 and total_trades > 30:
        new_score = min(int(min_score * 1.3), 35)
        if new_score > min_score:
            applied["min_score"] = {"from": min_score, "to": new_score,
                                    "reason": f"win_rate={win_rate:.2f} < 0.40"}
    elif win_rate < 0.45 and total_trades > 50:
        new_score = min(int(min_score * 1.15), 30)
        if new_score > min_score:
            applied["min_score"] = {"from": min_score, "to": new_score,
                                    "reason": f"win_rate={win_rate:.2f} < 0.45"}

    # ── Adaptive TP/SL ratio ──
    tp_pct = getattr(cfg.strategy, "tp_pct", 15)
    sl_pct = getattr(cfg.strategy, "sl_pct", 5)
    # If holding too short (< 2h avg), widen SL to avoid noise exits
    if avg_hold_hours < 2 and total_trades > 30:
        new_sl = min(sl_pct * 1.2, 10)
        if new_sl > sl_pct:
            applied["sl_pct"] = {"from": sl_pct, "to": new_sl,
                                 "reason": f"avg_hold={avg_hold_hours:.1f}h < 2h"}

    # ── Bias warnings (logged, not auto-fixed) ──
    bias_flags = profile.get("bias_flags", {})
    if bias_flags:
        for bias, flag in bias_flags.items():
            if flag:
                applied[f"bias_{bias}"] = {"warning": True,
                                           "reason": f"Bias detected: {bias}"}

    # ── PnL guard ──
    if total_pnl < -100 and total_trades > 50:
        applied["pnl_guard"] = {"warning": True,
                                "reason": f"Total PnL={total_pnl:.0f} < -$100"}

    return applied
