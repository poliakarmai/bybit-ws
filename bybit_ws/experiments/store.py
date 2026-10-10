"""ExperimentStore — SQLite-хранилище дерева экспериментов.

Отдельная БД `experiments.db` (НЕ боевой state.db). Обоснование выбора —
в docs/experiment-tree-design.md. Паттерн работы с SQLite повторяет state_db.py
(WAL + busy_timeout + executescript схемы).

Схема:
  experiment  — узел дерева (id, parent_id, params_json, hypothesis, status, created_at)
  run         — immutable прогон (id, experiment_id, kind, inputs_manifest,
                metrics_json, verdict, created_at)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

EXPERIMENTS_DIR = Path.home() / ".local" / "share" / "bybit-ws" / "experiments"
DEFAULT_DB = EXPERIMENTS_DIR / "experiments.db"
SANDBOX_DB = EXPERIMENTS_DIR / "sandbox.db"

# Допустимые kind прогонов (по ТЗ)
RUN_KINDS = ("backtest", "canary", "live")

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES experiment(id) ON DELETE SET NULL,
    name TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    hypothesis TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiment(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    inputs_manifest TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    verdict TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_experiment ON run(experiment_id);
CREATE INDEX IF NOT EXISTS idx_run_kind ON run(kind);
CREATE INDEX IF NOT EXISTS idx_experiment_parent ON experiment(parent_id);
"""


class ExperimentStore:
    """Thread-safe SQLite-хранилище дерева экспериментов (append-only runs)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    # ── experiment ─────────────────────────────────────────────

    def create_experiment(self, name: str, params: dict,
                          hypothesis: str | None = None,
                          parent_id: int | None = None) -> int:
        """Get-or-create узел experiment по уникальному имени.

        ВАЖНО: используется `ON CONFLICT(name) DO NOTHING`, а не `INSERT OR
        REPLACE`. REPLACE удаляет существующую строку, что с `ON DELETE CASCADE`
        на run.experiment_id молча стирает всю историю прогонов эксперимента.
        Здесь параметры фиксируются при первом создании, а повторные прогоны
        дописывают новые run-строки под тем же experiment id (immutable-семантика).
        """
        self.conn.execute(
            "INSERT INTO experiment "
            "(name, parent_id, params_json, hypothesis, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, parent_id, json.dumps(params, ensure_ascii=False),
             hypothesis, int(time.time())),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM experiment WHERE name=?", (name,)).fetchone()
        return int(row[0])

    def get_experiment(self, name: str | None = None,
                       experiment_id: int | None = None) -> dict | None:
        if name is not None:
            row = self.conn.execute(
                "SELECT * FROM experiment WHERE name=?", (name,)).fetchone()
        elif experiment_id is not None:
            row = self.conn.execute(
                "SELECT * FROM experiment WHERE id=?", (experiment_id,)).fetchone()
        else:
            raise ValueError("нужен name или experiment_id")
        if not row:
            return None
        cols = [d[1] for d in self.conn.execute("PRAGMA table_info(experiment)")]
        d = dict(zip(cols, row))
        d["params"] = json.loads(d["params_json"])
        return d

    def set_status(self, experiment_id: int, status: str):
        self.conn.execute(
            "UPDATE experiment SET status=? WHERE id=?", (status, experiment_id))
        self.conn.commit()

    # ── run ────────────────────────────────────────────────────

    def add_run(self, experiment_id: int, kind: str,
                inputs_manifest: dict, metrics: dict,
                verdict: str | None = None) -> int:
        if kind not in RUN_KINDS:
            raise ValueError(f"kind={kind!r} не из {RUN_KINDS}")
        self.conn.execute(
            "INSERT INTO run (experiment_id, kind, inputs_manifest, "
            "metrics_json, verdict, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (experiment_id, kind,
             json.dumps(inputs_manifest, ensure_ascii=False),
             json.dumps(metrics, ensure_ascii=False),
             verdict, int(time.time())),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def list_runs(self, experiment_id: int | None = None,
                  limit: int = 100) -> list[dict]:
        q = "SELECT * FROM run"
        params: list = []
        if experiment_id is not None:
            q += " WHERE experiment_id=?"
            params.append(experiment_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        cols = [d[1] for d in self.conn.execute("PRAGMA table_info(run)")]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["inputs_manifest"] = json.loads(d["inputs_manifest"])
            d["metrics"] = json.loads(d["metrics_json"])
            out.append(d)
        return out

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
