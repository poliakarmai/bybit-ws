"""Инференс LSTM не должен подавать нулевые строки признаков (20.10.2026).

Регресс: days=SEQUENCE_LENGTH+5 (35 баров) давал 14 из 30 шагов = нули,
т.к. _calc_features требует n>=20. Модель нулей при обучении не видела
(build_dataset такие окна отбрасывает) → половина входа вне распределения.
"""
import inspect

from bybit_ws.lstm_regime import SEQUENCE_LENGTH, _calc_features, _fetch_recent_klines


def _bars(n):
    """Синтетические бары — без сети."""
    closes = [100.0 + (i % 7) for i in range(n)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    volumes = [1000.0 + i for i in range(n)]
    return closes, highs, lows, volumes


def test_default_window_has_no_zero_padding():
    days = inspect.signature(_fetch_recent_klines).parameters['days'].default
    assert days >= SEQUENCE_LENGTH + 19, f'days={days} — окно слишком короткое'

    closes, highs, lows, volumes = _bars(days)
    n = len(closes)
    zeros = sum(
        1
        for j in range(n - SEQUENCE_LENGTH, n)
        if _calc_features(closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1]) is None
    )
    assert zeros == 0, f'{zeros}/{SEQUENCE_LENGTH} шагов последовательности пустые'


def test_short_window_would_pad_zeros():
    """Контроль: при старом days=35 нули действительно были — тест на них ловит."""
    closes, highs, lows, volumes = _bars(SEQUENCE_LENGTH + 5)
    n = len(closes)
    zeros = sum(
        1
        for j in range(n - SEQUENCE_LENGTH, n)
        if _calc_features(closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1]) is None
    )
    assert zeros > 0
