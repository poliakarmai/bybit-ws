"""Тест дедупа funding_log + строки funding в lp_report.

Только временные БД. Боевой state.db не читается и не пишется.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_funding.py -q
"""
import os
import sys
import tempfile

sys.path.insert(0, '/home/openclaw')
sys.path.insert(0, '/home/openclaw/bybit-ws')

from bybit_ws.state_db import StateDB  # noqa: E402
import funding_backfill as fb  # noqa: E402


ROW = ('QUSDT', -0.00123456, 0.00005, 1792368000, '106446106_QUSDT_1_0')


def test_upsert_dedup():
    """Одни и те же записи, вставленные дважды, дают одну строку."""
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(os.path.join(d, 'state.db'))
        ins1, seen1 = fb.upsert(db, [ROW])
        assert (ins1, seen1) == (1, 1)
        ins2, seen2 = fb.upsert(db, [ROW, ROW])
        assert (ins2, seen2) == (0, 2)
        rows = db.conn.execute('SELECT COUNT(*), SUM(cash_flow) FROM funding_log').fetchone()
        assert rows == (1, ROW[1])


def test_migrate_idempotent():
    """_migrate() повторно на той же БД не падает и не плодит таблиц."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'state.db')
        StateDB(path).conn.execute('SELECT 1')
        db = StateDB(path)
        cols = db.conn.execute("PRAGMA table_info(funding_log)").fetchall()
        assert [c[1] for c in cols] == ['id', 'symbol', 'cash_flow', 'fee_rate', 'ts', 'external_id']
        idx = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='funding_log'")}
        assert {'idx_funding_ts', 'idx_funding_sym'} <= idx


def test_lp_report_funding_line():
    """lp_report показывает строку funding из funding_log."""
    import lp_report
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'state.db')
        db = StateDB(path)
        fb.upsert(db, [ROW, ('QUSDT', 0.5, 0.00005, 1792368100, 'x2')])
        old = lp_report.DB
        lp_report.DB = path
        try:
            total, n = lp_report.funding_total(None)
        finally:
            lp_report.DB = old
        assert n == 2 and abs(total - 0.49876544) < 1e-6

    # отдельно: report() печатает строку funding
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'state.db')
        db = StateDB(path)
        db.add_trade(symbol='QUSDT', side='Buy', strategy='auto', entry_price=1.0,
                     exit_price=1.1, size=10.0, pnl=1.0, closed_at=1792368000)
        fb.upsert(db, [ROW])
        old = lp_report.DB
        lp_report.DB = path
        try:
            md = lp_report.report(None, 0.0)
        finally:
            lp_report.DB = old
        assert '| Funding (факт' in md and '$-0.00 |' in md.split('Funding')[1][:60]
