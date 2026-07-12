"""
Self-learning module v3 — canary mode + auto-rollback.

Canary-режим (Фаза 7.2):
  - Новые параметры применяются только к 10% входов
  - 48ч окно оценки
  - Если WR canary падает >10% от baseline → auto-rollback
  - Если WR canary >= baseline → promote to full
  - Все изменения логируются в self_learn.jsonl

State: ~/.local/share/bybit-ws/canary_state.json
"""
import json
import logging
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MAX_ADJUSTMENT = 0.20
LEARN_LOG = Path.home() / ".local" / "share" / "bybit-ws" / "self_learn.jsonl"
CANARY_STATE = Path.home() / ".local" / "share" / "bybit-ws" / "canary_state.json"
CANARY_ENTRIES = Path.home() / ".local" / "share" / "bybit-ws" / "canary_entries.jsonl"
_canary_lock = threading.Lock()   # защита read-modify-write canary_state.json
_entries_lock = threading.Lock()  # защита canary_entries.jsonl

# ── Canary constants ──
CANARY_ENTRY_PCT = 0.10       # 10% входов используют canary-параметры
CANARY_WINDOW_HOURS = 48      # окно оценки до promote/rollback
CANARY_WR_DROP_THRESHOLD = 0.10  # падение WR >10% → откат
CANARY_MATCH_WINDOW = 3600    # матчинг canary-входа с закрытием: ±1ч


def _load_canary_state() -> dict:
    """Загрузить состояние canary-режима."""
    if CANARY_STATE.exists():
        try:
            return json.loads(CANARY_STATE.read_text())
        except Exception:
            pass
    return {
        "active": False,
        "params": {},            # {param_name: new_value}
        "baseline": {},          # {param_name: old_value}
        "started_at": None,      # ISO timestamp
        "canary_trades": 0,
        "canary_wins": 0,
        "baseline_wr": 0.0,      # baseline WR на момент старта
        "promoted": False,
        "rolled_back": False,
        "history": [],           # [{ts, action, detail}]
    }


def _save_canary_state(state: dict):
    """Сохранить состояние canary-режима."""
    CANARY_STATE.parent.mkdir(parents=True, exist_ok=True)
    CANARY_STATE.write_text(json.dumps(state, indent=2))


def _log_adjustment(entry: dict):
    """Append adjustment to persistent log."""
    entry["ts"] = datetime.now().isoformat()
    try:
        LEARN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARN_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"self_learn log: {e}")


def is_canary_active() -> bool:
    """Активен ли canary-режим прямо сейчас."""
    state = _load_canary_state()
    if not state.get("active"):
        return False
    # Проверяем что canary не просрочен
    started = state.get("started_at")
    if started:
        try:
            started_ts = datetime.fromisoformat(started)
            hours_elapsed = (datetime.now() - started_ts).total_seconds() / 3600
            if hours_elapsed > CANARY_WINDOW_HOURS:
                # Авто-завершение canary (promote если данные есть)
                _finalize_canary(state)
                return False
        except Exception:
            pass
    return True


def should_use_canary() -> bool:
    """Должен ли этот вход использовать canary-параметры? (10% вероятность)"""
    if not is_canary_active():
        return False
    return random.random() < CANARY_ENTRY_PCT


def get_canary_param(param_name: str, baseline_value: Any,
                     symbol: str = None, side: str = None) -> Any:
    """Получить значение параметра: canary или baseline.

    Если возвращает canary-значение и передан symbol — маркирует вход
    в canary_entries.jsonl для последующего матчинга с закрытием.
    """
    if not should_use_canary():
        return baseline_value

    state = _load_canary_state()
    params = state.get("params", {})
    if param_name in params:
        canary_val = params[param_name]
        logger.debug(f"canary: using {param_name}={canary_val} (baseline={baseline_value})")
        # NOTE: mark_canary_entry() moved to auto_entry.py — только после реального входа
        return canary_val
    return baseline_value


def mark_canary_entry(symbol: str, side: str, entry_ts: float):
    """Записать canary-вход для будущего матчинга с закрытием."""
    entry = {
        "symbol": symbol,
        "side": side,
        "entry_ts": entry_ts,
        "marked_at": datetime.now().isoformat(),
    }
    with _entries_lock:
        try:
            CANARY_ENTRIES.parent.mkdir(parents=True, exist_ok=True)
            with open(CANARY_ENTRIES, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"canary mark entry: {e}")


def match_canary_entry(symbol: str, side: str, close_ts: float,
                       window: int = None) -> bool:
    """Проверить: был ли canary-вход для этого символа в окрестности close_ts.

    Возвращает True и удаляет совпавшую запись из файла.
    window — секунды до close_ts (по умолчанию CANARY_MATCH_WINDOW).
    """
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
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if (entry.get("symbol") == symbol
                    and entry.get("side") == side
                    and close_ts - entry.get("entry_ts", 0) <= window
                    and close_ts >= entry.get("entry_ts", 0)):
                matched = True
                # Не сохраняем — убираем совпавшую запись
            else:
                kept.append(line)

        # Периодически чистим старые (>48ч)
        now = time.time()
        kept = [l for l in kept if _entry_age(l) < CANARY_WINDOW_HOURS * 3600
                or _entry_age(l) < 0]  # keep broken lines

        try:
            CANARY_ENTRIES.write_text("\n".join(kept) + ("\n" if kept else ""))
        except Exception:
            pass

    return matched


def _entry_age(line: str) -> float:
    """Возраст записи canary_entries в секундах (>0 — старее, <0 — сломана)."""
    try:
        entry = json.loads(line)
        return time.time() - entry.get("entry_ts", 0)
    except Exception:
        return -1


def record_canary_result(win: bool):
    """Записать результат canary-сделки."""
    with _canary_lock:
        state = _load_canary_state()
        if not state.get("active"):
            return

        state["canary_trades"] = state.get("canary_trades", 0) + 1
        if win:
            state["canary_wins"] = state.get("canary_wins", 0) + 1

        state["history"].append({
            "ts": datetime.now().isoformat(),
            "action": "trade",
            "win": win,
            "canary_trades": state["canary_trades"],
            "canary_wins": state["canary_wins"],
        })

        # Проверяем не пора ли финализировать
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
    """Принять решение: promote или rollback."""
    if state.get("promoted") or state.get("rolled_back"):
        return

    canary_trades = state.get("canary_trades", 0)
    if canary_trades < 5:
        # Недостаточно данных — откатываем
        state["active"] = False
        state["rolled_back"] = True
        state["history"].append({
            "ts": datetime.now().isoformat(),
            "action": "rollback",
            "reason": f"insufficient data ({canary_trades} trades < 5)",
        })
        _save_canary_state(state)
        _log_canary_decision(state, "rollback", "insufficient data")
        return

    canary_wr = state["canary_wins"] / canary_trades if canary_trades > 0 else 0
    baseline_wr = state.get("baseline_wr", 0.5)
    wr_drop = baseline_wr - canary_wr

    if wr_drop > CANARY_WR_DROP_THRESHOLD:
        # Rollback — WR упал
        state["active"] = False
        state["rolled_back"] = True
        state["history"].append({
            "ts": datetime.now().isoformat(),
            "action": "rollback",
            "reason": f"WR drop: canary={canary_wr:.3f} vs baseline={baseline_wr:.3f} (drop={wr_drop:.3f})",
        })
        _save_canary_state(state)
        _log_canary_decision(state, "rollback",
                             f"WR drop {wr_drop:.1%}")
    else:
        # Promote — WR не хуже baseline
        state["active"] = False
        state["promoted"] = True
        state["history"].append({
            "ts": datetime.now().isoformat(),
            "action": "promote",
            "reason": f"WR ok: canary={canary_wr:.3f} vs baseline={baseline_wr:.3f}",
            "params": state.get("params", {}),
        })
        _save_canary_state(state)
        _log_canary_decision(state, "promote",
                             f"canary WR={canary_wr:.1%} >= baseline {baseline_wr:.1%}")


def _log_canary_decision(state: dict, decision: str, detail: str):
    """Логировать решение canary-режима."""
    canary_trades = state.get("canary_trades", 0)
    canary_wins = state.get("canary_wins", 0)
    canary_wr = canary_wins / canary_trades if canary_trades > 0 else 0

    _log_adjustment({
        "event": f"canary_{decision}",
        "detail": detail,
        "canary_trades": canary_trades,
        "canary_wr": round(canary_wr, 3),
        "baseline_wr": round(state.get("baseline_wr", 0), 3),
        "params": state.get("params", {}),
    })

    logger.info(
        f"🧪 Canary {decision}: {detail} "
        f"(trades={canary_trades}, canary_wr={canary_wr:.1%}, "
        f"baseline_wr={state['baseline_wr']:.1%})"
    )


async def apply_journal_insights(journal: dict, cfg) -> dict:
    """Apply self-learning adjustments from journal profile.

    Canary-режим (v3):
      - Не применяет параметры глобально
      - Запускает canary: 10% входов с новыми параметрами
      - Авто-rollback если WR падает >10%
      - Promote если WR не хуже baseline

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

    # Проверяем: не активен ли уже canary
    canary_state = _load_canary_state()
    if canary_state.get("active"):
        logger.info("Canary already active, skipping new adjustments")
        return applied

    # ── Adaptive min_score ──
    min_score = getattr(cfg.strategy, "min_score", 15)
    new_params = {}

    if win_rate < 0.40 and total_trades > 30:
        new_score = min(int(min_score * 1.3), 35)
        if new_score > min_score:
            new_params["min_score"] = new_score
            applied["min_score"] = {"from": min_score, "to": new_score,
                                    "reason": f"win_rate={win_rate:.2f} < 0.40"}
    elif win_rate < 0.45 and total_trades > 50:
        new_score = min(int(min_score * 1.15), 30)
        if new_score > min_score:
            new_params["min_score"] = new_score
            applied["min_score"] = {"from": min_score, "to": new_score,
                                    "reason": f"win_rate={win_rate:.2f} < 0.45"}

    # ── Adaptive TP/SL ratio ──
    sl_pct = getattr(cfg.strategy, "sl_pct", 5)
    if avg_hold_hours < 2 and total_trades > 30:
        new_sl = min(sl_pct * 1.2, 10)
        if new_sl > sl_pct:
            new_params["sl_pct"] = new_sl
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

    # ── Launch canary если есть новые параметры ──
    if new_params:
        # Собираем baseline-значения
        baseline_vals = {}
        for param_key, adj in applied.items():
            if isinstance(adj, dict) and "from" in adj:
                baseline_vals[param_key] = adj["from"]

        canary_state = {
            "active": True,
            "params": new_params,
            "baseline": baseline_vals,
            "started_at": datetime.now().isoformat(),
            "canary_trades": 0,
            "canary_wins": 0,
            "baseline_wr": win_rate,
            "promoted": False,
            "rolled_back": False,
            "history": [{
                "ts": datetime.now().isoformat(),
                "action": "start",
                "params": new_params,
                "baseline_wr": win_rate,
            }],
        }
        _save_canary_state(canary_state)

        log_base["canary"] = {
            "active": True,
            "entry_pct": CANARY_ENTRY_PCT,
            "window_hours": CANARY_WINDOW_HOURS,
            "params": new_params,
            "baseline_wr": round(win_rate, 3),
        }
        logger.info(
            f"🧪 Canary started: {len(new_params)} params, "
            f"{CANARY_ENTRY_PCT*100:.0f}% entries, "
            f"baseline WR={win_rate:.1%}"
        )
    else:
        log_base["canary"] = {"active": False, "reason": "no adjustments needed"}

    # ── Persistent log ──
    if applied:
        log_base["adjustments"] = applied

    _log_adjustment(log_base)

    return applied
