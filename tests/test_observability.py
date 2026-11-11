"""Наблюдаемость (01.11.2026): счётчики LLM-судьи и снимок входа в trade_history.

Проверяем две вещи, обе — машинным гейтом, без живого API:
1. judge_stats() реально двигается на pass / error / timeout / открытом брейкере.
2. trade_history получает колонки score/regime/funding/confluence и хранит их.

Только временные БД. Боевой state.db не читается и не пишется.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_observability.py -q
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, '/home/openclaw')

import bybit_ws.entry_judge as ej
from bybit_ws.state_db import StateDB

# ── 1. Снимок входа в trade_history ──────────────────────────────────────

def test_entry_snapshot_columns_exist_and_store():
    with tempfile.TemporaryDirectory() as d:
        db = StateDB(os.path.join(d, 'state.db'))
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(trade_history)")]
        for c in ('score', 'regime', 'funding', 'confluence'):
            assert c in cols, f'колонка {c} не создана миграцией'

        tid = db.add_trade(
            symbol='QUSDT', side='Buy', strategy='auto',
            entry_price=1.0, exit_price=0.9, size=10.0, pnl=-1.0,
            score=42, regime='TRENDING_DOWN', funding=-0.00031, confluence=2,
        )
        row = db.conn.execute(
            "SELECT score, regime, funding, confluence FROM trade_history WHERE id=?",
            (tid,)).fetchone()
        assert row == (42, 'TRENDING_DOWN', -0.00031, 2), row


# ── 2. Счётчики судьи ────────────────────────────────────────────────────

def _reset(monkeypatch, script_path):
    for k in ej._stats:
        ej._stats[k] = 0 if k != 'last_latency_ms' else 0.0
    monkeypatch.setattr(ej, 'JUDGE_ENABLED', True)
    monkeypatch.setattr(ej, 'JUDGE_MIN_SCORE', 0)
    monkeypatch.setattr(ej, 'JUDGE_SCRIPT', script_path)
    monkeypatch.setattr(ej, '_judge_verdict_cache', {})
    monkeypatch.setattr(ej, '_llm_disabled_until', 0.0)


def _call(symbol='BTCUSDT'):
    return ej.judge_entry(symbol, 'Buy', score=30, bb_pos=10.0, entry_price=100.0)


def test_stats_pass_and_error_and_timeout(monkeypatch, tmp_path):
    ok = tmp_path / 'ok.py'
    ok.write_text('import json, sys\n'
                  'print(json.dumps({"verdict": "pass", "blocking_issues": [], "confidence": 0.9}))\n')
    _reset(monkeypatch, str(ok))
    assert _call()['verdict'] == 'pass'
    s = ej.judge_stats()
    assert s['calls'] == 1 and s['pass'] == 1 and s['revise'] == 0
    assert s['last_latency_ms'] > 0, 'задержка не замерена'

    bad = tmp_path / 'bad.py'
    bad.write_text('import sys\nsys.exit(1)\n')
    _reset(monkeypatch, str(bad))
    assert _call()['verdict'] == 'revise'      # fail-closed
    s = ej.judge_stats()
    assert s['errors'] == 1 and s['revise'] == 0, s

    slow = tmp_path / 'slow.py'
    slow.write_text('import time\ntime.sleep(2)\n')
    _reset(monkeypatch, str(slow))
    monkeypatch.setattr(ej, 'JUDGE_TIMEOUT', 0.2)
    assert _call()['verdict'] == 'revise'
    s = ej.judge_stats()
    assert s['timeouts'] == 1 and s['errors'] == 0, s


def test_stats_breaker_and_fallback(monkeypatch, tmp_path):
    ok = tmp_path / 'ok.py'
    ok.write_text('import json\nprint(json.dumps({"verdict": "pass"}))\n')
    _reset(monkeypatch, str(ok))

    monkeypatch.setattr(ej, '_llm_disabled_until', time.time() + 100)  # брейкер открыт
    assert _call()['verdict'] == 'revise'
    assert ej.judge_stats()['breaker_hits'] == 1
    assert ej.judge_stats()['fallback_open'] == 0

    monkeypatch.setattr(ej, '_llm_disabled_until', time.time() + 7200)  # длинный аутедж
    assert _call()['verdict'] == 'pass'
    s = ej.judge_stats()
    assert s['breaker_hits'] == 2 and s['fallback_open'] == 1, s


def test_metrics_endpoint_exposes_judge_series():
    """/metrics обязан печатать серии судьи — иначе счётчики некуда читать."""
    import inspect

    from bybit_ws import rpc
    src = inspect.getsource(rpc)
    for series in ('bybit_ws_judge_calls',
                   'bybit_ws_judge_verdicts_total',
                   'bybit_ws_judge_errors_total',
                   'bybit_ws_judge_breaker_hits_total',
                   'bybit_ws_judge_last_latency_ms'):
        assert series in src, f'серия {series} не отдаётся в /metrics'
    assert 'judge_stats' in src, 'metrics не читает счётчики судьи'
