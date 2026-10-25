"""Регрессия второго захода (25.10): simple-trail без существующего SL + учёт входа в снапшоте.

Проверяет два места, где логика расходилась с заявленной:

1. `unified_sl._calc_simple_trail` требовал `current_sl > entry` — то есть трейлинг
   «каждые +5%» не включался, пока SL не подтянут другими приоритетами. При +6%
   и дефолтном SL под entry позиция оставалась с SL −5..−10%.
2. `main_async._note_entry` — только что открытый вход должен попадать в локальный
   снапшот `positions`, иначе `risk_check` следующего источника в том же цикле
   не видит новую позицию (лимит позиций/корреляция обходятся).

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_sl_trail_and_snapshot.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture()
def usl():
    return pytest.importorskip('bybit_ws.unified_sl')


@pytest.fixture()
def m():
    return pytest.importorskip('bybit_ws.main_async')


def _pos(**kw):
    base = {'entry': 100.0, 'mark': 100.0, 'side': 'Buy', 'size': 10.0}
    base.update(kw)
    return base


# ── simple trail ────────────────────────────────────────────

def test_simple_trail_without_sl_locks_half_profit(usl):
    """+6% без SL: SL ставится на половину пути (entry+3%), а не остаётся дефолтным."""
    target, desc = usl._calc_simple_trail(_pos(mark=106.0), True, 106.0, 100.0)
    assert target == pytest.approx(103.0), f'ожидали половину пути 103.0, получили {target}'
    assert 'simple trail LONG' in desc


def test_simple_trail_ignores_sl_below_entry(usl):
    """SL ниже entry (дефолтная защита) не блокирует трейлинг."""
    target, _ = usl._calc_simple_trail(_pos(mark=110.0, stopLoss=95.0), True, 110.0, 100.0)
    assert target == pytest.approx(105.0), target


def test_simple_trail_never_moves_backwards(usl):
    """SL уже выше половины пути → двигать некуда (монотонность)."""
    target, _ = usl._calc_simple_trail(_pos(mark=110.0, stopLoss=107.0), True, 110.0, 100.0)
    assert target is None, f'SL откатился бы назад: {target}'


def test_simple_trail_short_mirror(usl):
    """SHORT: +6% (цена ниже entry) → SL на половину пути вниз."""
    target, desc = usl._calc_simple_trail(_pos(side='Sell', mark=94.0), False, 94.0, 100.0)
    assert target == pytest.approx(97.0), target
    assert 'SHORT' in desc


def test_simple_trail_below_threshold(usl):
    """Меньше +5% — не трогаем."""
    target, _ = usl._calc_simple_trail(_pos(mark=104.9), True, 104.9, 100.0)
    assert target is None


# ── учёт входа в снапшоте ───────────────────────────────────

def test_note_entry_adds_placeholder(m):
    """Новый символ появляется в снапшоте — risk_check увидит его в этом же цикле."""
    positions = {'BTCUSDT': {'symbol': 'BTCUSDT', 'size': 1.0}}
    m._note_entry(positions, 'SOLUSDT', 'Sell')

    assert 'SOLUSDT' in positions, 'вход не попал в снапшот'
    assert positions['SOLUSDT']['size'] == 0, 'заглушка не должна выглядеть как реальная позиция'
    assert positions['SOLUSDT']['side'] == 'Sell'
    assert len(positions) == 2


def test_note_entry_does_not_overwrite_real_position(m):
    """Существующая позиция не подменяется заглушкой."""
    real = {'symbol': 'BTCUSDT', 'size': 5.0, 'entry': 60000.0}
    positions = {'BTCUSDT': real}
    m._note_entry(positions, 'BTCUSDT', 'Buy')
    assert positions['BTCUSDT'] is real
    assert positions['BTCUSDT']['size'] == 5.0


def test_note_entry_ignores_empty_symbol(m):
    positions = {}
    m._note_entry(positions, None, 'Buy')
    assert positions == {}
