"""Тест телеметрии входа: save_entry_diagnostic / get_entry_diagnostic round-trip.

Run: cd /home/openclaw/bybit-ws && python3 -m pytest test_entry_diag.py -q
"""
import sys

sys.path.insert(0, '/home/openclaw')


def test_entry_diag_roundtrip(tmp_path):
    from bybit_ws.state_db import StateDB
    db = StateDB(path=str(tmp_path / 'test.db'))

    db.save_entry_diagnostic('BTCUSDT', 'Buy', bb_pct=45.5, rsi=32.1, entry_reason='low_bb')
    diag = db.get_entry_diagnostic('BTCUSDT', 'Buy')
    assert diag is not None
    assert diag['bb_pct'] == 45.5
    assert diag['rsi'] == 32.1
    assert diag['entry_reason'] == 'low_bb'

    # pop-семантика: после чтения — удалено
    assert db.get_entry_diagnostic('BTCUSDT', 'Buy') is None

    # другой side не затирает
    db.save_entry_diagnostic('BTCUSDT', 'Sell', bb_pct=80.0, rsi=70.0, entry_reason='overbought')
    assert db.get_entry_diagnostic('BTCUSDT', 'Sell')['bb_pct'] == 80.0

    db.close()


def test_entry_diag_missing_returns_none(tmp_path):
    from bybit_ws.state_db import StateDB
    db = StateDB(path=str(tmp_path / 'test2.db'))
    assert db.get_entry_diagnostic('NOPEUSDT', 'Buy') is None
    db.close()
