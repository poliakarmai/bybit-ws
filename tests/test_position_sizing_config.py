"""Проверка: параметры сайзинга читаются из конфига, а не из хардкод-фоллбеков.

Задача #17 аудита. Тесты без сети и без обращения к Bybit.
"""

import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

from bybit_ws import config as cfg_mod  # noqa: E402
from bybit_ws import position_sizing as ps  # noqa: E402

_KEYS = [
    'enabled',
    'long_risk_pct',
    'x10_risk_pct',
    'dca_risk_pct',
    'pump_risk_pct',
    'max_positions',
    'min_margin',
    'max_position_share',
]

# ключ в _DEFAULT_POSITION_SIZING -> константа-фоллбек в position_sizing.py
_FB_PAIRS = [
    ('long_risk_pct', '_FB_LONG_RISK'),
    ('x10_risk_pct', '_FB_X10_RISK'),
    ('dca_risk_pct', '_FB_DCA_RISK'),
    ('pump_risk_pct', '_FB_PUMP_RISK'),
    ('max_positions', '_FB_MAX_POSITIONS'),
    ('min_margin', '_FB_MIN_MARGIN'),
    ('max_position_share', '_FB_MAX_SHARE'),
    ('min_deposit', '_FB_MIN_DEPOSIT'),
]


def test_ps_cfg_nonempty_and_has_all_keys():
    """Секция position_sizing есть в дефолтах конфига и содержит все 8 ключей."""
    section = ps._ps_cfg()
    assert section, 'position_sizing пуста — секции нет в дефолтном конфиге'
    missing = [k for k in _KEYS if k not in section]
    assert not missing, f'в position_sizing нет ключей: {missing}'


def test_defaults_match_fallbacks():
    """_DEFAULT_POSITION_SIZING и _FB_* не должны разъезжаться."""
    defaults = cfg_mod._DEFAULT_POSITION_SIZING
    mismatches = []
    for key, fb_name in _FB_PAIRS:
        assert key in defaults, f'в _DEFAULT_POSITION_SIZING нет ключа {key}'
        expected = getattr(ps, fb_name)
        actual = defaults[key]
        if actual != expected:
            mismatches.append(f'{key}: default={actual!r} != {fb_name}={expected!r}')
    assert not mismatches, 'расхождение дефолтов и фоллбеков: ' + '; '.join(mismatches)


def test_p_returns_config_value_not_default():
    """_p() берёт значение из конфига, а не переданный фоллбек."""
    expected = cfg_mod._DEFAULT_POSITION_SIZING['max_positions']
    assert ps._p('max_positions', 999) == expected
    assert ps._p('max_positions', 999) != 999


def test_p_reads_monkeypatched_config(monkeypatch):
    """Подмена get_config меняет результат _p — чтение идёт из конфига."""

    class FakeCfg(dict):
        pass

    fake = FakeCfg(position_sizing={'max_positions': 7})
    monkeypatch.setattr(ps, 'get_config', lambda: fake)
    assert ps._p('max_positions', 999) == 7
