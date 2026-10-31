"""Ручная метка переживает закрытие позиции (иначе импорт закрытия её не видит).

Инцидент 30.10: ликвидация четырёх ручных позиций легла в базу как manual=0 —
цикл чистит pumps.json ДО импорта closed-pnl, и метка исчезала за секунды до
того, как её спрашивали. Ручные сделки попадали в self-learn (по замеру 26.10
они дают почти весь минус аккаунта).

В бою pumps.json один: DATA_DIR и PUMP_STATE_FILE указывают на ~/.local/share/bybit-ws.
"""

import json
import time

import pytest

import bybit_ws.main_async as ma
import bybit_ws.manual_positions as mp
from bybit_ws.state_db import StateDB


@pytest.fixture
def env(tmp_path, monkeypatch):
    pumps = tmp_path / 'pumps.json'
    pumps.write_text('{}')
    monkeypatch.setattr(mp, 'PUMP_STATE_FILE', str(pumps))
    db = StateDB(path=str(tmp_path / 'state.db'))
    yield tmp_path, db, pumps
    db.close()


def _clean(env, positions):
    data_dir, db, pumps = env
    ma._clean_pump_state(data_dir, positions, db=db)
    return json.loads(pumps.read_text())


def test_manual_mark_survives_close_until_import(env):
    mp.mark_manual_position('DOTUSDT', entry=0.9167, side='Buy', leverage=75)
    assert mp.is_manual_position('DOTUSDT')

    state = _clean(env, positions={})  # позиция ушла с рынка
    assert 'DOTUSDT' in state, 'метка не должна исчезать до импорта закрытия'
    assert state['DOTUSDT']['closed_at'] > 0
    # живые модули (SL/TP) закрытую метку не видят, импорт — видит
    assert mp.is_manual_position('DOTUSDT') is False
    assert mp.is_manual_position('DOTUSDT', include_closed=True) is True


def test_old_closed_mark_is_pruned(env):
    mp.mark_manual_position('DOTUSDT', entry=1.0, side='Buy')
    _clean(env, positions={})
    pumps = env[2]
    state = json.loads(pumps.read_text())
    state['DOTUSDT']['closed_at'] = time.time() - 49 * 3600
    pumps.write_text(json.dumps(state))

    assert 'DOTUSDT' not in _clean(env, positions={})


def test_auto_reentry_clears_closed_mark(env):
    mp.mark_manual_position('DOTUSDT', entry=1.0, side='Buy')
    _clean(env, positions={})
    state = _clean(env, positions={'DOTUSDT': {'side': 'Buy', 'size': 1.0}})
    assert 'closed_at' not in state['DOTUSDT']
    assert mp.is_manual_position('DOTUSDT') is True  # сам ручной вход ещё жив


def test_non_manual_orphans_removed_as_before(env):
    pumps = env[2]
    pumps.write_text(json.dumps({'PUMPUSDT': {'pump': True}}))
    assert _clean(env, positions={}) == {}
