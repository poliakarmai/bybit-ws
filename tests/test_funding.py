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


# ── Регресс этапа 1.5: пагинация не теряет записи ─────────────────────────

def _fake_exchange(total=120):
    """Фейковый transaction-log: 120 записей, по 50 на страницу, курсор = смещение.

    Времена — с ненулевыми миллисекундами (как у Bybit): именно на них старый
    сдвиг окна «до секунды» терял записи.
    """
    now = 1792750000
    recs = [{'id': f'r{i}', 'type': 'SETTLEMENT', 'symbol': 'QUSDT', 'change': '-0.01',
             'transactionTime': str((now - i * 60) * 1000 + 37)} for i in range(total)]

    def fetch(method, path):
        off = int(path.split('cursor=')[1]) if 'cursor=' in path else 0
        page = recs[off:off + 50]
        nxt = str(off + 50) if off + 50 < total else ''
        return {'retCode': 0, 'result': {'list': page, 'nextPageCursor': nxt}}

    return recs, fetch


def test_iter_window_loses_nothing():
    """Все записи доходят: курсорная пагинация, дедупа нет, потерь нет."""
    recs, fetch = _fake_exchange()
    start, end = 1792750000 - 6 * 86400, 1792750001
    got = list(fb._iter_window(start, end, fetch=fetch))
    assert len(got) == len(recs), f'получено {len(got)} из {len(recs)}'
    assert len({r['id'] for r in got}) == len(recs), 'есть дубли'
