"""Артефакт бэктеста (roadmap #11): манифест, отпечатки, equity-кривая, воспроизводимость.

Критерий готовности из дорожной карты: отчёт содержит commit SHA, хеш конфига,
отпечаток данных, комиссии/фандинг и equity-кривую, и два прогона с одинаковыми
входами дают одинаковый результат. Сеть и кэш свечей подменяются синтетикой.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bybit_ws.experiments import backtest_runner, engine  # noqa: E402

REQUIRED_MANIFEST = {"engine_version", "params", "params_hash", "data_range", "git_commit",
                     "timestamp", "data_hash", "config_hash", "fees_model", "funding_model"}


def _klines(n=160, start_ms=1_700_000_000_000) -> list[dict]:
    """Детерминированная пила: даёт и перегревы, и провалы — BB-сигналы без сети."""
    out = []
    day = 86_400_000
    price = 100.0
    for i in range(n):
        drift = 1.5 if (i // 8) % 2 == 0 else -1.5
        price = max(5.0, price + drift)
        out.append({
            "open_time": start_ms + i * day,
            "open": price, "high": price * 1.01, "low": price * 0.99,
            "close": price, "volume": 1000.0 + i, "turnover": (1000.0 + i) * price,
        })
    return out


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(engine, "fetch_klines", lambda *a, **k: _klines())


def test_manifest_has_all_fingerprints():
    res = backtest_runner.run_one("TESTUSDT", 30, "D", "2026-10-01",
                                  dict(engine.DEFAULT_PARAMS), 1000.0)
    man = res["manifest"]
    assert REQUIRED_MANIFEST <= set(man), f"нет полей: {REQUIRED_MANIFEST - set(man)}"
    assert len(man["data_hash"]) == len(man["params_hash"]) == 64
    assert man["config_hash"] is None or len(man["config_hash"]) == 64
    assert res["metrics"]["total_fees"] is not None
    assert res["metrics"]["total_funding"] is not None


def test_two_runs_are_reproducible():
    p = dict(engine.DEFAULT_PARAMS)
    a = backtest_runner.run_one("TESTUSDT", 30, "D", "2026-10-01", p, 1000.0)
    b = backtest_runner.run_one("TESTUSDT", 30, "D", "2026-10-01", p, 1000.0)
    assert a["equity_hash"] == b["equity_hash"]
    assert a["metrics"] == b["metrics"]
    for key in ("data_hash", "params_hash", "config_hash", "git_commit", "engine_version"):
        assert a["manifest"][key] == b["manifest"][key], key


def test_params_change_changes_fingerprint():
    base = backtest_runner.run_one("TESTUSDT", 30, "D", "2026-10-01",
                                   dict(engine.DEFAULT_PARAMS), 1000.0)
    other = dict(engine.DEFAULT_PARAMS, rr_ratio=3.0)
    changed = backtest_runner.run_one("TESTUSDT", 30, "D", "2026-10-01", other, 1000.0)
    assert base["manifest"]["params_hash"] != changed["manifest"]["params_hash"]
    assert base["manifest"]["data_hash"] == changed["manifest"]["data_hash"]


def test_cli_writes_artifact(tmp_path):
    out = tmp_path / "run.json"
    rc = backtest_runner.main(["--symbol", "TESTUSDT", "--days", "30", "--end-date", "2026-10-01",
                               "--no-db", "--out", str(out)])
    assert rc == 0
    rec = json.loads(out.read_text())
    assert REQUIRED_MANIFEST <= set(rec["inputs_manifest"])
    assert rec["equity_hash"] and len(rec["equity_curve"]) > 1
    assert {"n_trades", "win_rate", "profit_factor", "max_drawdown_pct",
            "total_fees", "total_funding"} <= set(rec["metrics"])
