# Property-тесты денежных путей (Hypothesis) — по мотивам
# claude.dev/blog/how-the-claude-code-team-thinks-about-verification (16.11.2026).
# Свойства, которые не могут нарушиться, на тысячах случайных входов:
#   1. safe_json_write — round-trip: записал X → прочитал X
#   2. /calc_qty — инварианты размера: qty ≥ min_qty, шаг лота, конечность
#   3. state_db._migrate — идемпотентность (дважды = один раз)
#   4. migration #20 — дубли external_id вычищаются, unique-индекс стоит
import io
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('BYBIT_LOG_SILENT', '1')

from file_utils import safe_json_write  # noqa: E402
from state_db import StateDB  # noqa: E402

rpc = pytest.importorskip('bybit_ws.rpc')

_FAST = settings(max_examples=200, deadline=None,
                 suppress_health_check=[HealthCheck.function_scoped_fixture])


# JSON-значения без NaN/inf (json.dump их сериализует, json.loads — обратно не то же)
_json_scalars = st.one_of(
    st.none(), st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e15, max_value=1e15),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=10), children, max_size=4)),
    max_leaves=10,
)


# ── 1. safe_json_write: round-trip ──────────────────────────────
@_FAST
@given(_json_values)
def test_safe_json_write_roundtrip(tmp_path, data):
    """Записал X → прочитал ровно X. Инцидент 28.10: open('w') давал читателю 0 байт."""
    path = tmp_path / 'state.json'
    safe_json_write(str(path), data)
    assert json.loads(path.read_text()) == data


# ── 2. /calc_qty: инварианты размера позиции ────────────────────
class _FakeHandler:
    """Носитель wfile/send_response без сокета (паттерн test_rpc_paused_and_auth)."""

    def __init__(self):
        self.status = None
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


@_FAST
@given(
    balance=st.floats(min_value=1.0, max_value=1e6),
    risk_pct=st.floats(min_value=0.1, max_value=100.0),
    leverage=st.integers(min_value=1, max_value=100),
    entry=st.floats(min_value=0.01, max_value=1e5),
    min_qty=st.sampled_from([0.001, 0.01, 0.1, 1.0]),
    qty_step=st.sampled_from([0.001, 0.01, 0.1, 1.0]),
)
def test_calc_qty_invariants(monkeypatch, balance, risk_pct, leverage,
                             entry, min_qty, qty_step):
    """qty конечен, ≥ min_qty, выровнен по шагу лота; notional ≈ риск×плечо."""
    def fake_api(method, path, body=None):
        if 'wallet-balance' in path:
            return {'result': {'list': [{'totalWalletBalance': str(balance)}]}}
        return {'result': {'list': [{'lotSizeFilter': {
            'minOrderQty': str(min_qty), 'qtyStep': str(qty_step)}}]}}

    monkeypatch.setattr(rpc, '_bybit_api', fake_api)
    h = _FakeHandler()
    rpc.RPCHandler._handle_calc_qty(h, {
        'symbol': 'XRPUSDT', 'risk_pct': risk_pct,
        'leverage': leverage, 'entry': entry})

    resp = json.loads(h.wfile.getvalue())
    assert 'error' not in resp, resp
    qty = resp['qty']
    assert qty == qty and qty != float('inf')          # конечен
    assert qty >= min_qty - 1e-9                        # не меньше мин. лота
    position_value = balance * (risk_pct / 100) * leverage
    if qty > min_qty:                                   # не прижат min-лотом
        assert abs(qty * entry - position_value) <= qty_step * entry + 1e-6


# ── 3. _migrate: идемпотентность ────────────────────────────────
_LEGACY = """
CREATE TABLE trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL, exit_price REAL, size REAL, pnl REAL,
    entry_at INTEGER, closed_at INTEGER
);
"""
_MIGRATED_COLS = {'manual', 'leverage', 'external_id', 'score', 'regime',
                  'funding', 'confluence'}


@_FAST
@given(n_runs=st.integers(min_value=2, max_value=5))
def test_migrate_idempotent(tmp_path, n_runs):
    """Прогнал миграцию n раз = прогнал один раз (у них в списке свойств: idempotency)."""
    # tmp_path общий на все examples гипотезы — БД уникальным именем на каждый example
    path = str(tmp_path / f'state_{uuid.uuid4().hex}.db')
    con = sqlite3.connect(path)
    con.executescript(_LEGACY)
    con.commit()
    con.close()

    first_cols = None
    for _ in range(n_runs):
        db = StateDB(path)
        cols = {d[1] for d in db.conn.execute('PRAGMA table_info(trade_history)')}
        db.conn.close()
        db._conn = None
        if first_cols is None:
            first_cols = cols
        assert cols == first_cols
    assert first_cols is not None
    assert _MIGRATED_COLS <= first_cols


# ── 4. migration #20: дубли external_id не переживают миграцию ──
@_FAST
@given(dup_ids=st.lists(st.one_of(st.none(), st.integers(min_value=1, max_value=5)),
                        min_size=1, max_size=20))
def test_migrate_dedupes_external_ids(tmp_path, dup_ids):
    """Случайные дубли external_id → после миграции дублей нет, unique-индекс стоит."""
    path = str(tmp_path / f'state_{uuid.uuid4().hex}.db')
    con = sqlite3.connect(path)
    con.executescript(_LEGACY)
    con.execute('ALTER TABLE trade_history ADD COLUMN external_id TEXT')
    for eid in dup_ids:
        con.execute(
            'INSERT INTO trade_history (symbol, side, external_id) VALUES (?,?,?)',
            ('XRPUSDT', 'Buy', str(eid) if eid is not None else None))
    con.commit()
    con.close()

    db = StateDB(path)
    rows = db.conn.execute(
        'SELECT external_id, COUNT(*) c FROM trade_history '
        'WHERE external_id IS NOT NULL GROUP BY external_id HAVING c > 1').fetchall()
    idx = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_trade_history_external_id'").fetchall()
    db.conn.close()
    db._conn = None

    assert rows == [], f'дубли external_id пережили миграцию: {rows}'
    assert idx, 'unique-индекс external_id не создан'
