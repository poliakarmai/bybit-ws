"""#7: отказы риск-проверок попадают в SQLite (таблица risk_decisions).

Проверяем реальный risk_manager.check() на временной БД — журнал должен
содержать отклонение с passed=0 и именем сработавшей проверки.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_risk_journal.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


class _Cfg:
    """Минимальный Config для risk_manager.check()."""

    def __init__(self, **risk):
        self.risk = {'banned_symbols': [], 'max_daily_loss': 50,
                     'max_total_margin': 300, 'dynamic_margin_pct': 0, **risk}


@pytest.fixture()
def rm(tmp_path, monkeypatch):
    sdb = pytest.importorskip('bybit_ws.state_db')
    rm = pytest.importorskip('bybit_ws.risk_manager')
    db = sdb.StateDB(str(tmp_path / 'state.db'))
    db.conn  # схема
    monkeypatch.setattr(sdb, 'db', db)          # _log_decision берёт singleton отсюда
    monkeypatch.setattr(rm, 'get_daily_pnl',
                        lambda: {'total_pnl': 0.0, 'date': '2026-10-25',
                                 'unrealized_pnl': 0.0, 'realized_pnl': 0.0,
                                 'position_count': 0})
    monkeypatch.setattr(rm, 'check_circuit_breaker', lambda config=None: (False, ''))
    monkeypatch.setattr(rm, 'get_dynamic_max_positions',
                        lambda: {'max_positions': 12, 'volatility_level': 'normal'})
    yield rm, db
    db.close()


def _rows(db):
    return db.get_risk_decisions(limit=50)


def test_table_exists_with_columns(rm):
    _rm, db = rm
    cols = [d[1] for d in db.conn.execute('PRAGMA table_info(risk_decisions)')]
    assert cols == ['id', 'ts', 'symbol', 'check', 'passed', 'reason', 'margin', 'equity']


def test_banned_symbol_logged_as_failed(rm):
    rm_, db = rm
    allowed, reason = rm_.check({}, new_symbol='BTCUSDT',
                                config=_Cfg(banned_symbols=['BTCUSDT']))
    assert allowed is False and reason
    rows = _rows(db)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r['check'] == 'banned_symbol'
    assert r['passed'] == 0
    assert r['symbol'] == 'BTCUSDT'
    assert 'бан-лист' in r['reason']


def test_circuit_breaker_logged(rm, monkeypatch):
    rm_, db = rm
    monkeypatch.setattr(rm_, 'check_circuit_breaker',
                        lambda config=None: (True, 'CB: daily loss limit'))
    allowed, reason = rm_.check({}, new_symbol='ETHUSDT')
    assert allowed is False
    rows = _rows(db)
    assert [(r['check'], r['passed']) for r in rows] == [('circuit_breaker', 0)]
    assert rows[0]['reason'] == reason


def test_max_positions_logged(rm, monkeypatch):
    rm_, db = rm
    monkeypatch.setattr(rm_, 'get_dynamic_max_positions',
                        lambda: {'max_positions': 1, 'volatility_level': 'high'})
    allowed, _ = rm_.check({'BTCUSDT': {'margin': 5, 'size': 1}}, new_symbol='ETHUSDT', config=_Cfg())
    assert allowed is False
    assert _rows(db)[0]['check'] == 'max_positions'


def test_stub_position_with_zero_size_not_counted(rm, monkeypatch):
    """Фаза 11 D1: заглушки _note_entry (size=0) — не позиция, лимит не съедают."""
    rm_, db = rm
    monkeypatch.setattr(rm_, 'get_dynamic_max_positions',
                        lambda: {'max_positions': 1, 'volatility_level': 'high'})
    allowed, reason = rm_.check({'BTCUSDT': {'margin': 0, 'size': 0}},
                                new_symbol='ETHUSDT', config=_Cfg())
    assert allowed is True, reason


def test_daily_loss_logged(rm, monkeypatch):
    rm_, db = rm
    monkeypatch.setattr(rm_, 'get_daily_pnl',
                        lambda: {'total_pnl': -80.0, 'date': '2026-10-25',
                                 'unrealized_pnl': 0.0, 'realized_pnl': -80.0,
                                 'position_count': 0})
    allowed, _ = rm_.check({}, new_symbol='ETHUSDT', config=_Cfg(max_daily_loss=50))
    assert allowed is False
    assert _rows(db)[0]['check'] == 'daily_loss'


def test_concentration_logged(rm, monkeypatch):
    rm_, db = rm
    monkeypatch.setattr(rm_, '_get_wallet_balance', lambda: {'equity': 100.0})
    allowed, reason, pct = rm_.check_symbol_concentration('BTCUSDT', 50.0, {})
    assert allowed is False and pct > 0.05
    r = _rows(db)[0]
    assert r['check'] == 'concentration'
    assert r['passed'] == 0
    assert r['margin'] == 50.0
    assert r['equity'] == 100.0


def test_passing_entry_logged_as_passed(rm):
    """Разрешённый вход тоже пишется — иначе журнал не объясняет «почему вошли»."""
    rm_, db = rm
    allowed, reason = rm_.check({}, new_symbol='ETHUSDT', config=_Cfg())
    assert allowed is True, reason
    r = _rows(db)[0]
    assert (r['check'], r['passed']) == ('all_passed', 1)


def test_heartbeat_not_logged(rm):
    """check(positions) без нового входа — heartbeat каждый цикл, журнал не засоряем."""
    rm_, db = rm
    rm_.check({}, new_symbol=None)
    assert _rows(db) == []


def test_journal_failure_does_not_block_trading(tmp_path, monkeypatch):
    """Сломанная БД не должна запрещать вход — решение важнее аудита."""
    sdb = pytest.importorskip('bybit_ws.state_db')
    rm_ = pytest.importorskip('bybit_ws.risk_manager')

    class _Broken:
        def add_risk_decision(self, *a, **k):
            raise RuntimeError('db is gone')

    monkeypatch.setattr(sdb, 'db', _Broken())
    monkeypatch.setattr(rm_, 'check_circuit_breaker', lambda config=None: (False, ''))
    monkeypatch.setattr(rm_, 'get_dynamic_max_positions',
                        lambda: {'max_positions': 12, 'volatility_level': 'normal'})
    monkeypatch.setattr(rm_, 'get_daily_pnl',
                        lambda: {'total_pnl': 0.0, 'date': '2026-10-25',
                                 'unrealized_pnl': 0.0, 'realized_pnl': 0.0,
                                 'position_count': 0})
    allowed, reason = rm_.check({}, new_symbol='ETHUSDT', config=_Cfg())
    assert allowed is True, reason
