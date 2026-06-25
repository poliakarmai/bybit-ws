"""
Confluence Paper Tracker — Фаза 4.3.5

Отслеживает эффективность MTF-конфлюенса через paper-позиции.
Для каждого сигнала с известным конфлюенсом (2/3 или 3/3) открывает
виртуальную позицию и позже сравнивает winrate по уровням конклюенса.

Использование:
    from .confluence_paper import track_signal, get_confluence_stats
    track_signal(symbol, direction, entry, mark, confluence)
    stats = get_confluence_stats()
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
TRACKER_FILE = DATA_DIR / "confluence_tracker.json"


def _load() -> dict:
    """Загрузить трекер."""
    if TRACKER_FILE.exists():
        try:
            with open(TRACKER_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"signals": [], "results": {}}


def _save(data: dict):
    """Сохранить трекер."""
    TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKER_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def track_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    mark_price: float,
    confluence: int,
    score: Optional[int] = None,
):
    """Записать сигнал с известным конфлюенсом.

    Вызывается при обнаружении сигнала (до размещения ордера).
    Позже check_outcomes() обновит результат.
    """
    data = _load()
    data["signals"].append(
        {
            "symbol": symbol,
            "direction": direction,
            "entry": entry_price,
            "mark": mark_price,
            "confluence": confluence,
            "score": score,
            "ts": int(time.time()),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "status": "open",  # open → tp / sl / closed
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "exit_price": None,
        }
    )
    # Limit to last 500 signals
    if len(data["signals"]) > 500:
        data["signals"] = data["signals"][-500:]
    _save(data)


def check_outcomes(positions: dict):
    """Обновить статус открытых paper-сигналов на основе реальных позиций.

    Вызывается в главном цикле. Если символ исчез из positions — считаем закрытым.
    """
    data = _load()
    active_symbols = set(positions.keys())
    changed = False

    for sig in data["signals"]:
        if sig["status"] != "open":
            continue

        sym = sig["symbol"]
        direction = sig["direction"]

        if sym in active_symbols:
            # Позиция всё ещё открыта — обновляем PnL
            pos = positions[sym]
            side = pos.get("side", "")
            if (direction == "LONG" and side == "Buy") or (
                direction == "SHORT" and side == "Sell"
            ):
                mark = float(pos.get("markPrice", 0) or pos.get("mark", 0))
                size = float(pos.get("size", 0))
                entry = sig["entry"]
                if mark > 0 and entry > 0 and size > 0:
                    if direction == "LONG":
                        pnl = (mark - entry) * size
                        pnl_pct = (mark - entry) / entry * 100
                    else:
                        pnl = (entry - mark) * size
                        pnl_pct = (entry - mark) / entry * 100
                    sig["pnl"] = round(pnl, 2)
                    sig["pnl_pct"] = round(pnl_pct, 2)
                    sig["mark"] = mark
                    changed = True
        else:
            # Позиция закрылась — помечаем
            sig["status"] = "closed"
            changed = True

    if changed:
        _save(data)


def record_close(symbol: str, direction: str, exit_price: float, pnl: float):
    """Записать закрытие paper-позиции (вызывается из main.py при SL/TP)."""
    data = _load()
    for sig in reversed(data["signals"]):
        if (
            sig["status"] == "open"
            and sig["symbol"] == symbol
            and sig["direction"] == direction
        ):
            sig["status"] = "sl" if pnl < 0 else "tp"
            sig["exit_price"] = exit_price
            sig["pnl"] = round(pnl, 2)
            if sig["entry"] > 0:
                sig["pnl_pct"] = round(
                    pnl / (sig["entry"] * (sig.get("size", 1) or 1)) * 100, 2
                )
            _save(data)
            break


def get_confluence_stats(days: int = 30) -> dict:
    """Получить статистику winrate по уровням конфлюенса за последние N дней.

    Returns:
        {
            'total_signals': N,
            'by_confluence': {
                2: {'total': N, 'wins': N, 'losses': N, 'winrate': %, 'avg_pnl': $},
                3: {...}
            },
            'overall': {'winrate': %, 'avg_pnl': $}
        }
    """
    data = _load()
    cutoff = int(time.time()) - days * 86400

    closed = [
        s
        for s in data["signals"]
        if s["ts"] >= cutoff and s["status"] in ("tp", "sl", "closed")
    ]

    stats: Dict[int, dict] = {}
    for s in closed:
        c = s["confluence"]
        if c not in stats:
            stats[c] = {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
        stats[c]["total"] += 1
        stats[c]["total_pnl"] += s["pnl"]
        if s["pnl"] > 0:
            stats[c]["wins"] += 1
        else:
            stats[c]["losses"] += 1

    result = {"total_signals": len(closed), "by_confluence": {}, "overall": {}}

    total_wins = 0
    total_pnl = 0.0
    for c in sorted(stats.keys()):
        s = stats[c]
        winrate = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0
        avg_pnl = round(s["total_pnl"] / s["total"], 2) if s["total"] > 0 else 0
        result["by_confluence"][str(c)] = {
            "total": s["total"],
            "wins": s["wins"],
            "losses": s["losses"],
            "winrate": winrate,
            "avg_pnl": avg_pnl,
        }
        total_wins += s["wins"]
        total_pnl += s["total_pnl"]

    total_closed = len(closed)
    result["overall"] = {
        "winrate": round(total_wins / total_closed * 100, 1) if total_closed > 0 else 0,
        "avg_pnl": round(total_pnl / total_closed, 2) if total_closed > 0 else 0,
    }

    return result


def format_stats(stats: dict) -> str:
    """Форматировать статистику для вывода."""
    if stats["total_signals"] == 0:
        return "📊 Конфлюенс-статистика: нет закрытых сигналов за период"

    lines = [
        f"📊 MTF Конфлюенс-статистика ({stats['total_signals']} сделок):",
        "",
        "| Конфлюенс | Сделок | Winrate | Avg PnL |",
        "|-----------|--------|---------|---------|",
    ]

    for c, s in stats["by_confluence"].items():
        emoji = "🔥" if c == "3" else "✅"
        lines.append(
            f"| {emoji} {c}/3 | {s['total']} | {s['winrate']}% | ${s['avg_pnl']} |"
        )

    lines.append("")
    lines.append(
        f"**Всего:** winrate {stats['overall']['winrate']}%, "
        f"средний PnL ${stats['overall']['avg_pnl']}"
    )

    return "\n".join(lines)
