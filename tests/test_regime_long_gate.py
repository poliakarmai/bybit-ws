"""LONG не блокируется режимом рынка (20.10.2026).

Регресс: TRENDING_DOWN с conf=41% держал LONG=OFF — прибыльная сторона
стояла (WR 83%/PF 1.38), работала убыточная SHORT (PF 0.76).
Боковик/чоп по-прежнему закрывают ОБЕ стороны — это не LONG-гейт.
"""
from bybit_ws.lstm_regime import get_regime_strategy


def test_long_never_blocked_by_trend():
    for regime in ('TRENDING_UP', 'TRENDING_DOWN', 'HIGH_VOL', 'LOW_VOL', 'NEUTRAL', 'UNKNOWN'):
        assert get_regime_strategy(regime)['LONG_ENABLED'] is True, regime


def test_short_still_blocked_in_uptrend():
    assert get_regime_strategy('TRENDING_UP')['SHORT_ENABLED'] is False


def test_chop_and_range_still_block_both():
    for regime in ('RANGING', 'CHOPPY'):
        s = get_regime_strategy(regime)
        assert s['LONG_ENABLED'] is False and s['SHORT_ENABLED'] is False, regime


if __name__ == '__main__':
    for f in (test_long_never_blocked_by_trend, test_short_still_blocked_in_uptrend,
              test_chop_and_range_still_block_both):
        f()
    print('OK: 3/3')
