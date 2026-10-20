"""LONG не блокируется режимом рынка (20.10.2026).

Регресс: TRENDING_DOWN с conf=41% держал LONG=OFF — прибыльная сторона
стояла (WR 83%/PF 1.38), работала убыточная SHORT (PF 0.76).
Боковик/чоп по-прежнему закрывают ОБЕ стороны — это не LONG-гейт.

Флаг BYBIT_REGIME_AUTO читается на импорте модуля, поэтому тесты выставляют
REGIME_AUTO_ENABLED сами — иначе результат зависит от окружения CI.
"""
import pytest

from bybit_ws import lstm_regime as L


@pytest.fixture(autouse=True)
def _regime_auto_on(monkeypatch):
    monkeypatch.setattr(L, 'REGIME_AUTO_ENABLED', True)


def test_long_never_blocked_by_trend():
    for regime in ('TRENDING_UP', 'TRENDING_DOWN', 'HIGH_VOL', 'LOW_VOL', 'NEUTRAL', 'UNKNOWN'):
        assert L.get_regime_strategy(regime)['LONG_ENABLED'] is True, regime


def test_short_still_blocked_in_uptrend():
    assert L.get_regime_strategy('TRENDING_UP')['SHORT_ENABLED'] is False


def test_chop_and_range_still_block_both():
    for regime in ('RANGING', 'CHOPPY'):
        s = L.get_regime_strategy(regime)
        assert s['LONG_ENABLED'] is False and s['SHORT_ENABLED'] is False, regime


def test_flag_off_disables_gate():
    """Флаг выключен → гейт не блокирует ничего."""
    L.REGIME_AUTO_ENABLED = False
    try:
        assert L.get_regime_strategy('TRENDING_UP') == {'LONG_ENABLED': True, 'SHORT_ENABLED': True}
    finally:
        L.REGIME_AUTO_ENABLED = True


if __name__ == '__main__':
    L.REGIME_AUTO_ENABLED = True  # вне pytest фикстура не работает — ставим флаг сами
    for f in (test_long_never_blocked_by_trend, test_short_still_blocked_in_uptrend,
              test_chop_and_range_still_block_both, test_flag_off_disables_gate):
        f()
    print('OK: 4/4')
