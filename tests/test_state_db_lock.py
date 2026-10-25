"""Аудит #19: StateDB сериализует записи через threading.Lock.

Два писателя на одном соединении (asyncio-цикл main_async.py и RPC-поток
rpc.py) раньше могли перемешать `execute`/`commit` и `SELECT last_insert_rowid()`
— id, возвращённый из add_trade, мог принадлежать чужой вставке.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_state_db_lock.py -q
"""
import asyncio
import os
import sys
import threading

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture()
def sdb(tmp_path):
    mod = pytest.importorskip('bybit_ws.state_db')
    db = mod.StateDB(str(tmp_path / 'state.db'))
    db.conn  # схема + миграции
    yield db
    db.close()


def test_has_threading_lock(sdb):
    """StateDB должен иметь _lock — потоковый примитив, не asyncio.Lock."""
    lock = getattr(sdb, '_lock', None)
    assert lock is not None, 'StateDB без _lock — записи не сериализованы'
    assert hasattr(lock, 'acquire') and hasattr(lock, 'release')
    assert not isinstance(lock, asyncio.Lock), \
        'threading-лок обязателен: писатели живут в разных потоках'


def test_concurrent_writes_serialized(sdb):
    """Два потока пишут одновременно: id из add_trade принадлежит своей вставке."""
    n = 50
    errors = []

    def writer(tag):
        try:
            for i in range(n):
                sym = f'{tag}{i}USDT'
                trade_id = sdb.add_trade(sym, 'Buy', 'test', 1.0, 1.0, 1.0, 0.0)
                row = sdb.conn.execute(
                    'SELECT symbol FROM trade_history WHERE id=?', (trade_id,)).fetchone()
                assert row and row[0] == sym, f'{tag}: id {trade_id} -> {row}'
                sdb.set_kv(f'{tag}:{i}', i)
        except Exception as e:  # noqa: BLE001 — нужен любой сбой потока
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ('AAA', 'BBB')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(sdb.get_trades(limit=1000)) == 2 * n
    assert len([k for k in ('AAA', 'BBB')
                if all(sdb.get_kv(f'{k}:{i}') == i for i in range(n))]) == 2
