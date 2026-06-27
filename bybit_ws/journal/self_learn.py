"""
Self-learning module v2 — applies journal insights + persistent logging.

Logs every adjustment to ~/.local/share/bybit-ws/self_learn.jsonl
for audit and manual review.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_ADJUSTMENT = 0.20
LEARN_LOG = Path.home() / ".local" / "share" / "bybit-ws" / "self_learn.jsonl"


def _log_adjustment(entry: dict):
    """Append adjustment to persistent log."""
    entry["ts"] = datetime.now().isoformat()
    try:
        LEARN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARN_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"self_learn log: {e}")


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

    log_base = {
        "event": "self_learn",
        "win_rate": round(win_rate, 3),
        "total_trades": total_trades,
        "avg_hold_hours": round(avg_hold_hours, 1),
        "total_pnl": round(total_pnl, 2),
    }

    # ── Adaptive min_score ──
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
    sl_pct = getattr(cfg.strategy, "sl_pct", 5)
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

    # ── Persistent log ──
    if applied:
        log_base["adjustments"] = applied
        _log_adjustment(log_base)
        logger.info(
            f"Self-learn: {len(applied)} adjustments "
            f"(win_rate={win_rate:.2f}, trades={total_trades})"
        )

    return applied
