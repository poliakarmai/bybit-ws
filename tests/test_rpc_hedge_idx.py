"""Тесты маппинга positionIdx для hedge-режима в rpc.py.

Bybit v5 hedge: Buy (лонг) = 1, Sell (шорт) = 2.
Без живого API и без запуска HTTP-сервера rpc.py.
"""
import os
import sys

# Не гадим в боевой events.log (как в соседних тестах).
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw (как в test_tp_min_qty.py).
sys.path.insert(0, '/home/openclaw')

from bybit_ws.rpc import _hedge_idx  # noqa: E402


def test_hedge_idx_sell_is_two():
    assert _hedge_idx('Sell') == 2


def test_hedge_idx_buy_is_one():
    assert _hedge_idx('Buy') == 1


def test_hedge_idx_sell_never_one():
    # Точное значение из /enter (валидация выше уже strip()-нула): Sell НИКОГДА не 1.
    assert _hedge_idx('Sell') != 1
    assert _hedge_idx('Sell') == 2


def test_hedge_idx_non_sell_defaults_to_one():
    # Всё, что не ровно 'Sell', трактуется как лонг-сторона (1). Варианты регистра/
    # пробелов сюда не доходят (валидация), поэтому неявно к 2 не маппятся.
    for raw in ('Buy', 'buy', 'BUY', ' Buy ', '', 'Sell ', ' Sell'):
        assert _hedge_idx(raw) == 1, f'{raw!r} must map to 1'
