"""
Self-learning module v4 — canary mode + per-symbol + exit tracking + sessions + streak guard.

Новое в v4:
  1. Per-symbol params — каждая монета имеет свой профиль (min_score, max_hold, sl_pct)
  2. Exit reason tracking — понимаем ПОЧЕМУ закрылись (SL/TP/Manual/Time)
  3. Session/time-based — разные параметры для Азии/Европы/US
  4. Consecutive loss protection — серии лосей → cooldown + уменьшение размера

State files:
  ~/.local/share/bybit-ws/canary_state.json     — canary-режим
  ~/.local/share/bybit-ws/canary_entries.jsonl   — canary-входы для матчинга
  ~/.local/share/bybit-ws/self_learn.jsonl       — лог обучения
  ~/.local/share/bybit-ws/symbol_profiles.json   — NEW: per-symbol параметры
  ~/.local/share/bybit-ws/exit_stats.jsonl       — NEW: exit reason tracking
  ~/.local/share/bybit-ws/loss_streak.json       — NEW: счётчик лосей
"""
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────
DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
LEARN_LOG = DATA_DIR / "self_learn.jsonl"
CANARY_STATE = DATA_DIR / "canary_state.json"
CANARY_ENTRIES = DATA_DIR / "canary_entries.jsonl"
SYMBOL_PROFILES = DATA_DIR / "symbol_profiles.json"      # NEW
EXIT_STATS = DATA_DIR / "exit_stats.jsonl"               # NEW
LOSS_STREAK = DATA_DIR / "loss_streak.json"              # NEW

_canary_lock = threading.Lock()
_entries_lock = threading.Lock()
_profiles_lock = threading.Lock()
_streak_lock = threading.Lock()

# ── Canary constants ────────────────────────────────
CANARY_ENTRY_PCT = 0.10
CANARY_WINDOW_HOURS = 48
CANARY_WR_DROP_THRESHOLD = 0.10
CANARY_MATCH_WINDOW = 3600

# ── Session zones ───────────────────────────────────
def _session_hour() -> int:
    """Текущий час UTC (0-23)."""
    return datetime.now(timezone.utc).hour

SESSION_ZONES = {
    "asia":   (0, 7),    # 00-07 UTC
    "europe": (7, 15),    # 07-15 UTC  
    "us":     (13, 21),   # 13-21 UTC (overlap в порядке, us priority)
}

def current_session() -> str:
    """Определить текущую торговую сессию."""
    h = _session_hour()
    if 13 <= h < 21: return "us"
    if 7 <= h < 15: return "europe"
    return "asia"


# ══════════════════════════════════════════════════════
# 1. PER-SYMBOL PROFILES
# ══════════════════════════════════════════════════════

def _load_symbol_profiles() -> dict:
    """Загрузить per-symbol параметры."""
    if SYMBOL_PROFILES.exists():
        try:
            return json.loads(SYMBOL_PROFILES.read_text())
        except Exception:
            pass
    return {}

def _save_symbol_profiles(profiles: dict):
    SYMBOL_PROFILES.parent.mkdir(parents=True, exist_ok=True)
    SYMBOL_PROFILES.write_text(json.dumps(profiles, indent=2))

def get_symbol_params(symbol: str, param_name: str, default: Any) -> Any:
    """Получить per-symbol параметр. Если нет профиля — возвращает default."""
    with _profiles_lock:
        profiles = _load_symbol_profiles()
    sym_profile = profiles.get(symbol, {})
    return sym_profile.get(param_name, default)

def update_symbol_profile(symbol: str, updates: dict, reason: str = ""):
    """Обновить per-symbol профиль на основе статистики."""
    with _profiles_lock:
        profiles = _load_symbol_profiles()
    if symbol not in profiles:
        profiles[symbol] = {}
    for k, v in updates.items():
        profiles[symbol][k] = v
    profiles[symbol]["_updated"] = datetime.now().isoformat()
    if reason:
        profiles[symbol]["_reason"] = reason
    with _profiles_lock:
        _save_symbol_profiles(profiles)
    logger.info(f"📊 Symbol profile {symbol}: {updates} ({reason})")

def auto_tune_symbol(symbol: str, trades: list):
    """Автоматически подобрать параметры для символа на основе его сделок.

    trades = [{side, pnl, hold_hours, exit_reason}, ...]
    """
    if len(trades) < 5:
        return  # недостаточно данных

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    wr = len(wins) / len(trades) if trades else 0
    avg_hold = sum(t.get("hold_hours", 0) for t in trades) / len(trades)
    total_pnl = sum(t.get("pnl", 0) for t in trades)

    updates = {}
    profiles = _load_symbol_profiles()
    current = profiles.get(symbol, {})

    # Если WR < 30% — ужесточаем min_score
    if wr < 0.30 and len(trades) >= 8:
        old_score = current.get("min_score", 15)
        updates["min_score"] = min(old_score + 5, 35)

    # Если avg_hold < 2h — быстрые развороты, tighter SL
    elif avg_hold < 2 and len(trades) >= 5:
        old_sl = current.get("sl_pct", 5)
        updates["sl_pct"] = max(old_sl - 1, 2)

    # Если avg_hold > 24h — даём больше пространства
    elif avg_hold > 24 and len(trades) >= 5:
        old_sl = current.get("sl_pct", 5)
        updates["sl_pct"] = min(old_sl + 1, 10)
        if "max_hold_hours" not in current:
            updates["max_hold_hours"] = int(avg_hold * 1.5)

    if updates:
        update_symbol_profile(symbol, updates,
                              f"auto: wr={wr:.0%} avg_hold={avg_hold:.1f}h pnl=${total_pnl:.0f}")


# ══════════════════════════════════════════════════════
# 2. EXIT REASON TRACKING
# ══════════════════════════════════════════════════════

def record_exit(symbol: str, side: str, pnl: float, reason: str,
                hold_hours: float = 0, entry_price: float = 0, exit_price: float = 0):
    """Записать причину закрытия позиции для статистики."""
    entry = {
        "ts": datetime.now().isoformat(),
        "symbol": symbol,
        "side": side,
        "pnl": round(pnl, 4),
        "reason": reason,           # SL / TP / Manual / Time / Emergency
        "hold_hours": round(hold_hours, 2),
        "entry_price": round(entry_price, 6) if entry_price else 0,
        "exit_price": round(exit_price, 6) if exit_price else 0,
    }
    try:
        EXIT_STATS.parent.mkdir(parents=True, exist_ok=True)
        with open(EXIT_STATS, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"exit_stats: {e}")

def get_exit_stats(symbol: str = None, days: int = 30) -> dict:
    """Статистика причин закрытия."""
    if not EXIT_STATS.exists():
        return {"total": 0, "reasons": {}, "symbols": {}}

    cutoff = time.time() - days * 86400
    stats = {"total": 0, "reasons": {}, "symbols": {}}
    try:
        for line in EXIT_STATS.read_text().strip().split("\n"):
            if not line: continue
            entry = json.loads(line)
            try:
                ts = datetime.fromisoformat(entry["ts"]).timestamp()
            except Exception:
                continue
            if ts < cutoff: continue
            if symbol and entry.get("symbol") != symbol: continue

            stats["total"] += 1
            reason = entry.get("reason", "unknown")
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1

            sym = entry.get("symbol", "?")
            if sym not in stats["symbols"]:
                stats["symbols"][sym] = {"total": 0, "reasons": {}}
            stats["symbols"][sym]["total"] += 1
            stats["symbols"][sym]["reasons"][reason] = \
                stats["symbols"][sym]["reasons"].get(reason, 0) + 1
    except Exception:
        pass
    return stats


# ══════════════════════════════════════════════════════
# 3. SESSION-BASED PARAMS
# ══════════════════════════════════════════════════════

SESSION_MODIFIERS = {
    # Сессия: (min_score_mod, max_positions_mod, tp_multiplier)
    "asia":   (1.0,  1.0, 0.8),   # Азия: стандарт, осторожный TP
    "europe": (0.9,  1.0, 1.0),   # Европа: чуть ниже порог входа
    "us":     (0.85, 1.2, 1.2),   # US overlap: ниже порог, больше позиций, шире TP
}

def get_session_modifier(param: str) -> float:
    """Получить модификатор параметра для текущей сессии.

    Параметры:
      - min_score: умножается на modifier (US=0.85 → порог входа ниже)
      - tp_mult: умножается на modifier (US=1.2 → шире TP)
    """
    sess = current_session()
    mods = SESSION_MODIFIERS.get(sess, (1.0, 1.0, 1.0))
    if param == "min_score": return mods[0]
    if param == "max_positions": return mods[1]
    if param == "tp_mult": return mods[2]
    return 1.0


# ══════════════════════════════════════════════════════
# 4. CONSECUTIVE LOSS PROTECTION
# ══════════════════════════════════════════════════════

def _load_streak() -> dict:
    if LOSS_STREAK.exists():
        try:
            return json.loads(LOSS_STREAK.read_text())
        except Exception:
            pass
    return {"consecutive_losses": 0, "consecutive_wins": 0,
            "total_consecutive_runs": [], "cooldown_until": None}

def _save_streak(streak: dict):
    LOSS_STREAK.parent.mkdir(parents=True, exist_ok=True)
    LOSS_STREAK.write_text(json.dumps(streak, indent=2))

def record_trade_result(pnl: float):
    """Записать результат сделки для отслеживания серий."""
    with _streak_lock:
        s = _load_streak()

        if pnl >= 0:
            s["consecutive_wins"] += 1
            if s["consecutive_losses"] > 0:
                s["total_consecutive_runs"].append({
                    "type": "loss", "count": s["consecutive_losses"],
                    "ended": datetime.now().isoformat()
                })
            s["consecutive_losses"] = 0
        else:
            s["consecutive_losses"] += 1
            if s["consecutive_wins"] > 0:
                s["total_consecutive_runs"].append({
                    "type": "win", "count": s["consecutive_wins"],
                    "ended": datetime.now().isoformat()
                })
            s["consecutive_wins"] = 0

        # Cooldown logic
        losses = s["consecutive_losses"]
        if losses >= 5:
            s["cooldown_until"] = (datetime.now().timestamp() + 86400)  # 24h стоп
            logger.warning(f"🛑 LOSS STREAK {losses}: cooldown 24h!")
        elif losses >= 3:
            s["cooldown_until"] = (datetime.now().timestamp() + 14400)  # 4h
            logger.warning(f"⚠️ LOSS STREAK {losses}: cooldown 4h, half size")

        _save_streak(s)

def get_streak_status() -> dict:
    """Текущий статус серии: размер позиции ×? и активен ли cooldown."""
    s = _load_streak()
    now = datetime.now().timestamp()
    cooldown_until = s.get("cooldown_until")

    if cooldown_until and now < cooldown_until:
        remaining_h = (cooldown_until - now) / 3600
        blocked = s["consecutive_losses"] >= 5
        return {
            "in_cooldown": True,
            "blocked": blocked,           # True = вообще не входим
            "size_multiplier": 0.0 if blocked else 0.5,
            "consecutive_losses": s["consecutive_losses"],
            "remaining_hours": round(remaining_h, 1),
        }

    return {
        "in_cooldown": False,
        "blocked": False,
        "size_multiplier": 1.0,
        "consecutive_losses": s["consecutive_losses"],
        "consecutive_wins": s["consecutive_wins"],
    }


# ══════════════════════════════════════════════════════
# CANARY MODE (v3 base, extended in v4)
# ══════════════════════════════════════════════════════

def _load_canary_state() -> dict:
    if CANARY_STATE.exists():
        try:
            return json.loads(CANARY_STATE.read_text())
        except Exception:
            pass
    return {
        "active": False, "params": {}, "baseline": {},
        "started_at": None, "canary_trades": 0, "canary_wins": 0,
        "baseline_wr": 0.0, "promoted": False, "rolled_back": False,
        "history": [],
        # v4: per-symbol canary
        "symbol_params": {},   # {symbol: {param: value}}
        "session_params": {},  # {session: {param: value}}
    }

def _save_canary_state(state: dict):
    CANARY_STATE.parent.mkdir(parents=True, exist_ok=True)
    CANARY_STATE.write_text(json.dumps(state, indent=2))

def _log_adjustment(entry: dict):
    entry["ts"] = datetime.now().isoformat()
    try:
        LEARN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARN_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"self_learn log: {e}")

def _entry_age(line: str) -> float:
    try:
        entry = json.loads(line)
        return time.time() - entry.get("entry_ts", 0)
    except Exception:
        return -1

def is_canary_active() -> bool:
    state = _load_canary_state()
    if not state.get("active"):
        return False
    started = state.get("started_at")
    if started:
        try:
            started_ts = datetime.fromisoformat(started)
            hours_elapsed = (datetime.now() - started_ts).total_seconds() / 3600
            if hours_elapsed > CANARY_WINDOW_HOURS:
                _finalize_canary(state)
                return False
        except Exception:
            pass
    return True

def should_use_canary() -> bool:
    if not is_canary_active():
        return False
    return random.random() < CANARY_ENTRY_PCT

def get_canary_param(param_name: str, baseline_value: Any,
                     symbol: str = None, side: str = None) -> Any:
    """v4: проверяет symbol_params, session_params, затем общие params."""
    if not should_use_canary():
        return baseline_value

    state = _load_canary_state()

    # Per-symbol override
    if symbol:
        sym_params = state.get("symbol_params", {}).get(symbol, {})
        if param_name in sym_params:
            return sym_params[param_name]

    # Session override
    sess = current_session()
    sess_params = state.get("session_params", {}).get(sess, {})
    if param_name in sess_params:
        return sess_params[param_name]

    # Global canary params
    params = state.get("params", {})
    if param_name in params:
        return params[param_name]

    return baseline_value

def mark_canary_entry(symbol: str, side: str, entry_ts: float):
    entry = {"symbol": symbol, "side": side, "entry_ts": entry_ts,
             "marked_at": datetime.now().isoformat()}
    with _entries_lock:
        try:
            CANARY_ENTRIES.parent.mkdir(parents=True, exist_ok=True)
            with open(CANARY_ENTRIES, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"canary mark entry: {e}")

def match_canary_entry(symbol: str, side: str, close_ts: float,
                       window: int = None) -> bool:
    if window is None:
        window = CANARY_MATCH_WINDOW
    with _entries_lock:
        if not CANARY_ENTRIES.exists():
            return False
        try:
            lines = CANARY_ENTRIES.read_text().strip().split("\n")
        except Exception:
            return False
        matched = False
        kept = []
        for line in lines:
            line = line.strip()
            if not line: continue
            try:
                entry = json.loads(line)
            except Exception:
                kept.append(line); continue
            if (entry.get("symbol") == symbol
                    and entry.get("side") == side
                    and close_ts - entry.get("entry_ts", 0) <= window
                    and close_ts >= entry.get("entry_ts", 0)):
                matched = True
            else:
                kept.append(line)
        now = time.time()
        kept = [l for l in kept if _entry_age(l) < CANARY_WINDOW_HOURS * 3600 or _entry_age(l) < 0]
        try:
            CANARY_ENTRIES.write_text("\n".join(kept) + ("\n" if kept else ""))
        except Exception:
            pass
    return matched

def record_canary_result(win: bool):
    with _canary_lock:
        state = _load_canary_state()
        if not state.get("active"): return
        state["canary_trades"] = state.get("canary_trades", 0) + 1
        if win:
            state["canary_wins"] = state.get("canary_wins", 0) + 1
        state["history"].append({
            "ts": datetime.now().isoformat(), "action": "trade", "win": win,
            "canary_trades": state["canary_trades"],
            "canary_wins": state["canary_wins"],
        })
        started = state.get("started_at")
        if started and state["canary_trades"] >= 10:
            try:
                started_ts = datetime.fromisoformat(started)
                hours_elapsed = (datetime.now() - started_ts).total_seconds() / 3600
                if hours_elapsed >= CANARY_WINDOW_HOURS:
                    _finalize_canary(state)
                    return
            except Exception:
                pass
        _save_canary_state(state)

def _finalize_canary(state: dict):
    if state.get("promoted") or state.get("rolled_back"): return
    canary_trades = state.get("canary_trades", 0)
    if canary_trades < 5:
        state["active"] = False
        state["rolled_back"] = True
        state["history"].append({"ts": datetime.now().isoformat(), "action": "rollback",
                                 "reason": f"insufficient data ({canary_trades} < 5)"})
        _save_canary_state(state)
        _log_canary_decision(state, "rollback", "insufficient data")
        return
    canary_wr = state["canary_wins"] / canary_trades if canary_trades > 0 else 0
    baseline_wr = state.get("baseline_wr", 0.5)
    wr_drop = baseline_wr - canary_wr
    if wr_drop > CANARY_WR_DROP_THRESHOLD:
        state["active"] = False; state["rolled_back"] = True
        state["history"].append({"ts": datetime.now().isoformat(), "action": "rollback",
                                 "reason": f"WR drop: {canary_wr:.3f} vs {baseline_wr:.3f}"})
        _save_canary_state(state)
        _log_canary_decision(state, "rollback", f"WR drop {wr_drop:.1%}")
    else:
        state["active"] = False; state["promoted"] = True
        # v4: promote per-symbol params into profiles
        sym_params = state.get("symbol_params", {})
        for sym, params in sym_params.items():
            update_symbol_profile(sym, params, f"canary promote: {canary_wr:.1%} vs baseline {baseline_wr:.1%}")
        state["history"].append({"ts": datetime.now().isoformat(), "action": "promote",
                                 "reason": f"WR ok: {canary_wr:.3f} vs {baseline_wr:.3f}",
                                 "params": state.get("params", {}),
                                 "symbol_params": sym_params})
        _save_canary_state(state)
        _log_canary_decision(state, "promote", f"canary WR={canary_wr:.1%}")

def _log_canary_decision(state: dict, decision: str, detail: str):
    ct = state.get("canary_trades", 0)
    cw = state.get("canary_wins", 0)
    wr = cw / ct if ct > 0 else 0
    _log_adjustment({
        "event": f"canary_{decision}", "detail": detail,
        "canary_trades": ct, "canary_wr": round(wr, 3),
        "baseline_wr": round(state.get("baseline_wr", 0), 3),
        "params": state.get("params", {}),
        "symbol_params": state.get("symbol_params", {}),
    })
    logger.info(f"🧪 Canary {decision}: {detail} (trades={ct}, canary_wr={wr:.1%}, baseline_wr={state['baseline_wr']:.1%})")


# ══════════════════════════════════════════════════════
# APPLY JOURNAL INSIGHTS (v4)
# ══════════════════════════════════════════════════════

async def apply_journal_insights(journal: dict, cfg) -> dict:
    """v4: per-symbol tuning + exit analysis + session params + streak guard."""
    applied = {}
    profile = journal.get("profile", {})
    if not profile:
        return applied

    total_trades = profile.get("total_trades", 0)
    if total_trades < 20:
        return applied

    win_rate = profile.get("win_rate", 0.5)
    avg_hold_hours = profile.get("avg_hold_hours", 24)
    total_pnl = profile.get("total_pnl", 0)

    canary_state = _load_canary_state()
    if canary_state.get("active"):
        logger.info("Canary already active, skipping")
        return applied

    new_params = {}
    symbol_params = {}
    session_params = {}

    # ── 1. GLOBAL: min_score ──
    min_score = getattr(cfg.strategy, "min_score", 15)
    if win_rate < 0.40 and total_trades > 30:
        new_params["min_score"] = min(int(min_score * 1.3), 35)
        applied["min_score"] = {"from": min_score, "to": new_params["min_score"],
                                "reason": f"wr={win_rate:.2f}"}
    elif win_rate < 0.45 and total_trades > 50:
        new_params["min_score"] = min(int(min_score * 1.15), 30)
        applied["min_score"] = {"from": min_score, "to": new_params["min_score"],
                                "reason": f"wr={win_rate:.2f}"}

    # ── 2. PER-SYMBOL: auto-tune из журнала ──
    per_symbol = journal.get("per_symbol", {})
    for sym, stats in per_symbol.items():
        sym_trades = stats.get("total_trades", 0)
        sym_wr = stats.get("win_rate", 0.5)
        sym_pnl = stats.get("total_pnl", 0)
        sym_hold = stats.get("avg_hold_hours", 24)

        if sym_trades < 8:
            continue

        sym_updates = {}
        current_profile = _load_symbol_profiles().get(sym, {})
        old_score = current_profile.get("min_score", min_score)

        if sym_wr < 0.30:
            sym_updates["min_score"] = min(old_score + 5, 35)
        if sym_hold < 2:
            sym_updates["sl_pct"] = max(current_profile.get("sl_pct", 5) - 1, 2)
        elif sym_hold > 24:
            sym_updates["sl_pct"] = min(current_profile.get("sl_pct", 5) + 1, 10)

        if sym_updates:
            symbol_params[sym] = sym_updates
            applied[f"symbol_{sym}"] = sym_updates

    # ── 3. EXIT ANALYSIS: adjust based on WHY ──
    exit_stats = get_exit_stats(days=30)
    sl_pct = exit_stats.get("reasons", {}).get("SL", 0)
    tp_pct = exit_stats.get("reasons", {}).get("TP", 0)
    total_exits = exit_stats.get("total", 0)

    if total_exits > 20:
        sl_ratio = sl_pct / total_exits
        tp_ratio = tp_pct / total_exits

        if sl_ratio > 0.60:
            # >60% закрытий по SL → SL слишком tight
            old_sl = getattr(cfg.strategy, "sl_pct", 5)
            new_sl = min(old_sl + 1.5, 12)
            if "sl_pct" not in new_params:
                new_params["sl_pct"] = new_sl
                applied["sl_pct"] = {"from": old_sl, "to": new_sl,
                                     "reason": f"SL exit rate={sl_ratio:.0%}"}

        if tp_ratio > 0.50:
            # >50% TP → можно расширить TP
            old_tp = getattr(cfg.strategy, "tp_mult", 1.0)
            new_tp = min(old_tp + 0.2, 2.0)
            new_params["tp_mult"] = new_tp
            applied["tp_mult"] = {"from": old_tp, "to": new_tp,
                                  "reason": f"TP hit rate={tp_ratio:.0%}"}

    # ── 4. SESSION PARAMS ──
    session_stats = journal.get("by_session", {})
    for sess in ["asia", "europe", "us"]:
        ss = session_stats.get(sess, {})
        if ss.get("total_trades", 0) < 10:
            continue
        sess_wr = ss.get("win_rate", 0.5)
        if sess_wr < 0.35:
            # Повышаем порог входа в этой сессии
            session_params[sess] = {"min_score_mod": 1.15}
            applied[f"session_{sess}"] = {"min_score_mod": 1.15,
                                          "reason": f"wr={sess_wr:.2f}"}
        elif sess_wr > 0.55:
            # Можно ослабить порог
            session_params[sess] = {"min_score_mod": 0.90}
            applied[f"session_{sess}"] = {"min_score_mod": 0.90,
                                          "reason": f"wr={sess_wr:.2f}"}

    # ── PnL Guard ──
    if total_pnl < -100 and total_trades > 50:
        applied["pnl_guard"] = {"warning": True, "reason": f"PnL={total_pnl:.0f}"}

    # ── LAUNCH CANARY ──
    combined = bool(new_params or symbol_params or session_params)
    if combined:
        baseline_vals = {}
        for k, adj in applied.items():
            if isinstance(adj, dict) and "from" in adj:
                baseline_vals[k] = adj["from"]

        canary_state = {
            "active": True,
            "params": new_params,
            "baseline": baseline_vals,
            "started_at": datetime.now().isoformat(),
            "canary_trades": 0, "canary_wins": 0,
            "baseline_wr": win_rate,
            "promoted": False, "rolled_back": False,
            "history": [{"ts": datetime.now().isoformat(), "action": "start",
                         "params": new_params, "baseline_wr": win_rate}],
            "symbol_params": symbol_params,
            "session_params": session_params,
        }
        _save_canary_state(canary_state)
        logger.info(f"🧪 Canary v4: {len(new_params)} global + {len(symbol_params)} symbol + {len(session_params)} session params")

    log_base = {"event": "self_learn", "win_rate": round(win_rate, 3),
                "total_trades": total_trades, "avg_hold_hours": round(avg_hold_hours, 1),
                "total_pnl": round(total_pnl, 2)}
    if applied:
        log_base["adjustments"] = applied
    _log_adjustment(log_base)
    return applied
