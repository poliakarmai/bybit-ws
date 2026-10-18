"""Регресс 18.10.2026: min_/max_ из lstm_world_scaler.json — списки (JSON-раундтрип),
а `world_pred` код считал `max_ - min_` → TypeError: unsupported operand type(s)
for -: 'list' and 'list'. Весь world-model SHORT-скоринг молча падал в wm_score=0.

Проверяем только приведение на входе (без сети и без torch).
Запуск: cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_world_model_scaler.py -q
"""
import sys

import numpy as np

sys.path.insert(0, '/home/openclaw')

from bybit_ws import lstm_world_model as wm  # noqa: E402


def test_scaler_arrays_from_json_lists():
    d = wm._scaler_arrays({'min_': [0.0] * 13, 'max_': [1.0] * 13, 'extra': 'ignored'})
    assert isinstance(d['min_'], np.ndarray)
    assert isinstance(d['max_'], np.ndarray)
    # именно та операция, что падала: list - list → ndarray - ndarray
    assert (d['max_'] - d['min_'] + 1e-8).shape == (13,)
    assert 'extra' not in d


if __name__ == '__main__':
    test_scaler_arrays_from_json_lists()
    print('✅ scaler min_/max_ → ndarray')
