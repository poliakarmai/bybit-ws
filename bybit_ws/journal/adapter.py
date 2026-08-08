"""Адаптеры для загрузки торговой истории bybit-ws."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analyzer import Trade, analyze

logger = logging.getLogger(__name__)

DEFAULT_DB = Path("~/.local/share/bybit-ws/state.db").expanduser()


def load_from_sqlite(db_path: str | Path = DEFAULT_DB) -> dict[str, Any]:
    """Загружает историю из SQLite state.db и прогоняет анализ.

    Таблица trade_history:
        id, symbol, side, strategy, entry_price, exit_price,
        size, pnl, fees, entry_at, closed_at
    """
    db = Path(db_path).expanduser()
    if not db.exists():
        return {"error": f"DB not found: {db}"}

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_history WHERE closed_at IS NOT NULL AND strategy != 'imported' ORDER BY entry_at"  # Фаза 9: exclude imported
    ).fetchall()
    conn.close()

    if not rows:
        return {"error": "Нет закрытых сделок в trade_history"}

    trades = []
    skipped = 0
    for r in rows:
        try:
            side = "buy" if r["side"] == "Buy" else "sell"
            trades.append(Trade(
                symbol=r["symbol"],
                side=side,
                quantity=float(r["size"]),
                price=float(r["entry_price"]),
                fee=float(r["fees"] or 0),
                timestamp=float(r["entry_at"]),
                market="crypto",
                order_id=str(r["id"]),
            ))
            # Добавляем виртуальную сделку-закрытие для FIFO-матчинга
            trades.append(Trade(
                symbol=r["symbol"],
                side="sell" if side == "buy" else "buy",
                quantity=float(r["size"]),
                price=float(r["exit_price"]),
                fee=float(r["fees"] or 0),
                timestamp=float(r["closed_at"]),
                market="crypto",
                order_id=f"{r['id']}_close",
            ))
        except (TypeError, ValueError) as e:
            skipped += 1
            logger.warning("Skipping row %s: %s", r["id"], e)

    if not trades:
        return {"error": f"Не удалось загрузить сделки (пропущено: {skipped})"}

    result = analyze(trades)
    result["source"] = str(db)
    result["total_db_rows"] = len(rows)
    result["skipped"] = skipped
    return result


def load_from_list(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Загружает историю из списка словарей (для RPC / ручного ввода).

    Каждая запись: {symbol, side, entry_price, exit_price, size, entry_at, closed_at, [fees]}
    side: "Buy" или "Sell"
    """
    trades = []
    for r in records:
        side = "buy" if r.get("side", "").upper() == "BUY" else "sell"
        try:
            trades.append(Trade(
                symbol=r["symbol"],
                side=side,
                quantity=float(r["size"]),
                price=float(r["entry_price"]),
                fee=float(r.get("fees", 0)),
                timestamp=float(r["entry_at"]),
            ))
            trades.append(Trade(
                symbol=r["symbol"],
                side="sell" if side == "buy" else "buy",
                quantity=float(r["size"]),
                price=float(r["exit_price"]),
                fee=float(r.get("fees", 0)),
                timestamp=float(r["closed_at"]),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Skipping record: %s", e)
            continue

    if not trades:
        return {"error": "Нет валидных записей"}

    result = analyze(trades)
    result["source"] = "list"
    return result
