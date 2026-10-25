"""Аудит #20: trade_history.external_id — частичный UNIQUE-индекс.

Колонка добавлена через ALTER TABLE (без UNIQUE), поэтому дубли transaction-log
id проходили. Теперь в _migrate() создаётся частичный UNIQUE-индекс
(WHERE external_id IS NOT NULL) и чистятся уже накопленные дубли.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_external_id_unique.py -q
"""
import os
import sqlite3
import sys

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


def _add(db, external_id):
    return db.add_trade('BTCUSDT', 'LONG', 'test', 100.0, 101.0, 1.0, 1.0,
                        external_id=external_id)


def test_duplicate_external_id_raises(sdb):
    """Вторая строка с тем же external_id падает с IntegrityError."""
    _add(sdb, 'ex-1')
    with pytest.raises(sqlite3.IntegrityError):
        _add(sdb, 'ex-1')


def test_null_external_id_allowed(sdb):
    """Старые записи без external_id (NULL) индекс не блокирует."""
    _add(sdb, None)
    _add(sdb, None)
    n = sdb.conn.execute(
        "SELECT COUNT(*) FROM trade_history WHERE external_id IS NULL").fetchone()[0]
    assert n == 2


def test_migration_dedupes_existing_rows(tmp_path):
    """Старая БД с дублями: миграция оставляет MIN(id), счётчик не растёт."""
    mod = pytest.importorskip('bybit_ws.state_db')
    path = str(tmp_path / 'old.db')
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE trade_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL,
        closed_at INTEGER, external_id TEXT)""")
    for eid in ['dup', 'dup', 'dup', None]:
        raw.execute("INSERT INTO trade_history (symbol, side, closed_at, external_id)"
                    " VALUES ('BTCUSDT', 'LONG', 1, ?)", (eid,))
    raw.commit()
    raw.close()

    db = mod.StateDB(path)
    db.conn  # _migrate() чистит дубли + создаёт индекс
    rows = db.conn.execute(
        "SELECT id, external_id FROM trade_history ORDER BY id").fetchall()
    db.close()

    assert [r[1] for r in rows] == ['dup', None], f'осталось: {rows}'
    assert rows[0][0] == 1, 'должна остаться первая (MIN(id)) строка'
