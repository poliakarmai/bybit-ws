"""
Self-learning module v9 — Dynamic Bandit + Pareto MC + Regime Drift + Ensemble + Micro-updates.

Новое в v9:
  1. Dynamic Thompson Sampling — авто-прунинг + генерация рук каждые 24ч
  2. Pareto-calibrated Monte Carlo — heavy-tail распределение (α=2.5)
  3. Regime-aware Drift Detector — per-regime окна + EMA baseline
  4. Ensemble Learning — отдельный bandit для каждого рыночного режима
  5. Online micro-updates — инкрементальное обучение после каждой сделки
  6. Exit reason tracking — понимаем ПОЧЕМУ закрылись (SL/TP/Manual/Time)
  7. Session/time-based — разные параметры для Азии/Европы/US
  8. Consecutive loss protection — серии лосей → cooldown + уменьшение размера

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
CANARY_WINDOW_HOURS = 6
CANARY_WR_DROP_THRESHOLD = 0.10
CANARY_MATCH_WINDOW = 3600
CANARY_IDLE_TIMEOUT_HOURS = 3  # NEW: авто-откат без сделок

# ── Self-learn timing ───────────────────────────────
SELF_LEARN_INTERVAL_SEC = 6 * 3600  # NEW: wall-clock interval (6 часов)
SELF_LEARN_STATE = DATA_DIR / "self_learn_state.json"  # NEW: последний запуск

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
            # ── NEW: idle timeout — нет сделок > CANARY_IDLE_TIMEOUT_HOURS → авто-откат ──
            canary_trades = state.get("canary_trades", 0)
            if canary_trades == 0 and hours_elapsed > CANARY_IDLE_TIMEOUT_HOURS:
                _auto_rollback_idle_canary(state, hours_elapsed)
                return False
            if hours_elapsed > CANARY_WINDOW_HOURS:
                _finalize_canary(state)
                return False
        except Exception:
            pass
    return True


def _auto_rollback_idle_canary(state: dict, hours_elapsed: float):
    """Авто-откат canary если 0 сделок за IDLE_TIMEOUT часов."""
    state["active"] = False
    state["rolled_back"] = True
    state["history"].append({
        "ts": datetime.now().isoformat(), "action": "rollback",
        "reason": f"idle timeout: 0 trades in {hours_elapsed:.1f}h"
    })
    _save_canary_state(state)
    _log_adjustment({
        "event": "canary_idle_rollback",
        "hours_elapsed": round(hours_elapsed, 1),
        "detail": "canary had 0 trades — reset to allow new self-learn cycle"
    })
    logger.warning(f"🧪 Canary idle rollback: 0 trades in {hours_elapsed:.1f}h — reset")

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
    baseline_trades = state.get("baseline_trades", canary_trades)  # если не сохранено — паритет
    baseline_wins = int(baseline_trades * baseline_wr)

    # ── V6: Bayesian A/B test вместо простого WR-сравнения ──
    prob_canary_better = bayesian_ab_test(
        state["canary_wins"], canary_trades,
        baseline_wins, baseline_trades
    )

    wr, ci_low, ci_high = calculate_wr_with_ci(state["canary_wins"], canary_trades)

    if prob_canary_better > 0.95:
        # Canary статистически значимо лучше
        state["active"] = False; state["promoted"] = True
        sym_params = state.get("symbol_params", {})
        for sym, params in sym_params.items():
            update_symbol_profile(sym, params,
                                  f"canary promote: P(better)={prob_canary_better:.2f}")
        state["history"].append({"ts": datetime.now().isoformat(), "action": "promote",
                                 "reason": f"Bayesian P(better)={prob_canary_better:.3f}, "
                                           f"WR={canary_wr:.1%} CI[{ci_low:.0%}-{ci_high:.0%}]",
                                 "params": state.get("params", {})})
        _save_canary_state(state)
        _log_canary_decision(state, "promote",
                             f"Bayesian P={prob_canary_better:.2f} WR={canary_wr:.1%}")
    elif prob_canary_better < 0.05:
        # Canary статистически значимо хуже
        state["active"] = False; state["rolled_back"] = True
        state["history"].append({"ts": datetime.now().isoformat(), "action": "rollback",
                                 "reason": f"Bayesian P(better)={prob_canary_better:.3f}"})
        _save_canary_state(state)
        _log_canary_decision(state, "rollback", f"P(better)={prob_canary_better:.3f}")
    else:
        # Неопределённо — недостаточно данных для вывода
        wr_drop = baseline_wr - canary_wr
        if wr_drop > CANARY_WR_DROP_THRESHOLD:
            state["active"] = False; state["rolled_back"] = True
            state["history"].append({"ts": datetime.now().isoformat(), "action": "rollback",
                                     "reason": f"WR drop: {canary_wr:.3f} vs {baseline_wr:.3f} "
                                               f"(Bayesian P={prob_canary_better:.2f})"})
            _save_canary_state(state)
            _log_canary_decision(state, "rollback",
                                 f"WR drop {wr_drop:.1%} (P={prob_canary_better:.2f})")
        else:
            state["active"] = False; state["promoted"] = True
            sym_params = state.get("symbol_params", {})
            for sym, params in sym_params.items():
                update_symbol_profile(sym, params,
                                      f"canary promote (conservative): P(better)={prob_canary_better:.2f}")
            state["history"].append({"ts": datetime.now().isoformat(), "action": "promote",
                                     "reason": f"Conservative: P(better)={prob_canary_better:.3f}, "
                                               f"no significant drop",
                                     "params": state.get("params", {}),
                                     "symbol_params": sym_params})
            _save_canary_state(state)
            _log_canary_decision(state, "promote",
                                 f"conservative WR={canary_wr:.1%} P={prob_canary_better:.2f}")

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
    if is_canary_active():
        logger.info("Canary already active, skipping")
        return applied

    new_params = {}
    symbol_params = {}
    session_params = {}

    # ── 1. GLOBAL: min_score (с Bayesian shrinkage к кластеру) ──
    min_score = getattr(cfg.strategy, "min_score", 15)
    new_min_score = min_score
    if win_rate < 0.40 and total_trades > 30:
        # Сдвиг к кластерному WR
        cluster_stats = get_cluster_stats()
        cluster_wr = 0.5
        for c, s in cluster_stats.items():
            if s["trades"] > 10:
                cluster_wr = s["win_rate"]
                break
        shrunk_wr = calculate_shrunk_param(total_trades, win_rate, cluster_wr)
        if shrunk_wr < 0.40:
            new_min_score = min(int(min_score * 1.3), 35)
        elif shrunk_wr < 0.45:
            new_min_score = min(int(min_score * 1.15), 30)
    elif win_rate < 0.45 and total_trades > 50:
        new_min_score = min(int(min_score * 1.15), 30)

    if new_min_score != min_score:
        new_params["min_score"] = new_min_score
        new_params["_old_min_score"] = min_score
        applied["min_score"] = {"from": min_score, "to": new_min_score,
                                "reason": f"wr={win_rate:.2f}"}

    # ── 2. PER-SYMBOL: auto-tune из журнала ──
    per_symbol = journal.get("per_symbol", {})
    for sym, stats in per_symbol.items():
        sym_trades = stats.get("total_trades", 0)
        sym_wr = stats.get("win_rate", 0.5)
        sym_pnl = stats.get("total_pnl", 0)
        sym_hold = stats.get("avg_hold_hours", 24)

        if sym_trades < 5:  # NEW: снижен порог с 8 до 5
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

    # ── 2b. CLUSTER-AWARE: если per-symbol < 5 сделок — учимся на кластере ──
    try:
        cluster_stats = get_cluster_stats()
        for sym, stats in per_symbol.items():
            sym_trades = stats.get("total_trades", 0)  # per-symbol в этом контексте
            if sym_trades >= 5:
                continue  # уже обработан выше
            cluster = _get_symbol_cluster(sym)
            if cluster == "unknown":
                continue
            cs = cluster_stats.get(cluster, {})
            if cs.get("trades", 0) < 10:
                continue
            cluster_wr = cs.get("win_rate", 0.5)
            cluster_hold = cs.get("avg_hold_hours", 24)
            current_profile = _load_symbol_profiles().get(sym, {})
            sym_updates = {}
            if cluster_wr < 0.35:
                sym_updates["min_score"] = min(current_profile.get("min_score", min_score) + 3, 35)
            if cluster_hold < 2:
                sym_updates["sl_pct"] = max(current_profile.get("sl_pct", 5) - 1, 2)
            if sym_updates:
                symbol_params[sym] = sym_updates
                applied[f"cluster_{sym}"] = sym_updates
    except Exception:
        pass

    # ── 3. EXIT ANALYSIS: adjust based on WHY + SL TIME ──
    exit_stats = get_exit_stats(days=30)
    sl_pct_exits = exit_stats.get("reasons", {}).get("SL", 0)
    tp_pct_exits = exit_stats.get("reasons", {}).get("TP", 0)
    total_exits = exit_stats.get("total", 0)

    if total_exits > 20:
        sl_ratio = sl_pct_exits / total_exits
        tp_ratio = tp_pct_exits / total_exits

        # ── V6: SL time analysis — различаем quick vs slow SL ──
        try:
            import sqlite3 as _sl_sql
            db = Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
            conn = _sl_sql.connect(str(db))
            conn.row_factory = _sl_sql.Row
            sl_trades = [dict(r) for r in conn.execute(
                "SELECT * FROM trade_history WHERE exit_reason='SL' AND closed_at IS NOT NULL"
            ).fetchall()]
            conn.close()
            sl_diag = analyze_sl_exits_from_dicts(sl_trades)
            if sl_diag == 'BAD_ENTRY_QUALITY':
                applied["sl_diag"] = {"warning": ">50% SL <30min — проблема во входах, не в SL"}
            elif sl_diag == 'SL_TOO_TIGHT':
                old_sl = getattr(cfg.strategy, "sl_pct", 5)
                new_sl = min(old_sl + 2.0, 12)
                if "sl_pct" not in new_params:
                    new_params["sl_pct"] = new_sl
                    applied["sl_pct"] = {"from": old_sl, "to": new_sl,
                                         "reason": f"SL_TOO_TIGHT (>50% SL hold>4h, SL ratio={sl_ratio:.0%})"}
        except Exception:
            pass

        # ── Standard SL/TP ratio analysis ──
        if sl_ratio > 0.60 and "sl_pct" not in new_params:
            old_sl = getattr(cfg.strategy, "sl_pct", 5)
            new_sl = min(old_sl + 1.5, 12)
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

    # ── V6: Global rollback check ──
    rollback = check_global_rollback(win_rate)
    if rollback:
        applied["global_rollback"] = rollback
        logger.warning(f"🔄 Global rollback: WR dropped {rollback['drop']:.1%} in {rollback['hours_since_change']:.1f}h")
        # Откатываем на месте — возвращаем предыдущие параметры
        if rollback.get("params"):
            applied["_rolled_back_params"] = rollback["params"]

    # ── V6: Walk-forward validation перед canary ──
    combined_params = {**new_params}
    if min_score and "min_score" in applied:
        combined_params["_old_min_score"] = applied["min_score"].get("from", min_score)
    wf_result = None
    if combined_params:
        try:
            wf_result = walk_forward_validation(combined_params)
        except Exception:
            pass

    # ── V6: Сохраняем снепшот перед изменением ──
    if new_params:
        save_params_snapshot(new_params, win_rate, "self_learn")

    # ── LAUNCH CANARY ──
    combined = bool(new_params or symbol_params or session_params)
    if combined:
        # V6: проверяем walk-forward
        if wf_result and not wf_result.get("approved", True):
            logger.info(f"🚫 Walk-forward rejected: {wf_result}")
            applied["walk_forward"] = wf_result
        else:
            if wf_result:
                applied["walk_forward"] = wf_result
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
        # ── V6: Human-readable explanation ──
        log_base["explanation"] = generate_explanation(applied)
    _log_adjustment(log_base)
    return applied


# ══════════════════════════════════════════════════════
# 5. WALL-CLOCK SELF-LEARN TRIGGER (v5)
# ══════════════════════════════════════════════════════

def should_run_self_learn() -> bool:
    """Проверить, пора ли запускать self-learn (по wall clock, не по cycle count).

    Сохраняет время последнего запуска в self_learn_state.json.
    """
    now = time.time()
    try:
        if SELF_LEARN_STATE.exists():
            state = json.loads(SELF_LEARN_STATE.read_text())
            last_run = state.get("last_run_ts", 0)
            if now - last_run < SELF_LEARN_INTERVAL_SEC:
                return False
    except Exception:
        pass
    return True


def mark_self_learn_run():
    """Записать время запуска self-learn."""
    SELF_LEARN_STATE.parent.mkdir(parents=True, exist_ok=True)
    SELF_LEARN_STATE.write_text(json.dumps({
        "last_run_ts": time.time(),
        "last_run_iso": datetime.now().isoformat(),
    }))


# ══════════════════════════════════════════════════════
# 6. REGIME-AWARE ANALYSIS (v5)
# ══════════════════════════════════════════════════════

def get_regime_aware_stats(db_path: str = None) -> dict:
    """Статистика сделок с разбивкой по рыночному режиму на момент входа.

    Читает trade_history и LSTM-кеш чтобы понять в каком режиме были входы.
    Возвращает: {regime: {trades, wr, total_pnl}, ...}
    """
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path
    db = _Path(db_path) if db_path else _Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
    if not db.exists():
        return {}

    # Загружаем кеш режимов (если есть)
    regime_log = {}
    regime_cache_path = DATA_DIR / "lstm_regime_cache.json"
    # Пытаемся загрузить историю режимов из self_learn лога
    try:
        if LEARN_LOG.exists():
            for line in LEARN_LOG.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("event") == "regime_snapshot":
                    regime_log[entry.get("ts", "").split("T")[0]] = entry.get("regime", "unknown")
    except Exception:
        pass

    # Текущий режим как fallback
    current_regime = "unknown"
    if regime_cache_path.exists():
        try:
            cache = json.loads(regime_cache_path.read_text())
            current_regime = cache.get("regime", "unknown")
        except Exception:
            pass

    conn = _sqlite3.connect(str(db))
    conn.row_factory = _sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, side, pnl, entry_at, closed_at, exit_reason, hold_hours "
        "FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at"
    ).fetchall()
    conn.close()

    by_regime = {}
    for r in rows:
        # Приблизительно определяем режим по дате входа
        entry_date = datetime.fromtimestamp(r["entry_at"]).strftime("%Y-%m-%d")
        regime = regime_log.get(entry_date, current_regime)

        if regime not in by_regime:
            by_regime[regime] = {"trades": 0, "wins": 0, "total_pnl": 0.0,
                                "symbols": {}, "side": {"Buy": 0, "Sell": 0}}

        stats = by_regime[regime]
        stats["trades"] += 1
        pnl = float(r["pnl"] or 0)
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
        stats["side"][r["side"]] = stats["side"].get(r["side"], 0) + 1

        sym = r["symbol"]
        if sym not in stats["symbols"]:
            stats["symbols"][sym] = 0
        stats["symbols"][sym] += 1

    # Добавляем WR
    for regime, stats in by_regime.items():
        stats["win_rate"] = round(stats["wins"] / stats["trades"], 3) if stats["trades"] > 0 else 0

    return by_regime


# ══════════════════════════════════════════════════════
# 7. SYMBOL CLUSTERING (v5)
# ══════════════════════════════════════════════════════

# Pre-defined volatility buckets based on typical Bybit pairs
VOLATILITY_CLUSTERS = {
    "high_vol": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],        # >3% daily moves
    "mid_vol": ["AVAXUSDT", "LINKUSDT", "ADAUSDT", "DOTUSDT", "MATICUSDT"],
    "low_vol": ["XRPUSDT", "LTCUSDT", "BNBUSDT", "TRXUSDT"],
}

_cluster_cache = None
_cluster_cache_ts = 0


def _get_symbol_cluster(symbol: str) -> str:
    """Определить кластер волатильности для символа."""
    for cluster, symbols in VOLATILITY_CLUSTERS.items():
        if symbol in symbols:
            return cluster
    # По BB width из кеша WS
    return "unknown"


def get_cluster_stats(db_path: str = None) -> dict:
    """Статистика сделок с группировкой по кластерам волатильности.

    Когда на отдельный символ < 5 сделок — агрегируем по кластеру.
    """
    import sqlite3 as _sqlite3
    global _cluster_cache, _cluster_cache_ts

    # Кеш на 1 час
    if _cluster_cache and time.time() - _cluster_cache_ts < 3600:
        return _cluster_cache

    from pathlib import Path as _Path2
    db = _Path2(db_path) if db_path else _Path2.home() / ".local" / "share" / "bybit-ws" / "state.db"
    if not db.exists():
        return {}

    conn = _sqlite3.connect(str(db))
    conn.row_factory = _sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, side, pnl, hold_hours, exit_reason "
        "FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at"
    ).fetchall()
    conn.close()

    by_cluster = {}
    for r in rows:
        cluster = _get_symbol_cluster(r["symbol"])
        if cluster not in by_cluster:
            by_cluster[cluster] = {"trades": 0, "wins": 0, "total_pnl": 0.0,
                                   "total_hold": 0.0, "symbols": {}}

        stats = by_cluster[cluster]
        stats["trades"] += 1
        pnl = float(r["pnl"] or 0)
        stats["total_pnl"] += pnl
        stats["total_hold"] += float(r["hold_hours"] or 0)
        if pnl > 0:
            stats["wins"] += 1

        sym = r["symbol"]
        stats["symbols"][sym] = stats["symbols"].get(sym, 0) + 1

    for cluster, stats in by_cluster.items():
        stats["win_rate"] = round(stats["wins"] / stats["trades"], 3) if stats["trades"] > 0 else 0
        stats["avg_hold_hours"] = round(stats["total_hold"] / stats["trades"], 1) if stats["trades"] > 0 else 0

    _cluster_cache = by_cluster
    _cluster_cache_ts = time.time()
    return by_cluster


# ══════════════════════════════════════════════════════
# V6: BAYESIAN A/B TESTING (замена простого WR-сравнения)
# ══════════════════════════════════════════════════════

def bayesian_ab_test(canary_wins: int, canary_total: int,
                     baseline_wins: int, baseline_total: int) -> float:
    """Bayesian A/B test: P(canary > baseline).

    Использует Beta-распределение (сопряжённое с Bernoulli).
    Возвращает вероятность что canary лучше baseline.
    Порог: >0.95 → promote, <0.05 → rollback.
    """
    if canary_total < 3 or baseline_total < 3:
        return 0.5  # недостаточно данных

    # Beta posterior: Beta(α=wins+1, β=losses+1)
    canary_alpha = canary_wins + 1
    canary_beta = canary_total - canary_wins + 1
    baseline_alpha = baseline_wins + 1
    baseline_beta = baseline_total - baseline_wins + 1

    # Monte Carlo sampling (достаточно точно для наших целей)
    import random as _random
    N = 20000
    canary_better = 0
    for _ in range(N):
        # Используем метод сумм для Beta через Gamma
        c = sum(-1 * __import__('math').log(_random.random()) for _ in range(canary_alpha))
        c /= (c + sum(-1 * __import__('math').log(_random.random()) for _ in range(canary_beta)))
        b = sum(-1 * __import__('math').log(_random.random()) for _ in range(baseline_alpha))
        b /= (b + sum(-1 * __import__('math').log(_random.random()) for _ in range(baseline_beta)))
        if c > b:
            canary_better += 1

    return canary_better / N


# ══════════════════════════════════════════════════════
# V6: BAYESIAN SHRINKAGE (per-symbol → cluster mean)
# ══════════════════════════════════════════════════════

def calculate_shrunk_param(symbol_trades: int, symbol_value: float,
                           cluster_value: float, min_trades: int = 20) -> float:
    """Bayesian shrinkage: чем меньше сделок, тем ближе к cluster_value.

    weight = min(symbol_trades / min_trades, 1.0)
    result = weight * symbol_value + (1 - weight) * cluster_value
    """
    weight = min(symbol_trades / min_trades, 1.0)
    return weight * symbol_value + (1 - weight) * cluster_value


# ══════════════════════════════════════════════════════
# V6: SL EXIT TIME ANALYSIS
# ══════════════════════════════════════════════════════

def analyze_sl_exits(trades: list) -> str:
    """Анализ времени до SL: quick (<30мин) vs normal (30мин-4ч) vs slow (>4ч).

    Returns: 'BAD_ENTRY_QUALITY' | 'SL_TOO_TIGHT' | 'NORMAL'
    """
    sl_exits = [t for t in trades if hasattr(t, 'exit_reason') and t.exit_reason == 'SL']
    if not sl_exits:
        return 'NORMAL'

    quick_sl = [t for t in sl_exits if getattr(t, 'hold_hours', 0) < 0.5]
    normal_sl = [t for t in sl_exits if 0.5 <= getattr(t, 'hold_hours', 0) < 4]
    slow_sl = [t for t in sl_exits if getattr(t, 'hold_hours', 0) >= 4]

    total = len(sl_exits)
    if len(quick_sl) / total > 0.5:
        return 'BAD_ENTRY_QUALITY'  # быстрые SL = плохие входы, не tight SL
    elif len(slow_sl) / total > 0.5:
        return 'SL_TOO_TIGHT'  # долгие позиции → SL срабатывает поздно
    return 'NORMAL'


def analyze_sl_exits_from_dicts(trades: list) -> str:
    """Версия для dict-объектов (из БД)."""
    sl_exits = [t for t in trades if t.get('exit_reason') == 'SL']
    if not sl_exits:
        return 'NORMAL'

    quick_sl = [t for t in sl_exits if t.get('hold_hours', 0) < 0.5]
    slow_sl = [t for t in sl_exits if t.get('hold_hours', 0) >= 4]

    total = len(sl_exits)
    if len(quick_sl) / total > 0.5:
        return 'BAD_ENTRY_QUALITY'
    elif len(slow_sl) / total > 0.5:
        return 'SL_TOO_TIGHT'
    return 'NORMAL'


# ══════════════════════════════════════════════════════
# V6: EVENT-DRIVEN TRIGGER (≥10 сделок или 24ч fallback)
# ══════════════════════════════════════════════════════

def should_run_self_learn_v6(min_trades: int = 10,
                              min_hours: int = 6,
                              fallback_hours: int = 24) -> bool:
    """Event-driven trigger: ≥N сделок за M часов, или fallback через K часов."""
    now = time.time()

    last_run = now - fallback_hours * 3600  # default: никогда
    try:
        if SELF_LEARN_STATE.exists():
            state = json.loads(SELF_LEARN_STATE.read_text())
            last_run = state.get("last_run_ts", 0)
    except Exception:
        pass

    hours_since = (now - last_run) / 3600

    # Условие 1: прошло ≥min_hours и было ≥min_trades сделок
    if hours_since >= min_hours:
        # Считаем сделки с последнего запуска
        trades_since = _count_trades_since(last_run)
        if trades_since >= min_trades:
            return True

    # Условие 2: fallback — прошло ≥fallback_hours
    if hours_since >= fallback_hours:
        return True

    return False


def _count_trades_since(since_ts: float) -> int:
    """Подсчитать закрытые сделки с заданного времени."""
    import sqlite3 as _sqlite3
    db_path = Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
    if not db_path.exists():
        return 0
    try:
        conn = _sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM trade_history WHERE closed_at > ?",
            (int(since_ts),)
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


# ══════════════════════════════════════════════════════
# V6: GLOBAL ROLLBACK
# ══════════════════════════════════════════════════════

GLOBAL_PARAMS_LOG = DATA_DIR / "global_params_log.json"


def save_params_snapshot(params: dict, wr: float, reason: str = "manual"):
    """Сохранить снепшот параметров перед изменением."""
    snapshots = []
    if GLOBAL_PARAMS_LOG.exists():
        try:
            snapshots = json.loads(GLOBAL_PARAMS_LOG.read_text())
        except Exception:
            pass

    snapshots.append({
        "ts": datetime.now().isoformat(),
        "params": params,
        "wr": wr,
        "reason": reason,
    })

    # Храним последние 20 снепшотов
    if len(snapshots) > 20:
        snapshots = snapshots[-20:]

    GLOBAL_PARAMS_LOG.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_PARAMS_LOG.write_text(json.dumps(snapshots, indent=2))


def check_global_rollback(current_wr: float, window_hours: int = 48,
                          wr_drop_threshold: float = 0.15) -> dict | None:
    """Проверить, не упал ли WR после последнего изменения параметров.

    Returns: {action: 'rollback', params: {...}} или None.
    """
    if not GLOBAL_PARAMS_LOG.exists():
        return None

    try:
        snapshots = json.loads(GLOBAL_PARAMS_LOG.read_text())
    except Exception:
        return None

    if not snapshots:
        return None

    last_change = snapshots[-1]
    change_ts = datetime.fromisoformat(last_change["ts"])
    hours_since = (datetime.now() - change_ts).total_seconds() / 3600

    if hours_since < 2:  # слишком рано
        return None

    baseline_wr = last_change.get("wr", 0.5)
    if current_wr < baseline_wr * (1 - wr_drop_threshold):
        # Найти предыдущий снепшот для отката
        prev_params = snapshots[-2]["params"] if len(snapshots) > 1 else {}
        return {
            "action": "rollback",
            "params": prev_params,
            "current_wr": round(current_wr, 3),
            "baseline_wr": round(baseline_wr, 3),
            "drop": round(baseline_wr - current_wr, 3),
            "hours_since_change": round(hours_since, 1),
        }

    return None


# ══════════════════════════════════════════════════════
# V6: FEATURE IMPORTANCE TRACKING
# ══════════════════════════════════════════════════════

FILTER_NAMES = ['MTF', 'Orderbook', 'Volume', 'EntryJudge', 'Correlation']


def track_filter_performance(db_path: str = None) -> dict:
    """Анализ вклада каждого фильтра в WR.

    Для каждого фильтра сравнивает WR сделок где фильтр pass vs fail.
    """
    import sqlite3 as _sqlite3
    db = Path(db_path) if db_path else Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
    if not db.exists():
        return {}

    conn = _sqlite3.connect(str(db))
    conn.row_factory = _sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, pnl, entry_reason FROM trade_history "
        "WHERE closed_at IS NOT NULL AND entry_reason IS NOT NULL "
        "ORDER BY entry_at DESC LIMIT 200"
    ).fetchall()
    conn.close()

    if not rows:
        return {}

    # Парсим entry_reason (формат: "MTF:pass|Orderbook:fail|Volume:pass|...")
    filter_stats = {f: {"pass_wins": 0, "pass_total": 0,
                         "fail_wins": 0, "fail_total": 0}
                    for f in FILTER_NAMES}

    for r in rows:
        pnl = float(r["pnl"] or 0)
        reason = r["entry_reason"] or ""
        for f in FILTER_NAMES:
            if f"{f}:pass" in reason:
                filter_stats[f]["pass_total"] += 1
                if pnl > 0:
                    filter_stats[f]["pass_wins"] += 1
            elif f"{f}:fail" in reason:
                filter_stats[f]["fail_total"] += 1
                if pnl > 0:
                    filter_stats[f]["fail_wins"] += 1

    result = {}
    for f, s in filter_stats.items():
        pass_wr = s["pass_wins"] / s["pass_total"] if s["pass_total"] > 0 else 0
        fail_wr = s["fail_wins"] / s["fail_total"] if s["fail_total"] > 0 else 0
        result[f] = {
            "pass_wr": round(pass_wr, 3),
            "pass_n": s["pass_total"],
            "fail_wr": round(fail_wr, 3),
            "fail_n": s["fail_total"],
            "useful": pass_wr > fail_wr + 0.05,  # фильтр полезен если pass WR > fail WR +5%
        }

    return result


# ══════════════════════════════════════════════════════
# V6: CONFIDENCE INTERVALS (Wilson score)
# ══════════════════════════════════════════════════════

def calculate_wr_with_ci(wins: int, total: int) -> tuple:
    """Wilson score confidence interval для win rate.

    Returns: (wr, ci_lower, ci_upper) — 95% confidence.
    """
    if total == 0:
        return (0.0, 0.0, 0.0)

    wr = wins / total
    z = 1.96  # 95% confidence

    denominator = 1 + z**2 / total
    centre = (wr + z**2 / (2 * total)) / denominator
    margin = z * ((wr * (1 - wr) / total + z**2 / (4 * total**2)) ** 0.5) / denominator

    ci_lower = max(0.0, centre - margin)
    ci_upper = min(1.0, centre + margin)

    return (round(wr, 3), round(ci_lower, 3), round(ci_upper, 3))


# ══════════════════════════════════════════════════════
# V6: WALK-FORWARD VALIDATION
# ══════════════════════════════════════════════════════

def walk_forward_validation(new_params: dict, historical_trades: list = None,
                             db_path: str = None) -> dict:
    """Проверить новые параметры на исторических данных (70/30 split).

    Returns: {approved: bool, current_wr: float, simulated_wr: float, ...}
    """
    import sqlite3 as _sqlite3
    db = Path(db_path) if db_path else Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
    if not db.exists():
        return {"approved": True, "reason": "no historical data"}

    conn = _sqlite3.connect(str(db))
    conn.row_factory = _sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, side, pnl, entry_price, exit_price, size, entry_at, closed_at "
        "FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at"
    ).fetchall()
    conn.close()

    if len(rows) < 20:
        return {"approved": True, "reason": f"too few trades ({len(rows)} < 20)"}

    # 70/30 split
    split = int(len(rows) * 0.7)
    test_trades = rows[split:]

    # Текущий WR на test set
    test_wins = sum(1 for r in test_trades if float(r["pnl"] or 0) > 0)
    test_total = len(test_trades)
    current_wr = test_wins / test_total if test_total > 0 else 0

    # Симуляция с новыми параметрами (упрощённо: фильтруем по min_score)
    new_min_score = new_params.get("min_score", 15)
    # Симулируем: с повышенным min_score часть сделок не вошла бы
    # Для упрощения: дропаем random долю сделок пропорционально повышению min_score
    old_min_score = new_params.get("_old_min_score", 10)
    drop_ratio = max(0, (new_min_score - old_min_score) / 50)  # 0..1

    if drop_ratio > 0:
        import random as _random
        kept = [r for r in test_trades if _random.random() > drop_ratio * 0.3]
    else:
        kept = list(test_trades)

    sim_wins = sum(1 for r in kept if float(r["pnl"] or 0) > 0)
    sim_total = len(kept)
    simulated_wr = sim_wins / sim_total if sim_total > 0 else 0

    # Одобряем если simulated WR не хуже чем -5% от current
    approved = simulated_wr >= current_wr * 0.95

    return {
        "approved": approved,
        "current_wr": round(current_wr, 3),
        "simulated_wr": round(simulated_wr, 3),
        "test_trades": test_total,
        "sim_trades": sim_total,
        "drop_ratio": round(drop_ratio, 2),
    }


# ══════════════════════════════════════════════════════
# V6: HUMAN-READABLE EXPLANATIONS
# ══════════════════════════════════════════════════════

def generate_explanation(change: dict) -> str:
    """Сгенерировать человеко-читаемое объяснение изменения параметра."""
    reasons = []

    if "min_score" in change:
        adj = change["min_score"]
        if isinstance(adj, dict):
            reasons.append(
                f"min_score {adj.get('from', '?')}→{adj.get('to', '?')}: "
                f"{adj.get('reason', 'threshold adjustment')}"
            )

    if "sl_pct" in change:
        adj = change["sl_pct"]
        if isinstance(adj, dict):
            reasons.append(
                f"sl_pct {adj.get('from', '?')}→{adj.get('to', '?')}: "
                f"{adj.get('reason', 'SL ratio adjustment')}"
            )

    if "tp_mult" in change:
        adj = change["tp_mult"]
        if isinstance(adj, dict):
            reasons.append(
                f"tp_mult {adj.get('from', '?')}→{adj.get('to', '?')}: "
                f"{adj.get('reason', 'TP ratio adjustment')}"
            )

    # Per-symbol changes
    for key, adj in change.items():
        if key.startswith("symbol_") and isinstance(adj, dict):
            sym = key.replace("symbol_", "")
            if "min_score" in adj:
                reasons.append(f"{sym}: min_score→{adj['min_score']} (per-symbol auto-tune)")
            if "sl_pct" in adj:
                reasons.append(f"{sym}: sl_pct→{adj['sl_pct']}% (per-symbol auto-tune)")

    if "pnl_guard" in change:
        reasons.append(f"⚠️ PnL warning: {change['pnl_guard'].get('reason', 'negative')}")

    return " | ".join(reasons) if reasons else "no explanation generated"


# ══════════════════════════════════════════════════════
# V7: COMPOSITE SCORE (multi-objective optimization)
# ══════════════════════════════════════════════════════

def composite_score(trades: list) -> dict:
    """Многофакторная оценка: WR + Profit Factor + Sharpe + MaxDD + Avg Hold.

    Возвращает {score, wr, pf, sharpe, max_dd, avg_hold}.
    score: 0..1, выше = лучше.
    """
    if not trades:
        return {"score": 0.5, "wr": 0, "pf": 0, "sharpe": 0, "max_dd": 0, "avg_hold": 0}

    wins = [t for t in trades if (t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) > 0]
    losses = [t for t in trades if (t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) <= 0]

    n = len(trades)
    wr = len(wins) / n if n > 0 else 0

    # Profit Factor
    gross_profit = sum(abs(t.get("pnl", 0)) if isinstance(t, dict) else abs(getattr(t, "pnl", 0))
                        for t in wins)
    gross_loss = sum(abs(t.get("pnl", 0)) if isinstance(t, dict) else abs(getattr(t, "pnl", 0))
                     for t in losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else (2.0 if gross_profit > 0 else 0.5)

    # Sharpe ratio (упрощённый: mean/std возвратов)
    returns = [(t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) for t in trades]
    mean_ret = sum(returns) / n if n > 0 else 0
    variance = sum((r - mean_ret) ** 2 for r in returns) / n if n > 1 else 1
    std_ret = variance ** 0.5
    sharpe = mean_ret / std_ret if std_ret > 0 else 0

    # Max Drawdown (кумулятивный)
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Avg hold
    holds = [(t.get("hold_hours", 0) if isinstance(t, dict) else getattr(t, "hold_hours", 0))
             for t in trades]
    avg_hold = sum(holds) / n if n > 0 else 0

    # Нормализация и взвешивание
    pf_norm = min(pf / 3.0, 1.0) if pf < 3.0 else 1.0
    sharpe_norm = min(max(sharpe, -1) / 2.5 + 0.4, 1.0)  # маппим -1..1.5 → 0..1
    # MaxDD: <$15 = 1.0, >$100 = 0
    dd_norm = max(0, 1.0 - max_dd / 100.0) if max_dd > 0 else 1.0
    # Hold: <4ч = 1.0, >48ч = 0
    hold_norm = max(0, 1.0 - avg_hold / 48.0)

    score = (
        0.30 * wr +
        0.25 * pf_norm +
        0.20 * sharpe_norm +
        0.15 * dd_norm +
        0.10 * hold_norm
    )

    return {
        "score": round(score, 3),
        "wr": round(wr, 3),
        "pf": round(pf, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "avg_hold": round(avg_hold, 1),
    }


# ══════════════════════════════════════════════════════
# V7: REGIME-SPECIFIC PARAMETERS
# ══════════════════════════════════════════════════════

REGIME_PARAMS_PATH = DATA_DIR / "regime_params.json"

DEFAULT_REGIME_PARAMS = {
    "TRENDING_UP": {
        "min_score": 25, "sl_pct": 5.0, "tp_mult": 1.5,
        "max_positions": 10, "direction": "LONG_only",
    },
    "TRENDING_DOWN": {
        "min_score": 35, "sl_pct": 5.0, "tp_mult": 0.8,
        "max_positions": 5, "direction": "SHORT_only",
    },
    "RANGING": {
        "min_score": 30, "sl_pct": 5.0, "tp_mult": 1.0,
        "max_positions": 8, "direction": "BOTH",
    },
    "CHOPPY": {
        "min_score": 40, "sl_pct": 4.0, "tp_mult": 0.7,
        "max_positions": 3, "direction": "NONE",
    },
    "HIGH_VOL": {
        "min_score": 22, "sl_pct": 6.0, "tp_mult": 1.3,
        "max_positions": 12, "direction": "BOTH",
    },
    "LOW_VOL": {
        "min_score": 28, "sl_pct": 4.0, "tp_mult": 0.9,
        "max_positions": 6, "direction": "BOTH",
    },
}


def load_regime_params() -> dict:
    """Загрузить regime-specific параметры."""
    if REGIME_PARAMS_PATH.exists():
        try:
            return json.loads(REGIME_PARAMS_PATH.read_text())
        except Exception:
            pass
    return dict(DEFAULT_REGIME_PARAMS)


def save_regime_params(params: dict):
    """Сохранить regime-specific параметры."""
    REGIME_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGIME_PARAMS_PATH.write_text(json.dumps(params, indent=2))


def get_params_for_regime(regime: str = None) -> dict:
    """Получить параметры для текущего (или указанного) режима."""
    if regime is None:
        # Авто-определение из LSTM кеша
        cache = DATA_DIR / "lstm_regime_cache.json"
        if cache.exists():
            try:
                data = json.loads(cache.read_text())
                regime = data.get("regime", "RANGING")
            except Exception:
                regime = "RANGING"
        else:
            regime = "RANGING"

    params = load_regime_params()
    return params.get(regime, DEFAULT_REGIME_PARAMS.get(regime, {}))


def update_regime_params(regime: str, updates: dict, reason: str = ""):
    """Обновить параметры для конкретного режима (canary-promoted changes)."""
    params = load_regime_params()
    if regime not in params:
        params[regime] = dict(DEFAULT_REGIME_PARAMS.get(regime, {}))
    params[regime].update(updates)
    params[regime]["_updated"] = datetime.now().isoformat()
    if reason:
        params[regime]["_reason"] = reason
    save_regime_params(params)


# ══════════════════════════════════════════════════════
# V7: STRESS TESTING (historical crash scenarios)
# ══════════════════════════════════════════════════════

STRESS_SCENARIOS = [
    {
        "name": "COVID Crash (Mar 2020)",
        "description": "BTC -50% за неделю, все альты -60-80%",
        "btc_drop_pct": 50,
        "alt_drop_pct": 70,
        "volatility_mult": 5.0,
        "funding_rate": -0.05,  # extreme negative
    },
    {
        "name": "FTX Collapse (Nov 2022)",
        "description": "Паника, BTC -25%, SOL -60%, массовые ликвидации",
        "btc_drop_pct": 25,
        "alt_drop_pct": 40,
        "volatility_mult": 3.0,
        "funding_rate": -0.03,
    },
    {
        "name": "China Ban (May 2021)",
        "description": "BTC -35% за день, восстановление за 2 недели",
        "btc_drop_pct": 35,
        "alt_drop_pct": 50,
        "volatility_mult": 4.0,
        "funding_rate": -0.04,
    },
    {
        "name": "Luna Collapse (May 2022)",
        "description": "UST depeg → каскад ликвидаций, BTC -30%",
        "btc_drop_pct": 30,
        "alt_drop_pct": 55,
        "volatility_mult": 4.5,
        "funding_rate": -0.06,
    },
]


def stress_test_params(params: dict, trades: list = None) -> dict:
    """Проверить параметры на стресс-сценариях.

    Симулирует: что было бы с текущими позициями и параметрами в каждом кризисе.
    Возвращает: {passed: bool, scenarios: [{name, max_dd_sim, would_survive}]}
    """
    results = []
    all_pass = True

    # Текущие позиции (если есть)
    current_positions = []
    if trades:
        current_positions = [
            {"symbol": t.get("symbol", "UNKNOWN"), "pnl": t.get("pnl", 0),
             "side": t.get("side", "Buy")}
            for t in trades[-7:]  # последние 7 сделок
        ]

    for scenario in STRESS_SCENARIOS:
        # Симулируем влияние на позиции
        simulated_dd = 0
        pos_count = len(current_positions) if current_positions else 7

        if current_positions:
            for pos in current_positions:
                if pos["side"] == "Buy":  # LONG
                    simulated_dd += abs(float(pos.get("pnl", 0))) * scenario["alt_drop_pct"] / 15
                else:  # SHORT — профит в кризис
                    simulated_dd -= abs(float(pos.get("pnl", 0))) * scenario["alt_drop_pct"] / 30
        else:
            # Без данных: оцениваем по параметрам
            sl_pct = params.get("sl_pct", 5)
            max_pos = params.get("max_positions", 7)
            pos_size = 10  # ~$10 на позицию
            simulated_dd = max_pos * pos_size * (sl_pct / 100) * scenario["volatility_mult"] / 3

        sl_coverage = params.get("sl_pct", 5) >= scenario["btc_drop_pct"] / 5
        max_pos_ok = params.get("max_positions", 7) <= 8

        scenario_result = {
            "name": scenario["name"],
            "btc_drop": f"-{scenario['btc_drop_pct']}%",
            "simulated_dd": round(simulated_dd, 2),
            "sl_adequate": sl_coverage,
            "position_limit_ok": max_pos_ok,
            "would_survive": simulated_dd < 50 and sl_coverage,
        }

        if not scenario_result["would_survive"]:
            all_pass = False

        results.append(scenario_result)

    return {
        "passed": all_pass,
        "scenarios": results,
    }


# ══════════════════════════════════════════════════════
# V7: DRAWDOWN-BASED LEARNING
# ══════════════════════════════════════════════════════

def drawdown_adjustment(current_dd_pct: float = None) -> dict:
    """Адаптивные параметры в зависимости от текущей просадки.

    current_dd_pct: 0.05 = 5% drawdown, -0.05 = 5% profit.
    """
    if current_dd_pct is None:
        # Авто-расчёт из последних сделок
        try:
            import sqlite3 as _sql
            db = Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
            conn = _sql.connect(str(db))
            rows = conn.execute(
                "SELECT pnl FROM trade_history WHERE closed_at IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 50"
            ).fetchall()
            conn.close()
            pnls = [float(r[0] or 0) for r in rows]
            peak = 0
            cumulative = 0
            for p in reversed(pnls):
                cumulative += p
                peak = max(peak, cumulative)
            total_pnl = sum(pnls)
            current_dd_pct = (peak - cumulative) / abs(peak) if peak > 0 else 0
        except Exception:
            current_dd_pct = 0

    if current_dd_pct > 0.10:
        return {
            "min_score_mult": 1.3,
            "sl_pct_mult": 0.8,
            "position_size_mult": 0.5,
            "max_positions_mult": 0.6,
            "mode": "conservative",
        }
    elif current_dd_pct < 0 and abs(current_dd_pct) > 0.05:
        # В profit >5%: агрессивнее
        return {
            "min_score_mult": 0.85,
            "position_size_mult": 1.2,
            "mode": "normal",
        }
    else:
        # Нейтрально
        if abs(current_dd_pct) > 0.05:
            return {
                "min_score_mult": 0.9,
                "mode": "recovery",
            }
    return {"mode": "normal"}


# ══════════════════════════════════════════════════════
# V7: ADAPTIVE CANARY %
# ══════════════════════════════════════════════════════

def adaptive_canary_pct() -> float:
    """Динамический canary % на основе стабильности WR и волатильности."""
    try:
        import sqlite3 as _sql
        db = Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
        conn = _sql.connect(str(db))
        rows = conn.execute(
            "SELECT pnl FROM trade_history WHERE closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 100"
        ).fetchall()
        conn.close()

        if len(rows) < 30:
            return CANARY_ENTRY_PCT  # default

        pnls = [float(r[0] or 0) for r in rows]
        recent_wr = sum(1 for p in pnls if p > 0) / len(pnls)

        # Волатильность WR: считаем WR по sliding windows
        wr_readings = []
        window = min(15, len(pnls) // 3)
        for i in range(0, len(pnls) - window, window):
            chunk = pnls[i:i + window]
            chunk_wr = sum(1 for p in chunk if p > 0) / len(chunk)
            wr_readings.append(chunk_wr)

        if len(wr_readings) >= 3:
            mean_wr = sum(wr_readings) / len(wr_readings)
            wr_volatility = (
                sum((w - mean_wr) ** 2 for w in wr_readings) / len(wr_readings)
            ) ** 0.5
        else:
            wr_volatility = 0.05

        if wr_volatility < 0.03 and recent_wr > 0.50:
            return 0.20  # стабильно → больше экспериментов
        elif wr_volatility > 0.10 or recent_wr < 0.40:
            return 0.05  # нестабильно → меньше риска
        else:
            return 0.10  # default
    except Exception:
        return CANARY_ENTRY_PCT


# ══════════════════════════════════════════════════════
# V8: THOMPSON SAMPLING (замена фиксированного canary %)
# ══════════════════════════════════════════════════════

class ParameterBandit:
    """Multi-armed bandit: каждый набор параметров = 'рука'.

    Thompson Sampling: выбирает руку с max sampled reward из Beta posterior.
    Автоматически балансирует exploration vs exploitation.
    """

    def __init__(self, param_sets: list = None):
        """
        Args:
            param_sets: [{min_score: 25, sl_pct: 5}, {min_score: 30, sl_pct: 4}, ...]
        """
        if param_sets is None:
            param_sets = [
                {"min_score": 25, "sl_pct": 5.0, "tp_mult": 1.5},
                {"min_score": 30, "sl_pct": 5.0, "tp_mult": 1.2},
                {"min_score": 35, "sl_pct": 4.0, "tp_mult": 1.0},
            ]
        self.arms = [
            {"params": p, "alpha": 1.0, "beta": 1.0, "trades": 0, "wins": 0}
            for p in param_sets
        ]

    def select_arm(self) -> dict:
        """Thompson Sampling: выбрать руку с max sampled Beta."""
        import random as _random
        samples = [
            _random.betavariate(arm["alpha"], arm["beta"])
            for arm in self.arms
        ]
        best_idx = max(range(len(samples)), key=lambda i: samples[i])
        return self.arms[best_idx]["params"], best_idx

    def update(self, arm_idx: int, win: bool):
        """Обновить Beta posterior после сделки."""
        if 0 <= arm_idx < len(self.arms):
            self.arms[arm_idx]["trades"] += 1
            if win:
                self.arms[arm_idx]["alpha"] += 1
                self.arms[arm_idx]["wins"] += 1
            else:
                self.arms[arm_idx]["beta"] += 1

    def get_best_arm(self) -> dict:
        """Текущая лучшая рука по mean posterior."""
        means = [(a["alpha"] / (a["alpha"] + a["beta"]), i)
                 for i, a in enumerate(self.arms)]
        best_idx = max(means, key=lambda x: x[0])[1]
        arm = self.arms[best_idx]
        return {
            "params": arm["params"],
            "wr": round(arm["alpha"] / (arm["alpha"] + arm["beta"]), 3),
            "trades": arm["trades"],
            "confidence": round(max(means)[0], 3),
        }

    def to_dict(self) -> dict:
        """Сериализация для сохранения."""
        return {"arms": self.arms}

    @classmethod
    def from_dict(cls, data: dict) -> "ParameterBandit":
        """Восстановление из сохранённого состояния."""
        bandit = cls(param_sets=[])
        bandit.arms = data.get("arms", [])
        return bandit


BANDIT_PATH = DATA_DIR / "parameter_bandit.json"


def save_bandit(bandit: ParameterBandit):
    """Сохранить состояние bandit."""
    BANDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    BANDIT_PATH.write_text(json.dumps(bandit.to_dict(), indent=2))


def load_bandit() -> ParameterBandit:
    """Загрузить состояние bandit."""
    if BANDIT_PATH.exists():
        try:
            return ParameterBandit.from_dict(json.loads(BANDIT_PATH.read_text()))
        except Exception:
            pass
    return ParameterBandit()


# ══════════════════════════════════════════════════════
# V8: MONTE CARLO STRESS TEST (1000 random crash sims)
# ══════════════════════════════════════════════════════

def monte_carlo_stress_test(params: dict, n_simulations: int = 1000,
                             capital: float = 200) -> dict:
    """Генерация синтетических crash-сценариев.

    Симулирует 1000 случайных кризисов и оценивает устойчивость параметров.
    """
    import random as _random
    results = []

    sl_pct = params.get("sl_pct", 5)
    max_pos = params.get("max_positions", 7)
    tp_mult = params.get("tp_mult", 1.0)

    for _ in range(n_simulations):
        # Случайный crash-профиль
        btc_drop = _random.uniform(0.20, 0.60)       # BTC: -20% to -60%
        alt_drop = btc_drop * _random.uniform(1.2, 2.0)  # Alts: 1.2x-2x BTC
        duration_h = _random.uniform(1, 72)            # 1-72ч
        vol_spike = _random.uniform(2.0, 8.0)          # 2x-8x волатильность

        # Симуляция: каждая позиция теряет min(SL%, alt_drop * vol_factor)
        # При vol_spike=5, alt_drop=50%: позиция может потерять до SL%
        position_losses = []
        pos_size = capital * 0.05  # 5% на позицию

        for _ in range(max_pos):
            effective_drop = min(alt_drop / 100, sl_pct / 100) * vol_spike / 3
            loss = pos_size * effective_drop
            # Часть позиций в SHORT — профит в crash
            if _random.random() < 0.3:  # 30% SHORT
                loss = -loss * 0.5
            position_losses.append(loss)

        total_pnl = -sum(position_losses)
        # SL срабатывает и ограничивает убыток
        sl_capped = min(total_pnl, max_pos * pos_size * (sl_pct / 100))
        results.append(sl_capped)

    # Статистика
    results_sorted = sorted(results)
    p5 = results_sorted[int(n_simulations * 0.05)]
    p25 = results_sorted[int(n_simulations * 0.25)]
    median = results_sorted[n_simulations // 2]
    p95 = results_sorted[int(n_simulations * 0.95)]

    cvar_5 = sum(r for r in results if r <= p5) / max(1, sum(1 for r in results if r <= p5))
    prob_ruin = sum(1 for r in results if r < -capital * 0.5) / n_simulations  # потеря >50%

    return {
        "p5_loss": round(p5, 2),
        "p25_loss": round(p25, 2),
        "median_loss": round(median, 2),
        "p95_loss": round(p95, 2),
        "cvar_5": round(cvar_5, 2),
        "max_loss": round(min(results), 2),
        "prob_ruin": round(prob_ruin, 4),
        "passed": prob_ruin < 0.05,  # <5% вероятность разорения
        "n_simulations": n_simulations,
    }


# ══════════════════════════════════════════════════════
# V8: CONCEPT DRIFT DETECTOR (ADWIN-based simplified)
# ══════════════════════════════════════════════════════

class ConceptDriftDetector:
    """Обнаружение дрейфа: стратегия перестала работать.

    Упрощённый ADWIN: скользящее окно + сравнение с baseline.
    """

    def __init__(self, window_size: int = 100, threshold: float = 0.05,
                 min_samples: int = 30, confirm_window: int = 20):
        self.window_size = window_size
        self.threshold = threshold
        self.min_samples = min_samples
        self.confirm_window = confirm_window
        self.recent_outcomes = []  # 1.0=win, 0.0=loss
        self.baseline_wr = None
        self.drift_detected = False
        self.drift_since = None

    def update(self, win: bool) -> bool:
        """Добавить исход сделки. Возвращает True если дрейф обнаружен."""
        self.recent_outcomes.append(1.0 if win else 0.0)
        if len(self.recent_outcomes) > self.window_size:
            self.recent_outcomes = self.recent_outcomes[-self.window_size:]

        if len(self.recent_outcomes) < self.min_samples:
            return False

        current_wr = sum(self.recent_outcomes) / len(self.recent_outcomes)

        if self.baseline_wr is None:
            self.baseline_wr = current_wr
            return False

        drift = self.baseline_wr - current_wr

        if drift > self.threshold and not self.drift_detected:
            # Подтверждение: последние N сделок
            if len(self.recent_outcomes) >= self.confirm_window:
                recent_wr = sum(self.recent_outcomes[-self.confirm_window:]) / self.confirm_window
                if recent_wr < 0.30:
                    self.drift_detected = True
                    self.drift_since = datetime.now()
                    return True

        # Сброс если восстановились
        if self.drift_detected and drift < self.threshold * 0.5:
            self.drift_detected = False
            self.drift_since = None
            self.baseline_wr = current_wr  # новый baseline

        return False

    def get_status(self) -> dict:
        """Текущий статус детектора."""
        current_wr = sum(self.recent_outcomes) / max(1, len(self.recent_outcomes))
        return {
            "drift_detected": self.drift_detected,
            "drift_since": self.drift_since.isoformat() if self.drift_since else None,
            "current_wr": round(current_wr, 3),
            "baseline_wr": round(self.baseline_wr, 3) if self.baseline_wr else None,
            "drift_delta": round((self.baseline_wr or 0) - current_wr, 3),
            "recent_trades": len(self.recent_outcomes),
        }

    def on_drift_detected(self) -> dict:
        """Рекомендации при обнаружении дрейфа."""
        return {
            "action": "conservative",
            "min_score_mult": 1.5,
            "position_size_mult": 0.5,
            "canary_pct": 0.0,  # отключить эксперименты
            "alert": "Strategy drift detected — auto-conservative mode",
            "auto_reset_hours": 48,
        }


DRIFT_STATE_PATH = DATA_DIR / "drift_detector.json"

# Глобальный экземпляр
_drift_detector: ConceptDriftDetector = None


def get_drift_detector() -> ConceptDriftDetector:
    global _drift_detector
    if _drift_detector is None:
        _drift_detector = ConceptDriftDetector()
    return _drift_detector


# ══════════════════════════════════════════════════════
# V8: ANOMALY DETECTION (Isolation Forest для трейдов)
# ══════════════════════════════════════════════════════

def detect_anomalous_trades(trades: list) -> dict:
    """Исключить аномальные сделки из self-learning.

    Использует Isolation Forest на features: pnl, hold_time, entry_score.
    Без внешних зависимостей — упрощённая версия через IQR.
    """
    if len(trades) < 20:
        return {"normal": trades, "anomalous": [], "normal_count": len(trades),
                "anomalous_count": 0}

    # Извлекаем pnl для outlier detection (IQR метод)
    pnls = [
        abs(t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0))
        for t in trades
    ]

    # IQR-based outlier detection
    pnls_sorted = sorted(pnls)
    q1 = pnls_sorted[len(pnls) // 4]
    q3 = pnls_sorted[3 * len(pnls) // 4]
    iqr = q3 - q1
    upper_bound = q3 + 3.0 * iqr  # 3× IQR (консервативно)

    normal = []
    anomalous = []
    for i, t in enumerate(trades):
        pnl = abs(t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0))
        hold = t.get("hold_hours", 0) if isinstance(t, dict) else getattr(t, "hold_hours", 0)

        # Аномалия: экстремальный PnL ИЛИ слишком долгий hold (>100h)
        if pnl > upper_bound or hold > 100:
            anomalous.append(t)
        else:
            normal.append(t)

    return {
        "normal": normal,
        "anomalous": anomalous,
        "normal_count": len(normal),
        "anomalous_count": len(anomalous),
        "iqr_threshold": round(upper_bound, 2),
    }


# ══════════════════════════════════════════════════════
# V8: ADAPTIVE COMPOSITE WEIGHTS (per-regime)
# ══════════════════════════════════════════════════════

COMPOSITE_WEIGHTS = {
    "TRENDING_UP":   {"wr": 0.35, "pf": 0.20, "sharpe": 0.20, "dd": 0.15, "hold": 0.10},
    "TRENDING_DOWN": {"wr": 0.20, "pf": 0.25, "sharpe": 0.15, "dd": 0.30, "hold": 0.10},
    "RANGING":       {"wr": 0.25, "pf": 0.30, "sharpe": 0.20, "dd": 0.15, "hold": 0.10},
    "HIGH_VOL":      {"wr": 0.15, "pf": 0.20, "sharpe": 0.15, "dd": 0.40, "hold": 0.10},
    "LOW_VOL":       {"wr": 0.30, "pf": 0.25, "sharpe": 0.25, "dd": 0.10, "hold": 0.10},
    "CHOPPY":        {"wr": 0.20, "pf": 0.20, "sharpe": 0.15, "dd": 0.35, "hold": 0.10},
}


def composite_score_v8(trades: list, regime: str = None) -> dict:
    """Composite score с адаптивными весами по режиму рынка."""
    if regime is None:
        regime = "RANGING"
        cache = DATA_DIR / "lstm_regime_cache.json"
        if cache.exists():
            try:
                regime = json.loads(cache.read_text()).get("regime", "RANGING")
            except Exception:
                pass

    weights = COMPOSITE_WEIGHTS.get(regime, COMPOSITE_WEIGHTS["RANGING"])

    if not trades:
        return {"score": 0.5, "wr": 0, "pf": 0, "sharpe": 0, "max_dd": 0,
                "avg_hold": 0, "regime": regime, "weights": weights}

    n = len(trades)
    pnls = [(t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) for t in trades]
    holds = [(t.get("hold_hours", 0) if isinstance(t, dict) else getattr(t, "hold_hours", 0))
             for t in trades]

    wr = sum(1 for p in pnls if p > 0) / n

    # Profit Factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(abs(p) for p in pnls if p < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else (2.0 if gross_profit > 0 else 0.5)
    pf_norm = min(pf / 3.0, 1.0)

    # Sharpe
    mean_pnl = sum(pnls) / n
    variance = sum((p - mean_pnl) ** 2 for p in pnls) / max(1, n - 1)
    std_pnl = variance ** 0.5
    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
    sharpe_norm = min(max(sharpe + 1, 0) / 2.5, 1.0)

    # MaxDD
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    dd_norm = max(0, 1.0 - max_dd / 100.0)

    # AvgHold
    avg_hold = sum(holds) / n
    hold_norm = max(0, 1.0 - avg_hold / 48.0)

    score = (
        weights["wr"] * wr +
        weights["pf"] * pf_norm +
        weights["sharpe"] * sharpe_norm +
        weights["dd"] * dd_norm +
        weights["hold"] * hold_norm
    )

    return {
        "score": round(score, 3),
        "wr": round(wr, 3),
        "pf": round(pf, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 2),
        "avg_hold": round(avg_hold, 1),
        "regime": regime,
        "weights": weights,
    }


# ══════════════════════════════════════════════════════
# V8: PARAMETER VERSIONING (Git-like)
# ══════════════════════════════════════════════════════

PARAMS_HISTORY_DIR = DATA_DIR / "params_history"


def save_params_version(params: dict, reason: str, parent_version: str = None) -> str:
    """Сохранить снепшот параметров как версию."""
    PARAMS_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    # Определяем номер версии
    existing = sorted(PARAMS_HISTORY_DIR.glob("v*.json"))
    next_num = len(existing) + 1
    version_id = f"v{next_num:03d}"

    snapshot = {
        "version": version_id,
        "parent": parent_version,
        "timestamp": datetime.now().isoformat(),
        "params": params,
        "reason": reason,
    }

    (PARAMS_HISTORY_DIR / f"{version_id}.json").write_text(json.dumps(snapshot, indent=2))

    # HEAD
    (PARAMS_HISTORY_DIR / "HEAD.json").write_text(json.dumps(
        {"version": version_id, "timestamp": snapshot["timestamp"]}, indent=2
    ))

    return version_id


def get_params_history(limit: int = 20) -> list:
    """Список последних версий параметров."""
    if not PARAMS_HISTORY_DIR.exists():
        return []
    files = sorted(PARAMS_HISTORY_DIR.glob("v*.json"), reverse=True)[:limit]
    history = []
    for f in files:
        try:
            snap = json.loads(f.read_text())
            history.append({
                "version": snap["version"],
                "timestamp": snap["timestamp"],
                "reason": snap.get("reason", ""),
                "params": snap.get("params", {}),
            })
        except Exception:
            pass
    return history


# ══════════════════════════════════════════════════════
# V9: DYNAMIC THOMPSON SAMPLING (auto-generate + prune)
# ══════════════════════════════════════════════════════

class DynamicParameterBandit:
    """Thompson Sampling с авто-генерацией и прунингом рук.

    Каждые 24ч: удаляет 2 худшие руки, добавляет 2 вариации лучшей.
    """

    def __init__(self, base_params: dict = None, n_arms: int = 5):
        import random as _random
        if base_params is None:
            base_params = {"min_score": 30, "sl_pct": 5.0, "tp_mult": 1.2}
        self.base_params = dict(base_params)
        self._random = _random
        self.arms = [self._perturb(base_params) for _ in range(n_arms)]
        self.last_pruning = time.time()

    def _perturb(self, params: dict) -> dict:
        """Генерация вариации параметров с muted random walk."""
        return {
            "params": {
                "min_score": max(15, min(45, params["min_score"] + self._random.randint(-7, 7))),
                "sl_pct": round(max(2.0, min(10.0, params["sl_pct"] * self._random.uniform(0.75, 1.25))), 1),
                "tp_mult": round(max(0.5, min(2.0, params["tp_mult"] * self._random.uniform(0.75, 1.25))), 1),
            },
            "alpha": 1.0, "beta": 1.0, "trades": 0, "wins": 0,
        }

    def select_arm(self) -> tuple:
        """Thompson Sampling: выбрать руку с max sampled Beta."""
        samples = [self._random.betavariate(a["alpha"], a["beta"]) for a in self.arms]
        best_idx = max(range(len(samples)), key=lambda i: samples[i])
        return self.arms[best_idx]["params"], best_idx

    def update(self, arm_idx: int, win: bool):
        """Обновить Beta posterior."""
        if 0 <= arm_idx < len(self.arms):
            self.arms[arm_idx]["trades"] += 1
            if win:
                self.arms[arm_idx]["alpha"] += 1
                self.arms[arm_idx]["wins"] += 1
            else:
                self.arms[arm_idx]["beta"] += 1

    def prune_and_regenerate(self):
        """Удалить 2 худшие руки, добавить 2 вариации лучшей."""
        if time.time() - self.last_pruning < 86400:
            return

        if len(self.arms) < 5:
            return

        # Сортируем по mean reward
        def mean_reward(arm):
            return arm["alpha"] / (arm["alpha"] + arm["beta"])

        sorted_arms = sorted(self.arms, key=mean_reward, reverse=True)
        best = sorted_arms[0]["params"]

        # Оставляем топ-3, добавляем 2 новых
        self.arms = sorted_arms[:3]
        self.arms.append(self._perturb(best))
        self.arms.append(self._perturb(best))
        self.last_pruning = time.time()

    def get_best_arm(self) -> dict:
        means = [(a["alpha"] / (a["alpha"] + a["beta"]), i) for i, a in enumerate(self.arms)]
        best_idx = max(means, key=lambda x: x[0])[1]
        arm = self.arms[best_idx]
        return {
            "params": arm["params"],
            "wr": round(arm["alpha"] / (arm["alpha"] + arm["beta"]), 3),
            "trades": arm["trades"],
            "n_arms": len(self.arms),
        }

    def to_dict(self) -> dict:
        return {"arms": self.arms, "base_params": self.base_params,
                "last_pruning": self.last_pruning}

    @classmethod
    def from_dict(cls, data: dict) -> "DynamicParameterBandit":
        bandit = cls(base_params=data.get("base_params"), n_arms=0)
        bandit.arms = data.get("arms", [])
        bandit.last_pruning = data.get("last_pruning", 0)
        return bandit


DYNAMIC_BANDIT_PATH = DATA_DIR / "dynamic_bandit.json"


def save_dynamic_bandit(bandit: DynamicParameterBandit):
    DYNAMIC_BANDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DYNAMIC_BANDIT_PATH.write_text(json.dumps(bandit.to_dict(), indent=2))


def load_dynamic_bandit() -> DynamicParameterBandit:
    if DYNAMIC_BANDIT_PATH.exists():
        try:
            return DynamicParameterBandit.from_dict(json.loads(DYNAMIC_BANDIT_PATH.read_text()))
        except Exception:
            pass
    return DynamicParameterBandit()


# ══════════════════════════════════════════════════════
# V9: CALIBRATED MONTE CARLO (Pareto heavy-tail)
# ══════════════════════════════════════════════════════

def monte_carlo_stress_test_v9(params: dict, n_simulations: int = 1000,
                                 capital: float = 200) -> dict:
    """Monte Carlo с Pareto-distributed crash severity (heavy tails).

    Вместо uniform: BTC drop ~ Pareto(α=2.5), alt ratio ~ correlated.
    """
    import random as _random
    results = []
    sl_pct = params.get("sl_pct", 5)
    max_pos = params.get("max_positions", 7)
    pos_size = capital * 0.05

    for _ in range(n_simulations):
        # Pareto heavy-tail: BTC drop distribution
        # P(X > x) = (x_min/x)^α. α=2.5 даёт realistic tail risk.
        alpha = 2.5
        x_min = 0.15  # minimum crash: 15%
        btc_drop = x_min / (_random.random() ** (1.0 / alpha))  # inverse CDF
        btc_drop = min(btc_drop, 0.80)  # cap at 80%

        # Alt ratio: correlated with BTC drop (bigger crash → higher ratio)
        base_ratio = 1.2 + 0.8 * (btc_drop - 0.15) / 0.65  # 1.2 to 2.0 mapped
        alt_ratio = base_ratio + _random.gauss(0, 0.2)
        alt_ratio = max(1.1, min(2.5, alt_ratio))
        alt_drop = btc_drop * alt_ratio

        vol_spike = 2.0 + 6.0 * (btc_drop / 0.8) ** 0.5  # корень из crash severity

        position_losses = []
        for _ in range(max_pos):
            effective_drop = min(alt_drop, sl_pct / 100) * vol_spike / 3
            loss = pos_size * effective_drop
            if _random.random() < 0.3:
                loss = -loss * 0.5
            position_losses.append(loss)

        total_pnl = -sum(position_losses)
        sl_capped = min(total_pnl, max_pos * pos_size * (sl_pct / 100))
        results.append(sl_capped)

    # Statistics
    results_sorted = sorted(results)
    p5 = results_sorted[int(n_simulations * 0.05)]
    p25 = results_sorted[int(n_simulations * 0.25)]
    median = results_sorted[n_simulations // 2]
    p95 = results_sorted[int(n_simulations * 0.95)]

    cvar_5 = sum(r for r in results if r <= p5) / max(1, sum(1 for r in results if r <= p5))
    prob_ruin = sum(1 for r in results if r < -capital * 0.5) / n_simulations

    return {
        "p5_loss": round(p5, 2),
        "p25_loss": round(p25, 2),
        "median_loss": round(median, 2),
        "p95_loss": round(p95, 2),
        "cvar_5": round(cvar_5, 2),
        "max_loss": round(min(results), 2),
        "prob_ruin": round(prob_ruin, 4),
        "passed": prob_ruin < 0.05,
        "distribution": "Pareto(α=2.5, tail-heavy)",
        "n_simulations": n_simulations,
    }


# ══════════════════════════════════════════════════════
# V9: REGIME-AWARE DRIFT DETECTOR
# ══════════════════════════════════════════════════════

class RegimeAwareDriftDetector:
    """Детектор дрейфа с per-regime окнами и EMA baseline.

    Разные режимы имеют разную волатильность WR:
    - HIGH_VOL: окно 30 (быстрое обнаружение)
    - TRENDING_UP: окно 50
    - RANGING: окно 100
    - TRENDING_DOWN: окно 80
    """

    def __init__(self):
        self.windows = {
            "TRENDING_UP": [],
            "TRENDING_DOWN": [],
            "RANGING": [],
            "HIGH_VOL": [],
            "LOW_VOL": [],
            "CHOPPY": [],
        }
        self.window_sizes = {
            "TRENDING_UP": 50, "TRENDING_DOWN": 80, "RANGING": 100,
            "HIGH_VOL": 30, "LOW_VOL": 120, "CHOPPY": 60,
        }
        self.baselines = {}       # per-regime EMA baseline
        self.ema_alpha = 0.10     # скорость обновления baseline
        self.drift_detected = False
        self.drift_regime = None
        self.drift_since = None

    def update(self, regime: str, win: bool) -> bool:
        """Добавить исход сделки в окно её режима."""
        if regime not in self.windows:
            regime = "RANGING"

        window = self.windows[regime]
        window.append(1.0 if win else 0.0)
        max_size = self.window_sizes.get(regime, 100)
        if len(window) > max_size:
            self.windows[regime] = window[-max_size:]

        if len(window) < 15:
            return False

        current_wr = sum(window) / len(window)

        if regime not in self.baselines:
            self.baselines[regime] = current_wr
            return False

        baseline_wr = self.baselines[regime]
        drift = baseline_wr - current_wr
        threshold = 0.10  # 10% drop for regime-specific

        if drift > threshold and not self.drift_detected:
            if len(window) >= 10:
                recent_wr = sum(window[-10:]) / 10
                if recent_wr < 0.30:
                    self.drift_detected = True
                    self.drift_regime = regime
                    self.drift_since = datetime.now()
                    return True

        # Recovery: drift практически исчез
        if self.drift_detected and drift < threshold * 0.3:
            self.drift_detected = False
            self.drift_regime = None
            self.drift_since = None
            self.baselines[regime] = current_wr

        # EMA update baseline
        self.baselines[regime] = (
            (1 - self.ema_alpha) * self.baselines[regime]
            + self.ema_alpha * current_wr
        )

        return False

    def get_status(self) -> dict:
        result = {
            "drift_detected": self.drift_detected,
            "drift_regime": self.drift_regime,
            "drift_since": self.drift_since.isoformat() if self.drift_since else None,
            "per_regime": {},
        }
        for regime, window in self.windows.items():
            if len(window) < 5:
                continue
            wr = sum(window) / len(window)
            baseline = self.baselines.get(regime, wr)
            result["per_regime"][regime] = {
                "trades": len(window),
                "wr": round(wr, 3),
                "baseline": round(baseline, 3),
                "delta": round(baseline - wr, 3),
            }
        return result

    def on_drift_detected(self) -> dict:
        return {
            "action": "conservative",
            "drift_regime": self.drift_regime,
            "min_score_mult": 1.5,
            "position_size_mult": 0.5,
            "canary_pct": 0.0,
            "alert": f"Regime-specific drift in {self.drift_regime}",
        }


# Глобальный экземпляр
_regime_drift_detector: RegimeAwareDriftDetector = None


def get_regime_drift_detector() -> RegimeAwareDriftDetector:
    global _regime_drift_detector
    if _regime_drift_detector is None:
        _regime_drift_detector = RegimeAwareDriftDetector()
    return _regime_drift_detector


# ══════════════════════════════════════════════════════
# V9: ENSEMBLE LEARNING (per-regime bandits)
# ══════════════════════════════════════════════════════

class ParameterEnsemble:
    """Ансамбль: отдельный DynamicParameterBandit для каждого режима."""

    REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOL", "LOW_VOL", "CHOPPY"]

    def __init__(self):
        base_params = {
            "TRENDING_UP": {"min_score": 25, "sl_pct": 5.0, "tp_mult": 1.5},
            "TRENDING_DOWN": {"min_score": 35, "sl_pct": 5.0, "tp_mult": 0.8},
            "RANGING": {"min_score": 30, "sl_pct": 5.0, "tp_mult": 1.0},
            "HIGH_VOL": {"min_score": 22, "sl_pct": 6.0, "tp_mult": 1.3},
            "LOW_VOL": {"min_score": 28, "sl_pct": 4.0, "tp_mult": 0.9},
            "CHOPPY": {"min_score": 40, "sl_pct": 4.0, "tp_mult": 0.7},
        }
        self.bandits = {
            regime: DynamicParameterBandit(base_params[regime], n_arms=4)
            for regime in self.REGIMES
        }

    def select_params(self, regime: str = None) -> tuple:
        """Выбрать параметры для текущего режима."""
        if regime is None or regime not in self.bandits:
            regime = "RANGING"
        return self.bandits[regime].select_arm()

    def update(self, regime: str, arm_idx: int, win: bool):
        """Обновить bandit для конкретного режима."""
        if regime in self.bandits:
            self.bandits[regime].update(arm_idx, win)
            self.bandits[regime].prune_and_regenerate()

    def get_best_for_regime(self, regime: str) -> dict:
        if regime in self.bandits:
            return self.bandits[regime].get_best_arm()
        return {}

    def get_all_best(self) -> dict:
        return {r: self.bandits[r].get_best_arm() for r in self.REGIMES}

    def to_dict(self) -> dict:
        return {r: b.to_dict() for r, b in self.bandits.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "ParameterEnsemble":
        ensemble = cls()
        for regime, bandit_data in data.items():
            if regime in ensemble.bandits:
                ensemble.bandits[regime] = DynamicParameterBandit.from_dict(bandit_data)
        return ensemble


ENSEMBLE_PATH = DATA_DIR / "parameter_ensemble.json"


def save_ensemble(ensemble: ParameterEnsemble):
    ENSEMBLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENSEMBLE_PATH.write_text(json.dumps(ensemble.to_dict(), indent=2))


def load_ensemble() -> ParameterEnsemble:
    if ENSEMBLE_PATH.exists():
        try:
            return ParameterEnsemble.from_dict(json.loads(ENSEMBLE_PATH.read_text()))
        except Exception:
            pass
    return ParameterEnsemble()


# ══════════════════════════════════════════════════════
# V9: ONLINE MICRO-UPDATES (after each trade)
# ══════════════════════════════════════════════════════

def on_trade_closed(symbol: str, side: str, pnl: float, hold_hours: float,
                    exit_reason: str, regime: str = None):
    """Вызывается после КАЖДОЙ закрытой сделки для микро-обучения.

    Инкрементальные обновления без canary: bandit posterior, drift, regime stats.
    """
    win = pnl > 0

    # 1. Ensemble bandit update
    try:
        ensemble = load_ensemble()
        # Определяем текущий режим
        if regime is None:
            cache = DATA_DIR / "lstm_regime_cache.json"
            if cache.exists():
                try:
                    regime = json.loads(cache.read_text()).get("regime", "RANGING")
                except Exception:
                    regime = "RANGING"
            else:
                regime = "RANGING"

        # Update bandit для этого режима
        _, arm_idx = ensemble.select_params(regime)
        ensemble.update(regime, arm_idx, win)
        save_ensemble(ensemble)
    except Exception:
        pass

    # 2. Regime-aware drift detector
    try:
        dd = get_regime_drift_detector()
        dd.update(regime or "RANGING", win)
    except Exception:
        pass

    # 3. Update symbol profile (incremental)
    try:
        from .journal.self_learn import _load_symbol_profiles, _save_symbol_profiles, _profiles_lock
        with _profiles_lock:
            profiles = _load_symbol_profiles()
        if symbol not in profiles:
            profiles[symbol] = {"trades": 0, "wins": 0, "total_pnl": 0.0, "total_hold": 0.0}
        profiles[symbol]["trades"] += 1
        if win:
            profiles[symbol]["wins"] += 1
        profiles[symbol]["total_pnl"] += pnl
        profiles[symbol]["total_hold"] += hold_hours
        profiles[symbol]["last_exit_reason"] = exit_reason
        profiles[symbol]["_updated"] = datetime.now().isoformat()
        with _profiles_lock:
            _save_symbol_profiles(profiles)
    except Exception:
        pass


# ══════════════════════════════════════════════════════
# V10: ROBUST MICRO-UPDATES (outlier protection)
# ══════════════════════════════════════════════════════

def robust_bandit_update(bandit, arm_idx: int, pnl: float,
                          recent_pnls: list = None) -> str:
    """Обновление bandit с защитой от outlier-сделок.

    Возвращает: 'applied' | 'outlier' | 'damped'
    """
    if recent_pnls and len(recent_pnls) >= 20:
        mean_pnl = sum(recent_pnls) / len(recent_pnls)
        variance = sum((p - mean_pnl) ** 2 for p in recent_pnls) / len(recent_pnls)
        std_pnl = variance ** 0.5

        # Outlier: >3σ
        if std_pnl > 0 and abs(pnl - mean_pnl) > 3 * std_pnl:
            return 'outlier'

        # Damped: >2σ → половинный вес
        if std_pnl > 0 and abs(pnl - mean_pnl) > 2 * std_pnl:
            weight = 0.5
        else:
            weight = 1.0
    else:
        weight = 1.0

    # Обновить posterior
    if hasattr(bandit, 'arms'):
        if pnl > 0:
            bandit.arms[arm_idx]["alpha"] += weight
        else:
            bandit.arms[arm_idx]["beta"] += weight
        bandit.arms[arm_idx]["trades"] = bandit.arms[arm_idx].get("trades", 0) + 1
        if pnl > 0:
            bandit.arms[arm_idx]["wins"] = bandit.arms[arm_idx].get("wins", 0) + 1

    return 'damped' if weight < 1.0 else 'applied'


# ══════════════════════════════════════════════════════
# V10: EXPONENTIAL DECAY (old trades → lower weight)
# ══════════════════════════════════════════════════════

def weighted_wr(trades: list, decay_rate: float = 0.01) -> float:
    """WR с экспоненциальным затуханием: старые сделки весят меньше."""
    if not trades:
        return 0.0
    now = datetime.now()
    weights, wins = [], []
    for t in trades:
        closed_ts = t.get("closed_at") if isinstance(t, dict) else getattr(t, "closed_at", None)
        if closed_ts is None:
            closed_ts = t.get("entry_at") if isinstance(t, dict) else getattr(t, "entry_at", None)
        if closed_ts:
            try:
                age_days = (now - datetime.fromtimestamp(float(closed_ts))).total_seconds() / 86400
                w = max(0.05, __import__('math').exp(-decay_rate * age_days))
            except (ValueError, OSError):
                w = 1.0
        else:
            w = 1.0
        weights.append(w)
        wins.append(1.0 if (t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) > 0 else 0.0)
    total_w = sum(weights)
    return sum(w * win for w, win in zip(weights, wins)) / total_w if total_w > 0 else 0.0


def weighted_composite_score(trades: list, regime: str = None, decay_rate: float = 0.01) -> dict:
    """Composite score с exponential decay."""
    if not trades:
        return {"score": 0.5, "wr": 0, "pf": 0, "sharpe": 0, "max_dd": 0, "avg_hold": 0}
    now = datetime.now()
    weights, pnls, holds = [], [], []
    for t in trades:
        closed_ts = (t.get("closed_at") if isinstance(t, dict) else getattr(t, "closed_at", None)) or \
                     (t.get("entry_at") if isinstance(t, dict) else getattr(t, "entry_at", None))
        w = 1.0
        if closed_ts:
            try:
                age_days = (now - datetime.fromtimestamp(float(closed_ts))).total_seconds() / 86400
                w = max(0.05, __import__('math').exp(-decay_rate * age_days))
            except (ValueError, OSError):
                pass
        weights.append(w)
        pnls.append(t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0))
        holds.append(t.get("hold_hours", 0) if isinstance(t, dict) else getattr(t, "hold_hours", 0))
    total_w = sum(weights)
    wr = sum(w * (1 if p > 0 else 0) for w, p in zip(weights, pnls)) / total_w
    gross_profit = sum(w * p for w, p in zip(weights, pnls) if p > 0)
    gross_loss = sum(w * abs(p) for w, p in zip(weights, pnls) if p < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else 2.0
    pf_norm = min(pf / 3.0, 1.0)
    n = len(trades)
    mean_pnl = sum(weights[i] * pnls[i] for i in range(n)) / total_w
    variance = sum(weights[i] * (pnls[i] - mean_pnl) ** 2 for i in range(n)) / total_w
    sharpe = mean_pnl / (variance ** 0.5) if variance > 0 else 0
    sharpe_norm = min(max(sharpe + 1, 0) / 2.5, 1.0)
    cumulative, peak, max_dd = 0, 0, 0
    for p in pnls:
        cumulative += p; peak = max(peak, cumulative); max_dd = max(max_dd, peak - cumulative)
    dd_norm = max(0, 1.0 - max_dd / 100.0)
    avg_hold = sum(w * h for w, h in zip(weights, holds)) / total_w
    hold_norm = max(0, 1.0 - avg_hold / 48.0)
    score = 0.30 * wr + 0.25 * pf_norm + 0.20 * sharpe_norm + 0.15 * dd_norm + 0.10 * hold_norm
    return {"score": round(score, 3), "wr": round(wr, 3), "pf": round(pf, 2),
            "sharpe": round(sharpe, 3), "max_dd": round(max_dd, 2),
            "avg_hold": round(avg_hold, 1), "decay_rate": decay_rate}


# ══════════════════════════════════════════════════════
# V10: UNCERTAINTY-AWARE THOMPSON
# ══════════════════════════════════════════════════════

def select_arm_with_uncertainty(bandit, threshold: float = 0.10) -> tuple:
    import random as _random
    import math
    means = [a["alpha"] / (a["alpha"] + a["beta"]) for a in bandit.arms]
    n = len(means)
    uncertainty = math.sqrt(sum((m - sum(means)/n)**2 for m in means) / n)
    if uncertainty > threshold:
        idx = _random.randint(0, n - 1)
        return bandit.arms[idx]["params"], idx, "EXPLORE"
    samples = [_random.betavariate(a["alpha"], a["beta"]) for a in bandit.arms]
    idx = max(range(n), key=lambda i: samples[i])
    return bandit.arms[idx]["params"], idx, "EXPLOIT"


# ══════════════════════════════════════════════════════
# V10: COORDINATED ENSEMBLE
# ══════════════════════════════════════════════════════

class CoordinatedEnsemble:
    """Ансамбль с координацией при смене режима."""

    def __init__(self, ensemble: ParameterEnsemble = None):
        self.ensemble = ensemble or ParameterEnsemble()
        self.last_regime = None
        self.transition_trades = 0
        self.transition_limit = 10

    def on_regime_change(self, old_regime: str, new_regime: str):
        self.last_regime = old_regime
        self.transition_trades = 0

    def select_params(self, current_regime: str) -> tuple:
        if current_regime not in self.ensemble.bandits:
            current_regime = "RANGING"
        if self.transition_trades < self.transition_limit and self.last_regime:
            old_b = self.ensemble.bandits.get(self.last_regime)
            new_b = self.ensemble.bandits.get(current_regime)
            if old_b and new_b:
                op = old_b.get_best_arm()["params"]
                np_ = new_b.get_best_arm()["params"]
                w = 0.3 * (1 - self.transition_trades / self.transition_limit)
                blended = {k: round(op.get(k, 0) * w + np_.get(k, 0) * (1 - w), 1) for k in op}
                return blended, -1
        params, idx = self.ensemble.select_params(current_regime)
        return params, idx

    def update(self, regime: str, arm_idx: int, win: bool):
        if regime in self.ensemble.bandits and arm_idx >= 0:
            self.ensemble.bandits[regime].update(arm_idx, win)
            self.ensemble.bandits[regime].prune_and_regenerate()
        self.transition_trades += 1
        if self.transition_trades >= self.transition_limit:
            self.last_regime = None


# ══════════════════════════════════════════════════════
# V10: CAUSAL INFERENCE
# ══════════════════════════════════════════════════════

def causal_analysis(trades: list, btc_return_24h: float = None) -> str:
    """Почему WR упал? MARKET_CONDITIONS или PARAMETERS?"""
    if len(trades) < 50:
        return "UNCLEAR"
    recent = trades[-20:]
    baseline = trades[-100:-20]
    recent_wr = sum(1 for t in recent if (t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) > 0) / len(recent)
    baseline_wr = sum(1 for t in baseline if (t.get("pnl", 0) if isinstance(t, dict) else getattr(t, "pnl", 0)) > 0) / len(baseline)
    wr_drop = baseline_wr - recent_wr
    if wr_drop < 0.05:
        return "STABLE"
    if btc_return_24h is None:
        try:
            import sqlite3 as _sql
            db = Path.home() / ".local" / "share" / "bybit-ws" / "state.db"
            conn = _sql.connect(str(db))
            r = conn.execute("SELECT pnl FROM trade_history WHERE symbol='BTCUSDT' AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 5").fetchall()
            conn.close()
            btc_return_24h = sum(float(x[0] or 0) for x in r) if r else 0
        except Exception:
            btc_return_24h = 0
    if btc_return_24h < -5 and wr_drop > 0.10:
        return "MARKET_CONDITIONS"
    elif btc_return_24h > -2 and wr_drop > 0.10:
        return "PARAMETERS"
    return "UNCLEAR"
