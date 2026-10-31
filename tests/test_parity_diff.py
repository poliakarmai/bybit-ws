"""Тесты для parity-скрипта scripts_parity.py (live vs backtest)."""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts_parity as sp  # type: ignore[import-not-found]  # noqa: E402


# ── load_live_entries ──────────────────────────────────────────────

def test_load_live_entries_missing_db(tmp_path: Path):
    """БД не существует — пустой список."""
    fake_db = tmp_path / "nonexistent.db"
    with patch.object(sp, "STATE_DB", fake_db):
        result = sp.load_live_entries(7)
        assert result == []


def test_load_live_entries_with_data(tmp_path: Path):
    """БД с данными — записи корректно выгружены."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
            entry_price REAL, opened_at INTEGER, strategy TEXT
        )
    """)
    # Запись в пределах окна
    conn.execute("""
        INSERT INTO trade_history (symbol, side, entry_price, opened_at, strategy)
        VALUES ('BTCUSDT', 'Buy', 60000.0, 9999999999, 'bb_grid')
    """)
    # Запись за пределами окна
    conn.execute("""
        INSERT INTO trade_history (symbol, side, entry_price, opened_at, strategy)
        VALUES ('ETHUSDT', 'Sell', 3000.0, 1, 'bb_grid')
    """)
    conn.commit()
    conn.close()

    with patch.object(sp, "STATE_DB", db):
        entries = sp.load_live_entries(365 * 100)
        assert len(entries) == 2
        assert entries[0]["symbol"] == "BTCUSDT"


# ── compare_entries ──────────────────────────────────────────────────

def _entry(symbol="BTCUSDT", side="Buy", ts=1_700_000_000, strategy="bb_grid"):
    return {"symbol": symbol, "side": side, "opened_at": ts,
            "entry_price": 60000.0, "strategy": strategy}


def test_compare_exact_match():
    """Одинаковые записи — 100% совпадений."""
    live = [_entry(ts=1000)]
    bt = [_entry(ts=1000)]
    result = sp.compare_entries(live, bt, tolerance_minutes=1)
    assert result["summary"]["matched_count"] == 1
    assert result["summary"]["only_live_count"] == 0
    assert result["summary"]["only_backtest_count"] == 0


def test_compare_within_tolerance():
    """Записи в пределах допуска — совпадение."""
    live = [_entry(ts=1000)]
    bt = [_entry(ts=1300)]  # 5 min diff → within 15 min tolerance
    result = sp.compare_entries(live, bt, tolerance_minutes=15)
    assert result["summary"]["matched_count"] == 1


def test_compare_outside_tolerance():
    """Записи вне допуска — не совпадают."""
    live = [_entry(ts=1000)]
    bt = [_entry(ts=1000 + 16 * 60)]  # 16 min > 15 min tolerance
    result = sp.compare_entries(live, bt, tolerance_minutes=15)
    assert result["summary"]["matched_count"] == 0
    assert result["summary"]["only_live_count"] == 1
    assert result["summary"]["only_backtest_count"] == 1


def test_compare_different_symbol():
    """Разные символы — не совпадают."""
    live = [_entry(symbol="BTCUSDT", ts=1000)]
    bt = [_entry(symbol="ETHUSDT", ts=1000)]
    result = sp.compare_entries(live, bt)
    assert result["summary"]["matched_count"] == 0


def test_compare_different_side():
    """Разные стороны — не совпадают."""
    live = [_entry(side="Buy", ts=1000)]
    bt = [_entry(side="Sell", ts=1000)]
    result = sp.compare_entries(live, bt)
    assert result["summary"]["matched_count"] == 0


def test_compare_empty_inputs():
    """Пустые входы — ноль совпадений."""
    result = sp.compare_entries([], [])
    assert result["summary"]["matched_count"] == 0
    assert result["summary"]["match_rate"] == 0.0


def test_compare_match_rate():
    """Проверка match_rate при частичном совпадении."""
    live = [_entry(ts=1000), _entry(symbol="SOLUSDT", ts=2000)]
    bt = [_entry(ts=1000)]  # только первая совпадает
    result = sp.compare_entries(live, bt)
    assert result["summary"]["matched_count"] == 1
    assert result["summary"]["only_live_count"] == 1
    assert result["summary"]["only_backtest_count"] == 0
    assert result["summary"]["match_rate"] == 50.0  # 1/2


def test_compare_no_duplicate_matching():
    """Один backtest-вход не матчится дважды."""
    live = [_entry(ts=1000), _entry(ts=1000)]  # два одинаковых live
    bt = [_entry(ts=1000)]                      # один backtest
    result = sp.compare_entries(live, bt)
    assert result["summary"]["matched_count"] == 1
    assert result["summary"]["only_live_count"] == 1
