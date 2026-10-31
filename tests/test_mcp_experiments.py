"""Тесты для MCP experiment tools (backtest_run, experiment_list, validation_report)."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_server as _mcp  # type: ignore[import-not-found]  # noqa: E402


def _run(coro):
    """Запустить async-функцию синхронно."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Tool declarations ─────────────────────────────────────────────────

def test_backtest_run_tool_declaration():
    """Проверить, что backtest_run объявлен в tools."""
    tools = _run(_mcp.list_tools())
    backtest_tool = next((t for t in tools if t.name == "backtest_run"), None)

    assert backtest_tool is not None, "backtest_run не найден в списке инструментов"
    assert "symbol" in backtest_tool.inputSchema["properties"]
    assert "symbol" in backtest_tool.inputSchema["required"]
    assert "days" in backtest_tool.inputSchema["properties"]
    assert "interval" in backtest_tool.inputSchema["properties"]


def test_experiment_list_tool_declaration():
    """Проверить, что experiment_list объявлен в tools."""
    tools = _run(_mcp.list_tools())
    tool = next((t for t in tools if t.name == "experiment_list"), None)
    assert tool is not None
    assert "limit" in tool.inputSchema["properties"]


def test_validation_report_tool_declaration():
    """Проверить, что validation_report объявлен в tools."""
    tools = _run(_mcp.list_tools())
    tool = next((t for t in tools if t.name == "validation_report"), None)
    assert tool is not None
    assert "run_id" in tool.inputSchema["properties"]
    assert "run_id" in tool.inputSchema["required"]


# ── backtest_run ──────────────────────────────────────────────────────

def test_backtest_run_lock_prevents_concurrent():
    """Проверить, что lock предотвращает параллельный запуск."""
    _mcp._BACKTEST_LOCK.acquire()
    try:
        result = _run(_mcp.call_tool("backtest_run", {"symbol": "BTCUSDT"}))
        text = result[0].text
        assert "⏳" in text or "уже запущен" in text.lower()
    finally:
        _mcp._BACKTEST_LOCK.release()


def test_backtest_run_subprocess_timeout():
    """Проверить обработку timeout в backtest_run."""
    with patch.object(_mcp.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("cmd", 180)):
        result = _run(_mcp.call_tool("backtest_run", {"symbol": "BTCUSDT", "days": 30}))
        text = result[0].text
        assert "⏱" in text or "timeout" in text.lower()


def test_backtest_run_subprocess_error():
    """Проверить обработку ошибки subprocess в backtest_run."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Test error message"
    mock_result.stdout = ""

    with patch.object(_mcp.subprocess, "run", return_value=mock_result):
        result = _run(_mcp.call_tool("backtest_run", {"symbol": "BTCUSDT"}))
        text = result[0].text
        assert "❌" in text or "error" in text.lower()


def test_backtest_run_success():
    """Проверить успешный backtest_run."""
    artifact = {
        "metrics": {
            "n_trades": 15, "wins": 9, "losses": 6,
            "win_rate": 60.0, "total_pnl": 42.5,
            "total_pnl_pct": 8.5, "total_fees": 1.2,
        },
        "inputs_manifest": {"git_commit": "abc123def"},
        "equity_hash": "deadbeef12345678",
    }

    def fake_run(*a, **kw):
        # Записать artifact в --out файл
        cmd = a[0]
        out_idx = cmd.index("--out")
        out_path = cmd[out_idx + 1]
        with open(out_path, "w") as f:
            json.dump(artifact, f)
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch.object(_mcp.subprocess, "run", side_effect=fake_run):
        result = _run(_mcp.call_tool("backtest_run", {"symbol": "SOLUSDT", "days": 30}))
        text = result[0].text
        assert "✅" in text or "SOLUSDT" in text
        assert "15" in text         # n_trades
        assert "60.00" in text      # win_rate
        assert "abc123def" in text  # git_commit


# ── experiment_list ───────────────────────────────────────────────────

def _make_experiments_db(tmp_path: Path, rows=None) -> Path:
    """Создать временную experiments.db с опциональными данными."""
    db_path = tmp_path / "experiments.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE experiment (id INTEGER PRIMARY KEY, name TEXT, hypothesis TEXT)")
    conn.execute(
        "CREATE TABLE run (id INTEGER PRIMARY KEY, experiment_id INTEGER, "
        "metrics_json TEXT, created_at TEXT)")
    if rows:
        for exp, run in rows:
            conn.execute("INSERT INTO experiment VALUES (?, ?, ?)", exp)
            conn.execute("INSERT INTO run VALUES (?, ?, ?, ?)", run)
    conn.commit()
    conn.close()
    return db_path


def test_experiment_list_empty_db(tmp_path: Path):
    """Проверить experiment_list с пустой БД."""
    db = _make_experiments_db(tmp_path)
    with patch.object(_mcp, "EXPERIMENTS_DB", db):
        result = _run(_mcp.call_tool("experiment_list", {"limit": 10}))
        text = result[0].text
        assert "📋" in text
        assert "нет" in text.lower() or "пуст" in text.lower()


def test_experiment_list_with_data(tmp_path: Path):
    """Проверить experiment_list с данными."""
    db = _make_experiments_db(tmp_path, rows=[
        ((1, "test_exp", "test hypothesis"),
         (1, 1, '{"n_trades": 10, "win_rate": 60.0, "total_pnl": 100.5}', "2026-01-01")),
    ])
    with patch.object(_mcp, "EXPERIMENTS_DB", db):
        result = _run(_mcp.call_tool("experiment_list", {"limit": 10}))
        text = result[0].text
        assert "test_exp" in text
        assert "10" in text     # n_trades
        assert "60" in text     # win_rate


def test_experiment_list_missing_db(tmp_path: Path):
    """Проверить experiment_list с отсутствующей БД."""
    missing = tmp_path / "nonexistent.db"
    with patch.object(_mcp, "EXPERIMENTS_DB", missing):
        result = _run(_mcp.call_tool("experiment_list", {"limit": 10}))
        text = result[0].text
        assert "📋" in text
        assert "отсутствует" in text.lower() or "запусти" in text.lower()


# ── validation_report ─────────────────────────────────────────────────

def test_validation_report_missing_run(tmp_path: Path):
    """Проверить validation_report с отсутствующим run_id."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    db = _make_experiments_db(tmp_path)

    with patch.object(_mcp, "EXPERIMENTS_DIR", tmp_path), \
         patch.object(_mcp, "EXPERIMENTS_DB", db):
        result = _run(_mcp.call_tool("validation_report", {"run_id": 999}))
        text = result[0].text
        assert "❓" in text or "нет данных" in text.lower()


def test_validation_report_from_artifact(tmp_path: Path):
    """Проверить validation_report с artifact файлом."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    artifact = {
        "inputs_manifest": {"git_commit": "abc123"},
        "metrics": {
            "n_trades": 20, "wins": 12, "losses": 8,
            "win_rate": 60.0, "profit_factor": 1.8,
            "total_pnl": 150.0, "total_pnl_pct": 15.0,
            "max_drawdown_pct": 5.0, "sharpe_ratio": 1.5,
        },
        "verdict": "GO",
    }
    with open(runs_dir / "run_42.json", "w") as f:
        json.dump(artifact, f)

    with patch.object(_mcp, "EXPERIMENTS_DIR", tmp_path):
        result = _run(_mcp.call_tool("validation_report", {"run_id": 42}))
        text = result[0].text
        assert "run 42" in text.lower()
        assert "20" in text       # n_trades
        assert "60" in text       # win_rate
        assert "GO" in text       # verdict
