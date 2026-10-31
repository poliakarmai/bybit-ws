"""Тесты сторожа молчаливой блокировки входов (watchdog.py).

Сценарий-эталон: инцидент 31.10.2026 — 305 строк `💸 Депозит ... входы заблокированы`
и ноль авто-входов. Сторож обязан такое поймать и не звонить на обычную фильтрацию
кандидатов (`🚫 ... пропускаю`), которой в логе тысячи.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watchdog import build_report, reason_key, should_alert, tail_lines  # noqa: E402

NOW = datetime(2026, 10, 31, 9, 0, 0)
DEPOSIT = "💸 Депозит $-5.6 < $30.0 — входы заблокированы"
SKIP = "🚫 auto_short: SOLUSDT dry-spell throttle (12 холостых) — пропускаю"
POST_ONLY = "⛔ POST-ONLY LONG BTCUSDT: Order would immediately match — вход не открыт"


def _log(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "events.log"
    p.write_text("\n".join(lines) + "\n")
    return p


def _db(tmp_path: Path, entries_at: list[datetime]) -> Path:
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, entry_at INTEGER, manual INTEGER)")
    for ts in entries_at:
        con.execute("INSERT INTO trade_history (entry_at, manual) VALUES (?, 0)", (int(ts.timestamp()),))
    con.commit()
    con.close()
    return p


def _line(ts: datetime, msg: str) -> str:
    return f"[{ts.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"


def test_incident_31_10_alerts(tmp_path):
    """Депозитный гейт молотит, входов ноль → алерт."""
    lines = [_line(NOW - timedelta(minutes=m), DEPOSIT) for m in range(1, 40)]
    rep = build_report(6, 20, db_path=_db(tmp_path, []), log_path=_log(tmp_path, lines), now=NOW)
    assert rep["entries"] == 0 and rep["blocks"] == 39
    assert rep["alert"] is True
    assert "Депозит" in rep["top_reasons"][0]["reason"]


def test_entries_present_no_alert(tmp_path):
    """Один вход за окно — торговля живая, паники нет."""
    lines = [_line(NOW - timedelta(minutes=m), DEPOSIT) for m in range(1, 40)]
    rep = build_report(6, 20, db_path=_db(tmp_path, [NOW - timedelta(hours=2)]),
                       log_path=_log(tmp_path, lines), now=NOW)
    assert rep["entries"] == 1
    assert rep["alert"] is False


def test_candidate_filtering_is_not_a_block(tmp_path):
    """`пропускаю` — рабочая фильтрация, а не блокировка: 500 строк → тишина."""
    lines = [_line(NOW - timedelta(minutes=m), SKIP) for m in range(1, 500)]
    rep = build_report(6, 20, db_path=_db(tmp_path, []), log_path=_log(tmp_path, lines), now=NOW)
    assert rep["blocks"] == 0
    assert rep["alert"] is False


def test_below_threshold_and_old_lines(tmp_path):
    """Мало блокировок — не алерт; строки вне окна не считаются."""
    lines = [_line(NOW - timedelta(minutes=m), POST_ONLY) for m in range(1, 5)]
    lines += [_line(NOW - timedelta(hours=10), DEPOSIT)] * 50
    rep = build_report(6, 20, db_path=_db(tmp_path, []), log_path=_log(tmp_path, lines), now=NOW)
    assert rep["blocks"] == 4
    assert rep["alert"] is False


def test_reason_key_is_stable():
    a = reason_key("💸 Депозит $-5.6 < $30.0 — входы заблокированы")
    b = reason_key("💸 Депозит $-12.34 < $30.0 — входы заблокированы")
    assert a == b
    assert "SYM" in reason_key("⛔ POST-ONLY LONG SOLUSDT: отказ")


def test_tail_lines_limits_read(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n")
    got = tail_lines(p, max_bytes=200)
    assert len(got) < 1000 and got[-1] == "line 999"


def test_should_alert_rules():
    assert should_alert(0, 20, 20) is True
    assert should_alert(0, 19, 20) is False
    assert should_alert(1, 999, 20) is False
