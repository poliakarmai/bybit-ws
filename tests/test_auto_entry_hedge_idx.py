"""Тест _resolve_long_idx: LONG-вход в hedge-символе без открытой позиции.

Регресс на баг 12.11.2026: POST-ONLY LONG {LINK,ARB,ENA,DOGE,ADA}USDT получал
positionIdx=0 (дефолт one-way), хотя символ в hedge-режиме, → Bybit 10001
«position idx not match position mode» каждый цикл. hedge-режим виден по наличию
positionIdx 1/2 в ответе position/list даже при size=0.

    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_auto_entry_hedge_idx.py -q
"""

import pytest

from bybit_ws import auto_entry


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """position_idx/learn_position_idx изолированы от реального кэша _HEDGE_SYMS."""
    learned = {}
    monkeypatch.setattr(auto_entry, 'position_idx', lambda sym, side: 0)
    monkeypatch.setattr(auto_entry, 'learn_position_idx', lambda sym, idx: learned.__setitem__(sym, idx))
    return learned


def _p(idx, size='0'):
    return {'positionIdx': idx, 'size': size}


def test_hedge_without_position_returns_1(_isolate):
    pos_data = {'retCode': 0, 'result': {'list': [_p(1), _p(2)]}}
    assert auto_entry._resolve_long_idx(pos_data, 'LINKUSDT') == 1
    assert _isolate['LINKUSDT'] == 1  # режим запомнен


def test_hedge_with_open_short_still_returns_1(_isolate):
    # LONG в hedge идёт на idx=1, даже если открыт SHORT на idx=2
    pos_data = {'retCode': 0, 'result': {'list': [_p(1, '0'), _p(2, '5')]}}
    assert auto_entry._resolve_long_idx(pos_data, 'ARBUSDT') == 1


def test_one_way_with_position_returns_its_idx(_isolate):
    pos_data = {'retCode': 0, 'result': {'list': [_p(0, '5')]}}
    assert auto_entry._resolve_long_idx(pos_data, 'BTCUSDT') == 0


def test_one_way_empty_returns_default(_isolate):
    pos_data = {'retCode': 0, 'result': {'list': [_p(0, '0')]}}
    assert auto_entry._resolve_long_idx(pos_data, 'ETHUSDT') == 0


def test_api_error_or_none_returns_default(_isolate):
    assert auto_entry._resolve_long_idx({'retCode': 10001, 'result': {}}, 'XUSDT') == 0
    assert auto_entry._resolve_long_idx(None, 'XUSDT') == 0
