"""Тесты издержек в бэктест-движке: maker/taker + funding из funding_log (аудит #5).

Проверяем:
  - вход по maker (0.02%), выход по taker (0.055% + слиппедж 0.05%);
  - funding из funding_log попадает в pnl сделки (temp-БД, боевая не трогается);
  - total_fees / total_funding в метриках и модели издержек в манифесте.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_engine_fees.py -q
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '/home/openclaw')

from bybit_ws.experiments.engine import (  # noqa: E402
    build_manifest, compute_metrics, funding_in_window, load_funding, simulate,
)
from bybit_ws.paper_trade import MAKER_FEE, SLIPPAGE, TAKER_FEE, PaperExchange  # noqa: E402

HOUR_MS = 3_600_000
T0 = 1_700_000_000_000


def make_klines(n_bars: int = 80, spike_start: int = 65) -> list[dict]:
    """Синтетика: стабильно → крах → отскок (даёт реальные входы, как в test_vol_gate)."""
    klines: list[dict] = []
    for i in range(n_bars):
        ts = T0 + i * 60_000
        if i < spike_start:
            p, hi, lo = 100.0, 100.5, 99.7
            close = p
        elif i == spike_start:
            p, hi, lo, close = 100.0, 100.5, 78.0, 80.0
        elif i < spike_start + 5:
            p = 80.0 + (i - spike_start)
            hi, lo, close = p + 2.0, p - 1.0, p + 1.0
        else:
            p = 100.0 + (i - spike_start - 5) * 0.5
            hi, lo, close = p + 8.0, p - 6.0, p + 1.0
        klines.append({'open_time': ts, 'open': p, 'high': hi, 'low': lo,
                       'close': close, 'volume': 200_000, 'turnover': 200_000 * p})
    return klines


def _funding_db(path: Path, symbol: str, ts: int, cash_flow: float) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE funding_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          cash_flow REAL NOT NULL,
          fee_rate REAL,
          ts INTEGER NOT NULL,
          external_id TEXT UNIQUE
        );
    """)
    con.execute(
        'INSERT INTO funding_log (symbol, cash_flow, fee_rate, ts, external_id) '
        'VALUES (?, ?, ?, ?, ?)', (symbol, cash_flow, 0.0001, ts, 'ext-1'))
    con.commit()
    con.close()


def test_entry_is_maker_exit_is_taker():
    """Вход 0.02% (PostOnly), выход 0.055% + 0.05% слиппеджа."""
    assert MAKER_FEE < TAKER_FEE

    ex = PaperExchange('XUSDT', [], 1000.0)
    ex.open_long(100.0, 95.0, 110.0, 1.0, T0)
    assert abs(ex.balance - (1000.0 - 100.0 * MAKER_FEE)) < 1e-9

    ex._close_position(110.0, 'TP', T0 + 8 * HOUR_MS)
    t = ex.trades[0]
    expected_fee = 100.0 * MAKER_FEE + 110.0 * (TAKER_FEE + SLIPPAGE)
    assert abs(t['fee'] - expected_fee) < 1e-6
    assert t['funding'] == 0.0  # funding_fn не задан
    assert abs(t['pnl'] - (10.0 - 110.0 * (TAKER_FEE + SLIPPAGE))) < 0.01  # pnl округлён до центов


def test_funding_charged_from_log():
    """Funding из funding_log попадает в pnl сделки, чьё окно его накрывает."""
    klines = make_klines()
    sym = 'TESTUSDT'
    base_trades, _ = simulate(klines, sym, 10_000.0, {})
    assert base_trades, 'нужны сделки для проверки funding'
    base = base_trades[0]
    ts = ((base['entry_time'] + base['exit_time']) // 2) // 1000

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / 'state.db'
        _funding_db(db, sym, ts, -1.5)

        assert load_funding(sym, db) == [(ts, -1.5)]
        trades, _ = simulate(klines, sym, 10_000.0, {}, funding_db=db)

    assert trades[0]['funding'] == -1.5
    assert abs(trades[0]['pnl'] - (base['pnl'] - 1.5)) < 0.011
    # вне окна — ноль
    assert funding_in_window([(ts, -1.5)], ts * 1000 + 1_000, ts * 1000 + 2_000) == 0.0
    assert funding_in_window([(ts, -1.5)], ts * 1000 - 2_000, ts * 1000 + 1_000) == -1.5


def test_metrics_and_manifest_costs():
    """compute_metrics отдаёт total_fees/total_funding, манифест — модели издержек."""
    trades = [{'pnl': 5.0, 'pnl_pct': 1.0, 'fee': 0.3, 'funding': -0.2},
              {'pnl': -2.0, 'pnl_pct': -0.4, 'fee': 0.1, 'funding': 0.05}]
    m = compute_metrics(trades, [1000.0, 1005.0], 1000.0)
    assert abs(m['total_fees'] - 0.4) < 1e-9
    assert abs(m['total_funding'] - (-0.15)) < 1e-9

    man = build_manifest({}, {}, None, 'hash', 'ts')
    assert man['fees_model'] == 'maker_entry_taker_exit'
    assert man['funding_model'] == 'funding_log_sqlite'
