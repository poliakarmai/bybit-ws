"""Тест fail-closed поведения risk_manager.check() в секции лимита маржи.

Контракт (item 1): если блок «Проверка максимальной маржи» падает с исключением,
check() обязан вернуть (False, 'risk check failed: ...'), а не молча пропустить
вход (ранее — except: pass → fall-through в return True, '').

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_risk_failclosed.py -q
"""
import os
import sys

# Не гадим в боевой events.log (аналогично test_smoke.py).
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw (как в test_cluster_risk.py).
sys.path.insert(0, '/home/openclaw')


class _BadRisk:
    """risk-конфиг, чей get() падает именно на 'max_total_margin'."""

    def get(self, key, default=None):
        if key == 'max_total_margin':
            raise RuntimeError('boom')
        return default


class _BadCfg:
    risk = _BadRisk()


class _EmptyConn:
    """Мини-стаб sqlite-подключения: anti-ludomania видит пустую историю."""

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return []


class _FakeStateDB:
    """Стаб StateDB: без сделок за последний час → anti-ludomania не блокирует."""

    def __init__(self, *args, **kwargs):
        self.conn = _EmptyConn()


def _benign_daily_pnl() -> dict:
    return {
        'date': '1970-01-01',
        'unrealized_pnl': 0.0,
        'realized_pnl': 0.0,
        'total_pnl': 0.0,
        'position_count': 0,
    }


def test_margin_exception_is_fail_closed(monkeypatch):
    """Падение секции лимита маржи → check() возвращает (False, 'risk check failed: ...')."""
    from bybit_ws import risk_manager

    # Гейты 1–5 при пустом positions должны пропускать поток до секции маржи.
    monkeypatch.setattr(risk_manager, 'check_circuit_breaker', lambda config=None: (False, ''))
    monkeypatch.setattr(risk_manager, 'get_daily_pnl', _benign_daily_pnl)
    monkeypatch.setattr(
        risk_manager,
        'get_dynamic_max_positions',
        lambda: {'max_positions': 12, 'volatility_level': 'normal'},
    )
    monkeypatch.setattr('bybit_ws.state_db.StateDB', _FakeStateDB)

    allowed, reason = risk_manager.check(
        {}, new_symbol='FOOUSDT', new_side='Buy', config=_BadCfg()
    )

    assert allowed is False
    assert 'risk check failed' in reason
