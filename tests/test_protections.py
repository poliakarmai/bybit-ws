"""Замки на входы: серия стопов и пауза пары после стопа.

Проверяем правила на подставленных сделках (БД не нужна) и что гейт в
`margin_for_strategy` реально возвращает 0 при замке.
"""

import sys
import time
from pathlib import Path

import pytest

import bybit_ws.position_sizing as ps  # noqa: E402
import bybit_ws.protections as pr  # noqa: E402

CFG = dict(pr._DEFAULTS)


def _t(sym, pnl, hours_ago, reason='SL', now=None):
    now = time.time() if now is None else now
    return {'symbol': sym, 'pnl': pnl, 'closed_at': now - hours_ago * 3600,
            'exit_reason': reason}


def test_stop_series_locks_all():
    now = time.time()
    trades = [_t('AUSDT', -5, h, now=now) for h in (1, 2, 3, 4, 5, 6)]
    locks = pr.compute_locks(trades, CFG, now)
    assert locks['global'] and locks['global']['until'] > now
    assert '6 стопов' in locks['global']['reason']


def test_stop_series_below_limit_no_global():
    now = time.time()
    trades = [_t('AUSDT', -5, h, now=now) for h in (1, 2, 3, 4, 5)]
    assert pr.compute_locks(trades, CFG, now)['global'] is None


def test_old_stops_outside_window_ignored():
    now = time.time()
    trades = [_t('AUSDT', -5, 20, now=now) for _ in range(10)]  # 20ч назад
    assert pr.compute_locks(trades, CFG, now)['global'] is None


def test_pnl_positive_exits_do_not_count():
    now = time.time()
    trades = [_t('AUSDT', 3, h, reason='TP', now=now) for h in (1, 2, 3, 4, 5, 6, 7)]
    locks = pr.compute_locks(trades, CFG, now)
    assert locks['global'] is None and not locks['pairs']


def test_pair_paused_after_its_stop():
    now = time.time()
    trades = [_t('SOLUSDT', -5, 1, now=now)]
    locks = pr.compute_locks(trades, CFG, now)
    assert 'SOLUSDT' in locks['pairs']
    assert locks['pairs']['SOLUSDT']['until'] == pytest.approx(now + 4 * 3600 - 3600, abs=5)


def test_pair_cooldown_expires():
    now = time.time()
    trades = [_t('SOLUSDT', -5, 5, now=now)]  # стоп 5ч назад, пауза 4ч → истекла
    assert 'SOLUSDT' not in pr.compute_locks(trades, CFG, now)['pairs']


def test_disabled_returns_nothing():
    now = time.time()
    trades = [_t('AUSDT', -5, h, now=now) for h in (1, 2, 3, 4, 5, 6, 7)]
    assert pr.compute_locks(trades, dict(CFG, enabled=False), now) == {'global': None, 'pairs': {}}


def test_lock_reason_reads_state():
    now = time.time()
    state = {'global': {'until': now + 600, 'reason': '6 стопов за 12ч'},
             'pairs': {'SOLUSDT': {'until': now + 600, 'reason': 'пауза после стопа'}}}
    assert 'глобальный замок' in pr.lock_reason('XRPUSDT', state=state)
    assert 'замок пары' in pr.lock_reason('SOLUSDT', state={'global': None, 'pairs': state['pairs']})
    assert pr.lock_reason('BTCUSDT', state={'global': None, 'pairs': {}}) is None


def test_expired_state_does_not_lock():
    now = time.time()
    state = {'global': {'until': now - 1, 'reason': 'старое'}, 'pairs': {}}
    assert pr.lock_reason('SOLUSDT', state=state) is None


def test_margin_gate_returns_zero_when_locked(monkeypatch, tmp_path):
    """Гейт читает файл состояния (BYBIT_PROTECTIONS_STATE) — кладём туда живой
    замок и проверяем, что маржа не считается. Через файл, а не monkeypatch:
    в прогоне pytest модуль может быть импортирован заново другим объектом."""
    import json
    state_file = tmp_path / "protections.json"
    state_file.write_text(json.dumps({
        'global': {'until': time.time() + 600, 'reason': '6 стопов за 12ч'},
        'pairs': {}}))
    monkeypatch.setenv('BYBIT_PROTECTIONS_STATE', str(state_file))
    assert ps.margin_for_strategy('long', score=7.0, symbol='SOLUSDT') == 0
