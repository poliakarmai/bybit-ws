"""Порог доверия гейта режима (20.10.2026).

Регресс: conf=41% (5 классов, базовая линия 20%) жёстко выключал сторону
22 дня подряд — 5841 замер. Ниже порога гейт не должен применяться вообще.
"""
from bybit_ws import lstm_regime as L

BLOCKED = {'LONG_ENABLED': False, 'SHORT_ENABLED': True}
NEUTRAL = {'LONG_ENABLED': True, 'SHORT_ENABLED': True}


def test_below_threshold_gate_is_neutral():
    assert L.apply_confidence_gate(BLOCKED, L.REGIME_MIN_CONFIDENCE - 1) == NEUTRAL


def test_at_threshold_gate_applies():
    assert L.apply_confidence_gate(BLOCKED, L.REGIME_MIN_CONFIDENCE) == BLOCKED


def test_real_world_reading_is_neutral():
    """41% — фактическое чтение модели: гейт не срабатывает."""
    assert L.apply_confidence_gate(BLOCKED, 41) == NEUTRAL
    assert L.apply_confidence_gate(BLOCKED, 42) == NEUTRAL


def test_threshold_is_env_tunable():
    assert isinstance(L.REGIME_MIN_CONFIDENCE, int)
    assert 0 < L.REGIME_MIN_CONFIDENCE <= 100


if __name__ == '__main__':
    for f in (test_below_threshold_gate_is_neutral, test_at_threshold_gate_applies,
              test_real_world_reading_is_neutral, test_threshold_is_env_tunable):
        f()
    print('OK: 4/4')
