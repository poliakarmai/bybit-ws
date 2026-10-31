"""Регресс: pytest не пишет в БОЕВУЮ risk_decisions (найдено 30.10.2026).

Тесты гоняют risk_manager.check() на фикстурах (BTCUSDT/FOOUSDT/AAAUSDT) и через
синглтон state_db.db писали в боевую базу: 2860 строк, живых 0. Из-за этого замер
shadow-режима кластерного риска («22 срабатывания») набрался запусками pytest.

Гейт: BYBIT_LOG_SILENT=1 глушит запись ТОЛЬКО в БД по боевому пути; тесты с
инжектированной временной БД пишут как обычно (см. tests/test_risk_journal.py).

Подмена — через sys.modules, не через атрибут пакета: test_kill_switch.py
пересоздаёт модули bybit_ws.* (pop + re-import), и ссылка, взятая на импорте,
указывает на устаревший объект.
"""
import os
import sys
import types

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

from bybit_ws import risk_manager as rm  # noqa: E402
from bybit_ws import state_db as sdb  # noqa: E402

PROD_DB_PATH = sdb.DB_PATH


class _Spy:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def add_risk_decision(self, *a, **k):
        self.calls.append(a)


def _inject(monkeypatch, spy):
    """Подменить bybit_ws.state_db целиком: _log_decision читает её на каждом вызове."""
    stub = types.ModuleType('bybit_ws.state_db')
    stub.DB_PATH = PROD_DB_PATH
    stub.db = spy
    monkeypatch.setitem(sys.modules, 'bybit_ws.state_db', stub)


def test_prod_db_not_written_under_pytest(monkeypatch):
    spy = _Spy(PROD_DB_PATH)
    _inject(monkeypatch, spy)
    monkeypatch.setenv('BYBIT_LOG_SILENT', '1')

    rm._log_decision('AAAUSDT', 'OVER_POSITIONS', False, 'fixture')
    assert spy.calls == [], 'pytest пишет в боевую risk_decisions'


def test_temp_db_written_even_under_pytest(monkeypatch):
    """Тесты самого журнала работают на временной БД — гейт им не мешает."""
    spy = _Spy('/tmp/whatever-state.db')
    _inject(monkeypatch, spy)
    monkeypatch.setenv('BYBIT_LOG_SILENT', '1')

    rm._log_decision('BTCUSDT', 'banned_symbol', False, 'tmp db')
    assert len(spy.calls) == 1


def test_prod_db_written_outside_pytest(monkeypatch):
    """Гейт не глушит прод: без флага журнал пишется и в боевую БД."""
    spy = _Spy(PROD_DB_PATH)
    _inject(monkeypatch, spy)
    monkeypatch.setenv('BYBIT_LOG_SILENT', '0')

    rm._log_decision('BTCUSDT', 'all_passed', True, 'live')
    assert len(spy.calls) == 1
