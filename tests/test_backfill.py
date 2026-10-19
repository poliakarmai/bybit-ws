"""Бэкфилл: pnl не трогается + пустые поля заполняются.

Только временная БД, боевой state.db не читается.
"""
import os
import sys
import tempfile

sys.path.insert(0, '/home/openclaw')
sys.path.insert(0, '/home/openclaw/bybit-ws')

from bybit_ws.state_db import StateDB  # noqa: E402


def test_backfill_update_only_empty():
    """UPDATE ... WHERE fees IS NULL OR fees=0 не перезаписывает уже заполненную строку."""
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(os.path.join(d, 'state.db'))
        db.add_trade(symbol='QUSDT', side='Buy', strategy='auto', entry_price=1.0,
                     exit_price=1.1, size=10.0, pnl=1.0, closed_at=1792368000)
        db.add_trade(symbol='LTCUSDT', side='Buy', strategy='auto', entry_price=1.0,
                     exit_price=1.1, size=10.0, pnl=2.0, closed_at=1792368100)
        db.conn.execute("UPDATE trade_history SET fees=0.5, leverage=3 WHERE symbol='LTCUSDT'")
        db.conn.commit()
        pnl_before = db.conn.execute('SELECT ROUND(SUM(pnl),2) FROM trade_history').fetchone()[0]

        for sym, fees in (('QUSDT', 0.25), ('LTCUSDT', 9.99)):
            db.conn.execute(
                'UPDATE trade_history SET fees=? WHERE symbol=? AND (fees IS NULL OR fees=0)',
                (fees, sym))
        db.conn.commit()

        got = dict(db.conn.execute('SELECT symbol, fees FROM trade_history').fetchall())
        assert got == {'QUSDT': 0.25, 'LTCUSDT': 0.5}, got
        assert db.conn.execute('SELECT ROUND(SUM(pnl),2) FROM trade_history').fetchone()[0] == pnl_before
