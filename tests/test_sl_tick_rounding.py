"""Регресс: SL не должен уезжать по неверную сторону рынка на дешёвых монетах.

Баг 27.10 (RVNUSDT): entry 0.0014, mark 0.001364. _calc_default_sl возвращал
round(entry*0.974, 4) = 0.0014 — выше mark. Биржа отвечала
«StopLoss:140000 set for Buy position should lower than base_price:136400»
70 раз, позиция висела без стопа.
"""
import pytest

from bybit_ws import api as api_mod
from bybit_ws import unified_sl as usl

TICK = 0.000001


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(api_mod, 'get_tick_size', lambda sym: TICK)
    monkeypatch.setattr(usl, 'get_bb_data', lambda sym: {})
    usl._next_sl_update.clear()


def test_round_price_safe_rounds_away_from_market():
    # LONG: вниз, SHORT: вверх — тик 0.000001
    assert api_mod.round_price_safe('RVNUSDT', 0.0013637, True) == 0.001363
    assert api_mod.round_price_safe('RVNUSDT', 0.0013637, False) == 0.001364


def test_long_sl_stays_below_mark_on_cheap_coin(monkeypatch):
    sent = []
    monkeypatch.setattr(usl, 'place_stop_loss', lambda *a: sent.append(a) or True)

    # Кейс RVN: безубыток от entry даёт стоп выше рынка
    positions = {'RVNUSDT': {'symbol': 'RVNUSDT', 'side': 'Buy', 'size': 800000.0,
                             'entry': 0.0014, 'mark': 0.001364, 'stopLoss': None,
                             'positionIdx': 0, 'leverage': 5}}
    usl.manage_sl(positions, cycle=0)

    assert sent, 'SL вообще не отправлен — позиция осталась бы без стопа'
    sl = float(str(sent[0][4]))
    assert sl < 0.001364, f'SL {sl} не ниже mark 0.001364 — биржа отклонит'
    # не вплотную к рынку: зазор не меньше 2% (иначе выбьет шумом)
    assert sl <= 0.001364 * 0.98, f'SL {sl} слишком близко к mark — выбьет шумом'
    # шаг цены соблюдён
    assert abs(round(sl / TICK) * TICK - sl) < 1e-12


def test_short_sl_stays_above_mark_on_cheap_coin(monkeypatch):
    sent = []
    monkeypatch.setattr(usl, 'place_stop_loss', lambda *a: sent.append(a) or True)

    positions = {'RVNUSDT': {'symbol': 'RVNUSDT', 'side': 'Sell', 'size': 800000.0,
                             'entry': 0.0014, 'mark': 0.001364, 'stopLoss': None,
                             'positionIdx': 0, 'leverage': 5}}
    usl.manage_sl(positions, cycle=0)

    assert sent, 'SL не отправлен для шорта'
    sl = float(str(sent[0][4]))
    assert sl > 0.001364, f'SL {sl} не выше mark — биржа отклонит'
