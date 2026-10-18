"""Тесты телеметрии trade_history: fees / leverage / external_id.

Задача C (rev.2): fees заполняется из openFee+closeFee, leverage и external_id —
новые колонки. pnl НЕ трогаем (он уже net из closedPnl).

Только временные БД (tempfile / tmp_path). Боевой state.db не читается и не пишется.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_trade_fees.py -q
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, '/home/openclaw')

from bybit_ws.state_db import StateDB  # noqa: E402


def _row(db, trade_id):
    return db.conn.execute(
        "SELECT fees, leverage, external_id FROM trade_history WHERE id=?",
        (trade_id,),
    ).fetchone()


def test_new_columns_stored():
    """add_trade(fees=..., leverage=..., external_id=...) сохраняет все три (read back)."""
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(os.path.join(d, 'state.db'))
        tid = db.add_trade(
            symbol='QUSDT', side='Sell', strategy='auto',
            entry_price=1.0, exit_price=1.1, size=10.0, pnl=1.0,
            fees=1.25, leverage=10, external_id='abc123',
        )
        fees, leverage, external_id = _row(db, tid)
        assert fees == 1.25
        assert leverage == 10
        assert external_id == 'abc123'
        db.close()


def test_backward_compat_defaults():
    """add_trade без новых параметров: fees=0, leverage IS NULL, external_id IS NULL."""
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(os.path.join(d, 'state.db'))
        tid = db.add_trade(
            symbol='BTCUSDT', side='Buy', strategy='auto',
            entry_price=1.0, exit_price=1.1, size=1.0, pnl=0.5,
        )
        fees, leverage, external_id = _row(db, tid)
        assert fees == 0
        assert leverage is None
        assert external_id is None
        db.close()


_OLD_SCHEMA = """
CREATE TABLE trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    fees REAL DEFAULT 0,
    entry_at INTEGER,
    closed_at INTEGER,
    bb_pct REAL,
    rsi REAL,
    entry_reason TEXT,
    dca_count INTEGER DEFAULT 0,
    partial_tp_count INTEGER DEFAULT 0,
    exit_reason TEXT,
    hold_hours REAL,
    manual INTEGER DEFAULT 0
);
"""


def test_migrate_adds_new_columns():
    """Старая схема (без leverage/external_id) → _migrate() добавляет обе колонки."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'state.db')
        raw = sqlite3.connect(path)
        raw.executescript(_OLD_SCHEMA)
        raw.commit()
        raw.close()

        # Инициализация StateDB на этой БД запускает _migrate().
        db = StateDB(path)
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(trade_history)")]
        assert 'leverage' in cols
        assert 'external_id' in cols
        db.close()
