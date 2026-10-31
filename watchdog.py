"""Сторож молчаливой блокировки входов (инцидент 31.10.2026).

31.10 бот 1.5 суток повторял `💸 Депозит $-5.6 < $30 — входы заблокированы`: сигналы
были, входов не было, и об этом никто не узнал — причину нашли случайно при обзоре лога.
Проверка простая: за окно **0 авто-входов** (SSOT `trade_history.entry_at`) и
**≥N блокирующих строк** в events.log → алерт с топ-причинами.

Запуск:
    python3 -m bybit_ws.watchdog                 # окно 6ч, порог 20 блоков
    python3 -m bybit_ws.watchdog --hours 12 --dry-run
    python3 -m bybit_ws.watchdog --json

Крон: ежечасно (Hermes cron, no_agent). Дедупликация алертов — в
`alerts.send_telegram_alert` (нормализованный хеш сообщения).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
STATE_DB_PATH = DATA_DIR / "state.db"
EVENTS_LOG_PATH = DATA_DIR / "events.log"

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*")
# Блокировка входа: явный текст или маркер отказа. Обычный «⚠️» и «🚫 … пропускаю»
# (dry-spell throttle, BB% < порога) — не блокировки, а обычная фильтрация
# кандидатов: их в логе тысячи каждый час, и алерт по ним звонил бы всегда.
BLOCK_RE = re.compile(r"вход(ы)? заблокирован|⛔|🛑|💸|STOP:.*маржи")
SKIP_MARKERS = ("пропускаю", "main loop stopped")  # не блокировки входа
SYMBOL_RE = re.compile(r"\b[A-Z0-9]{2,12}(?:USDT|PERP)\b")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

TAIL_BYTES = 2_000_000  # ~последние сутки при текущем потоке; файл 31 МБ целиком не читаем


def tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    """Последние строки файла без чтения целиком (первая строка может быть обрезана)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        blob = f.read().decode("utf-8", errors="ignore")
    lines = blob.splitlines()
    return lines[1:] if size > max_bytes else lines


def reason_key(msg: str) -> str:
    """Причина без цифр и тикеров — стабильный ключ для группировки и дедупликации."""
    m = SYMBOL_RE.sub("SYM", msg)
    m = NUM_RE.sub("N", m)
    return " ".join(m.split())[:120]


def scan_log(lines: list[str], since: datetime) -> tuple[Counter, datetime | None]:
    """Группировка блокирующих строк окна по причине + время последней."""
    reasons: Counter = Counter()
    last: datetime | None = None
    for line in lines:
        mt = TS_RE.match(line)
        if not mt:
            continue
        try:
            ts = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < since:
            continue
        msg = line[mt.end():].strip()
        if any(s in msg for s in SKIP_MARKERS) or not BLOCK_RE.search(msg):
            continue
        reasons[reason_key(msg)] += 1
        if last is None or ts > last:
            last = ts
    return reasons, last


def count_entries(since_ts: float, db_path: Path | None = None) -> int:
    """Авто-входы за окно: строки trade_history с entry_at >= since (read-only)."""
    path = Path(db_path or STATE_DB_PATH)
    if not path.exists():
        return 0
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT count(*) FROM trade_history WHERE entry_at >= ?", (int(since_ts),)
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def should_alert(entries: int, n_blocks: int, min_blocks: int) -> bool:
    """Алерт: входов нет совсем, а блокировок много — значит сигналы есть и режутся."""
    return entries == 0 and n_blocks >= min_blocks


def build_report(hours: int, min_blocks: int, db_path: Path | None = None,
                 log_path: Path | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    since = now - timedelta(hours=hours)
    lines = tail_lines(Path(log_path or EVENTS_LOG_PATH))
    reasons, last_block = scan_log(lines, since)
    entries = count_entries(since.timestamp(), db_path)
    n_blocks = sum(reasons.values())
    top = [{"reason": r, "count": c} for r, c in reasons.most_common(3)]
    return {
        "window_hours": hours,
        "since": since.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
        "blocks": n_blocks,
        "min_blocks": min_blocks,
        "top_reasons": top,
        "last_block_at": last_block.strftime("%Y-%m-%d %H:%M:%S") if last_block else None,
        "alert": should_alert(entries, n_blocks, min_blocks),
    }


def format_alert(rep: dict) -> str:
    top = "; ".join(f"{t['reason']} ×{t['count']}" for t in rep["top_reasons"]) or "нет"
    return (f"🛑 bybit-ws: {rep['window_hours']}ч без авто-входов, "
            f"блокировок {rep['blocks']}. Топ: {top}. Проверь /balance и events.log")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Сторож молчаливой блокировки входов bybit-ws")
    p.add_argument("--hours", type=int, default=6, help="окно проверки, часов")
    p.add_argument("--min-blocks", type=int, default=20, help="порог блокировок для алерта")
    p.add_argument("--dry-run", action="store_true", help="только вывод, без алерта")
    p.add_argument("--json", action="store_true", help="JSON-вывод")
    a = p.parse_args(argv)

    rep = build_report(a.hours, a.min_blocks)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"окно {rep['window_hours']}ч с {rep['since']}: входов {rep['entries']}, "
              f"блокировок {rep['blocks']} (порог {rep['min_blocks']})"
              + (f", последняя {rep['last_block_at']}" if rep["last_block_at"] else ""))
        for t in rep["top_reasons"]:
            print(f"  ×{t['count']:5d}  {t['reason']}")
        print("ALERT" if rep["alert"] else "ok")

    if rep["alert"] and not a.dry_run:
        try:
            from .alerts import send_telegram_alert
            send_telegram_alert(format_alert(rep), level="STOP")
        except Exception as e:  # алерт не должен падать из-за канала
            print(f"алерт не отправлен: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
