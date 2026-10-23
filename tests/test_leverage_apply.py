"""Тесты этапа 2 (плечо): авто-ветка выставляет плечо из конфига, mismatch → алерт,
ручной /enter без явного плеча ничего не навязывает.

Без сети: bybit()/`_api_call` подменяются фейками, events.log глушится.
"""
import os
import re
import sys
import types

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')  # bybit_ws -> ~/bybit-ws

from bybit_ws import api, rpc  # noqa: E402
from bybit_ws import auto_entry  # noqa: E402


class _Recorder:
    """Фейковый bybit(): пишет вызовы, отдаёт заранее заданные ответы."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, path, body=None, retries=None):
        self.calls.append((method, path, body))
        for key, resp in self.responses.items():
            if key in path:
                return resp
        return {'retCode': 0, 'retMsg': 'OK'}

    def paths(self):
        return [c[1] for c in self.calls]


class _FakeConf:
    def __init__(self, long_lev=3, short_lev=5):
        self.strategy = types.SimpleNamespace(
            long={'leverage': long_lev},
            short={'leverage': short_lev},
        )


def test_auto_path_config_leverage_reaches_set_leverage(monkeypatch):
    """(а) auto_entry: плечо ИЗ КОНФИГА уходит в POST /v5/position/set-leverage до ордера."""
    rec = _Recorder()
    monkeypatch.setattr(api, 'bybit', rec)
    monkeypatch.setattr(auto_entry, 'Config', lambda: _FakeConf(long_lev=7))

    lev = auto_entry.apply_entry_leverage('SOLUSDT', 'Buy')

    assert lev == 7
    assert len(rec.calls) == 1
    method, path, body = rec.calls[0]
    assert (method, path) == ('POST', '/v5/position/set-leverage')
    assert body['symbol'] == 'SOLUSDT'
    assert body['buyLeverage'] == '7' and body['sellLeverage'] == '7'
    assert body['category'] == 'linear'


def test_auto_path_short_side_uses_short_leverage(monkeypatch):
    """(а, продолжение) SHORT-ветка берёт config.strategy.short.leverage."""
    rec = _Recorder()
    monkeypatch.setattr(api, 'bybit', rec)
    monkeypatch.setattr(auto_entry, 'Config', lambda: _FakeConf(short_lev=5))

    assert auto_entry.apply_entry_leverage('SUIUSDT', 'Sell') == 5
    assert rec.calls[0][2]['buyLeverage'] == '5'


def test_leverage_mismatch_raises_alert(monkeypatch):
    """(б) фактическое плечо на бирже ≠ запрошенному → mismatch=True + алерт."""
    rec = _Recorder({
        '/v5/position/list': {'retCode': 0, 'result': {'list': [
            {'symbol': 'ZKUSDT', 'size': '120', 'leverage': '25'},
        ]}},
    })
    alerts = []
    monkeypatch.setattr(api, 'bybit', rec)
    monkeypatch.setattr(api, 'add_alert', lambda level, msg: alerts.append((level, msg)))

    res = api.verify_leverage('ZKUSDT', 3)

    assert res == {'requested': 3, 'actual': 25.0, 'mismatch': True}
    assert alerts and alerts[0][0] == 'LEVERAGE'
    assert 'ZKUSDT' in alerts[0][1] and '25' in alerts[0][1]


def test_leverage_match_no_alert(monkeypatch):
    """Обратная сторона: плечо совпало — тишина, mismatch=False."""
    rec = _Recorder({
        '/v5/position/list': {'retCode': 0, 'result': {'list': [
            {'symbol': 'SOLUSDT', 'size': '10', 'leverage': '3'},
        ]}},
    })
    alerts = []
    monkeypatch.setattr(api, 'bybit', rec)
    monkeypatch.setattr(api, 'add_alert', lambda level, msg: alerts.append((level, msg)))

    res = api.verify_leverage('SOLUSDT', 3)

    assert res['mismatch'] is False and res['actual'] == 3.0
    assert alerts == []


def test_manual_enter_without_leverage_imposes_nothing(monkeypatch):
    """(в) ручной /enter без явного плеча НЕ шлёт set-leverage (даже если в конфиге 3)."""
    calls = []
    monkeypatch.setattr(rpc, '_api_call',
                        lambda m, p, b=None: calls.append((m, p, b)) or {'retCode': 0})

    assert rpc._apply_manual_leverage('XRPUSDT', None) is None
    assert calls == [], 'ручной путь не должен трогать плечо'


def test_manual_enter_uses_explicit_leverage(monkeypatch):
    """Ручной /enter с явным плечом Алексея: ставим ровно его, без капов."""
    calls = []
    monkeypatch.setattr(rpc, '_api_call',
                        lambda m, p, b=None: calls.append((m, p, b)) or {'retCode': 0})

    assert rpc._apply_manual_leverage('XRPUSDT', 25) == 25
    assert calls[0][0:2] == ('POST', '/v5/position/set-leverage')
    assert calls[0][2]['buyLeverage'] == '25'


def test_manual_entry_marks_position_exactly_once():
    """Регресс: ручной /enter метит позицию ОДНИМ вызовом mark_manual_position.

    Два вызова (первый с note+auto_sltp, второй с leverage) перезаписывали друг друга —
    позиция теряла auto_sltp=True и заметку, т.е. оставалась без авто-SL/TP.
    """
    src = open(os.path.join(os.path.dirname(__file__), '..', 'rpc.py'), encoding='utf-8').read()
    hits = [m.start() for m in re.finditer(r'mark_manual_position\(', src)]
    assert len(hits) == 1, f'mark_manual_position вызывается {len(hits)} раз'
    call = ' '.join(src[hits[0]:hits[0] + 200].split())
    assert 'auto_sltp=True' in call, f'потерян auto_sltp: {call}'
    assert 'leverage=' in call, f'плечо не передаётся: {call}'
