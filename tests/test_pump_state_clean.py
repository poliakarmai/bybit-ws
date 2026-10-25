"""TASK H: pump_state не должен стираться для только что открытой позиции.

Симптом (замер 18.10 по /v5/execution/list, QUSDT):
    18:26:40 Sell 130   ← вход
    18:26:41 🧹 pump_state clean: QUSDT (позиция закрыта)   ← позиция ОТКРЫТА
    18:31:41 Sell 130   ← повторный вход → позиция 260 вместо 130
Причём это шорт «БЕЗ SL», т.е. удвоение экспозиции без стопа на пампе +290%.
Тот же дубль: 17:46:38 (Sell 150) → 17:51:38 (Sell 140).

Корень: `_clean_pump_state` вызывался ПОСЛЕ check_pumps/check_weekly_pumps и
судил по снапшоту `positions`, взятому в начале цикла — только что открытый
символ в нём отсутствует.

⚠️ 25.10: функция перешла на StateDB-singleton и БЕЗ инъекции била в ПРОДОВУЮ
state.db — прогон этих кейсов вычищал живой pump_state. Поэтому db передаётся
явно третьим аргументом, прод не трогается.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_pump_state_clean.py -q
"""
import json
import os
import sys
from pathlib import Path

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402

from bybit_ws.main_async import _clean_pump_state  # noqa: E402
from bybit_ws.state_db import StateDB  # noqa: E402


def _make_data_dir(tmp_path, symbols):
    """Мини-каталог данных: state.db с pump_state + pumps.json. Возвращает (dir, db)."""
    db = StateDB(str(tmp_path / 'state.db'))
    db.conn  # схема
    db.conn.executemany('INSERT OR REPLACE INTO pump_state (symbol) VALUES (?)',
                        [(s,) for s in symbols])
    db.conn.commit()
    (tmp_path / 'pumps.json').write_text(
        '{%s}' % ','.join('"%s": {}' % s for s in symbols))
    return Path(tmp_path), db


def _pump_symbols(db):
    return {r[0] for r in db.conn.execute('SELECT symbol FROM pump_state')}


def test_keeps_symbols_with_open_position(tmp_path):
    """Символ с открытой позицией остаётся — иначе следующий цикл входит повторно."""
    d, db = _make_data_dir(tmp_path, ['QUSDT', 'XRPUSDT'])
    try:
        _clean_pump_state(d, {'XRPUSDT': {'size': 100}}, db)
        assert _pump_symbols(db) == {'XRPUSDT'}
        assert list(json.loads((d / 'pumps.json').read_text())) == ['XRPUSDT']
    finally:
        db.close()


def test_removes_symbols_without_position(tmp_path):
    """Символ без позиции вычищается — иначе состояние копится вечно."""
    d, db = _make_data_dir(tmp_path, ['QUSDT', 'XRPUSDT'])
    try:
        _clean_pump_state(d, {'XRPUSDT': {'size': 100}}, db)
        assert 'QUSDT' not in _pump_symbols(db)
    finally:
        db.close()


def test_removes_all_when_flat(tmp_path):
    d, db = _make_data_dir(tmp_path, ['QUSDT'])
    try:
        _clean_pump_state(d, {}, db)
        assert _pump_symbols(db) == set()
    finally:
        db.close()


def test_clean_runs_before_pump_entries():
    """Инвариант порядка: уборщик вызывается ДО задач, открывающих позицию.

    Именно нарушение этого порядка дало дубль QUSDT 260 вместо 130: clean
    выполнялся после check_weekly_pumps и судил по устаревшему снапшоту.
    """
    src = Path('/home/openclaw/bybit-ws/main_async.py').read_text()
    i_clean = src.find('_clean_pump_state, DATA_DIR')
    i_entry = src.find('run_in_thread(check_pumps, positions')
    assert i_clean != -1, 'вызов _clean_pump_state пропал из main_async.py'
    assert i_entry != -1, 'вызов check_pumps пропал из main_async.py'
    assert i_clean < i_entry, (
        'pump_state снова чистится ПОСЛЕ входа в позицию — вернётся дубль '
        'входа по пампу (QUSDT 18.10: 260 вместо 130, шорт без SL)')
