"""TASK I: гигиена фандинг-ротации (reason, дедуп по кулдауну, стабильность).

Проверяем 4 дефекта гигиены `funding_rotation.check_funding_rotation`:
- Дефект 1: пустой `()` в алерте — в словаре действия не было ключа `reason`.
- Дефект 2: спам одинаковым алертом — `add_alert` дедупа не имеет, поэтому
  дедуп делается на стороне ротации (`ALERT_COOLDOWN`, `last_alert_ts`).
- Дефект 5 (регресс): пороги/скоринг/BB-бонус не тронуты.

Живых API нет: `_get_funding_map`, `_get_bb_signal`, `is_manual_position`
мокаются, `ROTATION_STATE_FILE` подменяется на tmp-путь.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_funding_rotation_hygiene.py -v
"""
import json
import os
import sys
import time

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402

from bybit_ws import funding_rotation as fr  # noqa: E402


@pytest.fixture
def rot(tmp_path, monkeypatch):
    """Изолированная ротация: tmp state-файл + мок позиций/кандидатов."""
    state_file = tmp_path / 'funding_rotation_state.json'
    monkeypatch.setattr(fr, 'ROTATION_STATE_FILE', str(state_file))
    monkeypatch.setattr(fr, 'is_manual_position', lambda sym: False)
    # BB всегда «хороший лонг-сигнал» (bb_pct < 20), цена фиксирована
    monkeypatch.setattr(
        fr, '_get_bb_signal',
        lambda sym: {'bb_pct': 10.0, 'lower': 0.9, 'upper': 1.1, 'price': 1.0},
    )
    return fr


POS = {'XVGUSDT': {'size': 100.0, 'entry': 1.0, 'side': 'Buy', 'positionIdx': 0}}


def _write_state(state_file, last_alert_ts):
    with open(state_file, 'w') as f:
        json.dump({'cooldowns': {}, 'daily_count': 0, 'last_reset': '',
                   'last_alert_ts': last_alert_ts}, f)


def test_reason_is_nonempty_and_has_both_rates(rot, monkeypatch):
    """Дефект 1: reason есть, непустой и содержит обе ставки."""
    monkeypatch.setattr(fr, '_get_funding_map',
                        lambda: {'XVGUSDT': 0.001, 'NEARUSDT': -0.001})
    actions = rot.check_funding_rotation(POS)

    assert len(actions) == 1
    a = actions[0]
    assert 'reason' in a
    assert isinstance(a['reason'], str) and a['reason'].strip()
    # обе ставки в процентном виде (0.1% и -0.1%) присутствуют
    assert '0.1%' in a['reason']
    assert '-0.1%' in a['reason']
    assert a['reason'] == 'фандинг 0.1% → -0.1% (Δ0.2%), BB 10.0%'


def test_second_call_within_cooldown_returns_nothing(rot, monkeypatch):
    """Дефект 2: два подряд вызова на одном состоянии → второй отдаёт 0."""
    monkeypatch.setattr(fr, '_get_funding_map',
                        lambda: {'XVGUSDT': 0.001, 'NEARUSDT': -0.001})

    first = rot.check_funding_rotation(POS)
    assert len(first) == 1, 'первый вызов должен предложить ротацию'

    second = rot.check_funding_rotation(POS)
    assert second == [], 'внутри ALERT_COOLDOWN повторный алерт не отдаём'

    # состояние зафиксировало пару с timestamp
    with open(rot.ROTATION_STATE_FILE) as f:
        saved = json.load(f)
    assert 'XVGUSDT→NEARUSDT' in saved['last_alert_ts']


def test_after_cooldown_expires_returns_again(rot, monkeypatch):
    """Дефект 2: со сдвинутым last_alert_ts (−2×кулдаун) действие снова отдаётся."""
    monkeypatch.setattr(fr, '_get_funding_map',
                        lambda: {'XVGUSDT': 0.001, 'NEARUSDT': -0.001})
    stale_ts = time.time() - 2 * fr.ALERT_COOLDOWN
    _write_state(rot.ROTATION_STATE_FILE, {'XVGUSDT→NEARUSDT': stale_ts})

    actions = rot.check_funding_rotation(POS)
    assert len(actions) == 1
    assert actions[0]['to'] == 'NEARUSDT'


def test_pair_change_alerts_immediately(rot, monkeypatch):
    """Дефект 2: другая пара отдаётся сразу, несмотря на свежий last_alert_ts."""
    # свежий timestamp у ПЕРВОЙ пары (XVGUSDT→NEARUSDT) не должен глушить вторую
    _write_state(rot.ROTATION_STATE_FILE, {'XVGUSDT→NEARUSDT': time.time()})
    # единственный валидный кандидат — ALGOUSDT (NEAR отсутствует в fmap)
    monkeypatch.setattr(fr, '_get_funding_map',
                        lambda: {'XVGUSDT': 0.001, 'ALGOUSDT': -0.001})

    actions = rot.check_funding_rotation(POS)
    assert len(actions) == 1
    assert actions[0]['to'] == 'ALGOUSDT'
    assert actions[0]['from'] == 'XVGUSDT'


def test_thresholds_and_scoring_unchanged(rot, monkeypatch):
    """Дефект 5: ровно одно действие с ожидаемыми from/to/delta.

    Пороги (FUNDING_DELTA_MIN), скоринг и BB-бонус не тронуты: delta =
    0.001 − (−0.001) = 0.002 → 0.2% в процентах.
    """
    monkeypatch.setattr(fr, '_get_funding_map',
                        lambda: {'XVGUSDT': 0.001, 'NEARUSDT': -0.001})

    actions = rot.check_funding_rotation(POS)
    assert len(actions) == 1
    a = actions[0]
    assert a['from'] == 'XVGUSDT'
    assert a['to'] == 'NEARUSDT'
    assert a['side'] == 'Buy'
    assert a['delta'] == 0.2
    assert a['current_funding'] == 0.1
    assert a['new_funding'] == -0.1
    assert a['bb_pct'] == 10.0
