"""StateDB — SQLite-хранилище для bybit-ws.

Замена 15 JSON-файлов на одну транзакционную базу.
WAL-режим: читатели не блокируют писателей.

Схема:
  trade_history   — аудит сделок (PnL, комиссии, стратегия)
  positions       — кэш открытых позиций
  short_state     — состояние автошорта
  pump_state      — трекинг пампов
  x10_limits      — дневной лимит x10 убытков
  x10_positions   — трекинг x10 позиций
  cooldowns        — кулдауны (SL/TP/входы)
  alert_dedup      — дедупликация алертов

Использование:
    from .state_db import db
    db.save_short_state(sym, {...})
    data = db.get_short_state(sym)
"""

import os
import sqlite3
import json
import time
from datetime import datetime

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
DB_PATH = os.path.join(DATA_DIR, 'state.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    fees REAL DEFAULT 0,
    entry_at INTEGER,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trade_symbol ON trade_history(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_closed ON trade_history(closed_at);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    side TEXT,
    entry REAL,
    mark REAL,
    size REAL,
    leverage REAL,
    stop_loss REAL,
    take_profit REAL,
    position_idx INTEGER DEFAULT 0,
    upnl REAL DEFAULT 0,
    liq_price REAL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS short_state (
    symbol TEXT PRIMARY KEY,
    last_short_ts INTEGER,
    entry_price REAL,
    qty REAL,
    bb_pct REAL,
    is_junk INTEGER DEFAULT 0,
    dca_level INTEGER DEFAULT 0,
    state_json TEXT
);

CREATE TABLE IF NOT EXISTS pump_state (
    symbol TEXT PRIMARY KEY,
    first_seen_ts INTEGER,
    peak_price REAL,
    alerts_json TEXT,
    daily_pump INTEGER DEFAULT 0,
    weekly_pump INTEGER DEFAULT 0,
    short_entry_ts INTEGER,
    manual INTEGER DEFAULT 0,
    state_json TEXT
);

CREATE TABLE IF NOT EXISTS x10_limits (
    date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    losses INTEGER DEFAULT 0,
    pnl REAL DEFAULT 0,
    stopped_at INTEGER,
    PRIMARY KEY (date, strategy)
);

CREATE TABLE IF NOT EXISTS x10_positions (
    symbol TEXT PRIMARY KEY,
    strategy TEXT,
    entry_price REAL,
    size REAL,
    opened_at INTEGER,
    state_json TEXT
);

CREATE TABLE IF NOT EXISTS cooldowns (
    key TEXT PRIMARY KEY,
    until INTEGER
);

CREATE TABLE IF NOT EXISTS alert_dedup (
    key TEXT PRIMARY KEY,
    last_at INTEGER
);

-- Config/state table for key-value storage
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class StateDB:
    """Thread-safe SQLite database for bybit-ws state."""

    def __init__(self, path=None):
        self.path = path or DB_PATH
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = None

    @property
    def conn(self):
        """Ленивое подключение (thread-local не нужно — bybit-ws однопоточный)."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    # ── trade_history ─────────────────────────────────────────

    def _cols(self, table):
        """Получить имена колонок таблицы."""
        return [d[1] for d in self.conn.execute(f"PRAGMA table_info({table})")]

    def _row_dict(self, row, table):
        """Преобразовать строку в dict {col: value}."""
        cols = self._cols(table)
        return dict(zip(cols, row))

    def add_trade(self, symbol, side, strategy, entry_price, exit_price,
                  size, pnl, fees=0, entry_at=None, closed_at=None):
        self.conn.execute("""INSERT INTO trade_history
            (symbol, side, strategy, entry_price, exit_price, size, pnl, fees, entry_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, strategy, entry_price, exit_price, size, pnl,
             fees, entry_at or int(time.time()), closed_at or int(time.time())))
        self.conn.commit()

    def get_trades(self, symbol=None, since=None, limit=100):
        q = "SELECT * FROM trade_history WHERE 1=1"
        params = []
        if symbol:
            q += " AND symbol=?"
            params.append(symbol)
        if since:
            q += " AND closed_at > ?"
            params.append(since)
        q += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(q, params).fetchall()
        return [self._row_dict(r, "trade_history") for r in rows]

    def get_pnl_summary(self, since=None):
        """Суммарный PnL за период."""
        q = "SELECT SUM(pnl) as total_pnl, SUM(fees) as total_fees, COUNT(*) as trades FROM trade_history"
        params = []
        if since:
            q += " WHERE closed_at > ?"
            params.append(since)
        row = self.conn.execute(q, params).fetchone()
        return {'total_pnl': row[0] or 0, 'total_fees': row[1] or 0, 'trades': row[2] or 0}

    # ── positions ──────────────────────────────────────────────

    def save_positions(self, positions_dict):
        """Сохранить снепшот позиций (замена positions.json)."""
        now = int(time.time())
        with self.conn:
            self.conn.execute("DELETE FROM positions")
            for sym, p in positions_dict.items():
                self.conn.execute("""INSERT OR REPLACE INTO positions
                    (symbol, side, entry, mark, size, leverage, stop_loss, take_profit,
                     position_idx, upnl, liq_price, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sym, p.get('side'), p.get('entry'), p.get('mark'), p.get('size'),
                     p.get('leverage'), p.get('stopLoss'), p.get('takeProfit'),
                     p.get('positionIdx', 0), p.get('upnl', 0), p.get('liqPrice'), now))

    def get_positions(self):
        """Получить все открытые позиции."""
        rows = self.conn.execute("SELECT * FROM positions").fetchall()
        return {r[0]: self._row_dict(r, "positions") for r in rows}

    # ── short_state ────────────────────────────────────────────

    def save_short_state(self, symbol, data):
        """Сохранить состояние автошорта (замена short_positions.json)."""
        self.conn.execute("""INSERT OR REPLACE INTO short_state
            (symbol, last_short_ts, entry_price, qty, bb_pct, is_junk, dca_level, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, data.get('last_short_ts'), data.get('entry_price'), data.get('qty'),
             data.get('bb_pct'), int(data.get('is_junk', False)), data.get('dca_level', 0),
             json.dumps(data)))
        self.conn.commit()

    def get_short_state(self, symbol=None):
        """Получить состояние автошорта для символа или все."""
        if symbol:
            row = self.conn.execute("SELECT * FROM short_state WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                return None
            return self._row_dict(row, "short_state")
        rows = self.conn.execute("SELECT * FROM short_state").fetchall()
        return {r[0]: self._row_dict(r, "short_state") for r in rows}

    def get_all_short_state(self):
        """Получить все short_state как dict symbol→data (совместимость со старым API)."""
        result = {}
        for sym, data in self.get_short_state().items():
            result[sym] = data
        return result

    # ── pump_state ─────────────────────────────────────────────

    def save_pump_state(self, symbol, data):
        """Сохранить состояние пампа (замена pumps.json)."""
        self.conn.execute("""INSERT OR REPLACE INTO pump_state
            (symbol, first_seen_ts, peak_price, alerts_json, daily_pump, weekly_pump,
             short_entry_ts, manual, state_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, data.get('first_seen_ts'), data.get('peak_price'),
             json.dumps(data.get('alerts', [])),
             int(data.get('daily_pump', False)), int(data.get('weekly_pump', False)),
             data.get('short_entry_ts'), int(data.get('manual', False)),
             json.dumps(data)))
        self.conn.commit()

    def get_pump_state(self, symbol=None):
        """Получить pump_state для символа или все."""
        if symbol:
            row = self.conn.execute("SELECT * FROM pump_state WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                return {}
            data = self._row_dict(row, "pump_state")
            data['alerts'] = json.loads(data.get('alerts_json', '[]'))
            data['daily_pump'] = bool(data.get('daily_pump'))
            data['weekly_pump'] = bool(data.get('weekly_pump'))
            data['manual'] = bool(data.get('manual'))
            return {k: v for k, v in data.items() if v is not None}
        rows = self.conn.execute("SELECT * FROM pump_state").fetchall()
        result = {}
        for r in rows:
            data = self._row_dict(r, "pump_state")
            data['alerts'] = json.loads(data.get('alerts_json', '[]'))
            data['daily_pump'] = bool(data.get('daily_pump'))
            data['weekly_pump'] = bool(data.get('weekly_pump'))
            data['manual'] = bool(data.get('manual'))
            result[r[0]] = {k: v for k, v in data.items() if v is not None}
        return result

    def get_all_pump_state(self):
        """dict symbol→data (совместимость)."""
        return self.get_pump_state()

    # ── x10 ────────────────────────────────────────────────────

    def save_x10_limits(self, date, strategy, data):
        self.conn.execute("""INSERT OR REPLACE INTO x10_limits (date, strategy, losses, pnl, stopped_at)
            VALUES (?, ?, ?, ?, ?)""",
            (date, strategy, data.get('losses', 0), data.get('pnl', 0), data.get('stopped_at')))
        self.conn.commit()

    def get_x10_limits(self):
        rows = self.conn.execute("SELECT * FROM x10_limits").fetchall()
        result = {}
        for r in rows:
            d = self._row_dict(r, "x10_limits")
            key = f"{d['date']}:{d['strategy']}"
            result[key] = d
        return result

    def save_x10_position(self, symbol, data):
        self.conn.execute("""INSERT OR REPLACE INTO x10_positions
            (symbol, strategy, entry_price, size, opened_at, state_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, data.get('strategy'), data.get('entry_price'), data.get('size'),
             data.get('opened_at', int(time.time())), json.dumps(data)))
        self.conn.commit()

    def get_x10_positions(self):
        rows = self.conn.execute("SELECT * FROM x10_positions").fetchall()
        return {r[0]: self._row_dict(r, "x10_positions") for r in rows}

    def remove_x10_position(self, symbol):
        self.conn.execute("DELETE FROM x10_positions WHERE symbol=?", (symbol,))
        self.conn.commit()

    # ── cooldowns ──────────────────────────────────────────────

    def set_cooldown(self, key, seconds):
        """Установить кулдаун на N секунд."""
        until = int(time.time()) + seconds
        self.conn.execute("INSERT OR REPLACE INTO cooldowns (key, until) VALUES (?, ?)", (key, until))
        self.conn.commit()

    def is_cooling_down(self, key):
        """Проверить, активен ли кулдаун."""
        row = self.conn.execute("SELECT until FROM cooldowns WHERE key=?", (key,)).fetchone()
        if not row:
            return False
        return time.time() < row[0]

    def get_cooldown_remaining(self, key):
        """Оставшееся время кулдауна (сек), 0 если не активен."""
        row = self.conn.execute("SELECT until FROM cooldowns WHERE key=?", (key,)).fetchone()
        if not row:
            return 0
        return max(0, row[0] - int(time.time()))

    def clear_cooldown(self, key):
        self.conn.execute("DELETE FROM cooldowns WHERE key=?", (key,))
        self.conn.commit()

    def clean_expired_cooldowns(self):
        """Удалить истекшие кулдауны."""
        now = int(time.time())
        self.conn.execute("DELETE FROM cooldowns WHERE until < ?", (now,))
        self.conn.commit()

    # ── alert_dedup ────────────────────────────────────────────

    def should_alert(self, key, cooldown_seconds):
        """Проверить, можно ли отправить алерт. True = можно, False = дубликат."""
        row = self.conn.execute("SELECT last_at FROM alert_dedup WHERE key=?", (key,)).fetchone()
        now = int(time.time())
        if row and (now - row[0]) < cooldown_seconds:
            return False
        self.conn.execute("INSERT OR REPLACE INTO alert_dedup (key, last_at) VALUES (?, ?)", (key, now))
        self.conn.commit()
        return True

    def clean_old_alerts(self, max_age=86400):
        """Удалить старые записи дедупликации (>24ч)."""
        cutoff = int(time.time()) - max_age
        self.conn.execute("DELETE FROM alert_dedup WHERE last_at < ?", (cutoff,))
        self.conn.commit()

    # ── kv_store ───────────────────────────────────────────────

    def set_kv(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                          (key, json.dumps(value) if not isinstance(value, str) else value))
        self.conn.commit()

    def get_kv(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    # ── maintenance ────────────────────────────────────────────

    def vacuum(self):
        """Оптимизация БД."""
        self.conn.execute("VACUUM")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# Глобальный экземпляр
db = StateDB()


# ═══════════════════════════════════════════════════════════
# Async StateDB (Фаза 4.7 — asyncio-миграция)
# ═══════════════════════════════════════════════════════════

import asyncio

class AsyncStateDB:
    """Async-обёртка над SQLite через aiosqlite."""

    def __init__(self):
        self._db = None
        self._lock = asyncio.Lock()

    async def _ensure_db(self):
        if self._db is None:
            import aiosqlite
            os.makedirs(DATA_DIR, exist_ok=True)
            self._db = await aiosqlite.connect(DB_PATH)
            await self._db.execute('PRAGMA journal_mode=WAL')
            await self._db.execute('PRAGMA busy_timeout=5000')
            await self._db.executescript(SCHEMA)
        return self._db

    async def get_positions(self):
        db = await self._ensure_db()
        db.row_factory = aiosqlite.Row
        rows = await db.execute('SELECT * FROM positions')
        return [dict(r) for r in await rows.fetchall()]

    async def update_position(self, symbol, data):
        db = await self._ensure_db()
        await db.execute('''
            INSERT OR REPLACE INTO positions (symbol, side, entry, mark, size, leverage,
                stop_loss, take_profit, position_idx, upnl, liq_price, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            symbol, data.get('side'), data.get('entry'), data.get('mark'),
            data.get('size'), data.get('leverage'), data.get('stopLoss'),
            data.get('takeProfit'), data.get('positionIdx', 0),
            data.get('upnl'), data.get('liqPrice'), int(time.time())
        ))
        await db.commit()

    async def log_trade(self, symbol, side, strategy, entry_price, exit_price, size, pnl, fees=0):
        db = await self._ensure_db()
        now = int(time.time())
        await db.execute('''
            INSERT INTO trade_history (symbol, side, strategy, entry_price, exit_price, size, pnl, fees, entry_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        ''', (symbol, side, strategy, entry_price, exit_price, size, pnl, fees, now))
        await db.commit()

    async def get_metrics(self, date=None):
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        db = await self._ensure_db()
        # Агрегация
        rows = await db.execute('''
            SELECT side, COUNT(*) as count, SUM(pnl) as total_pnl
            FROM trade_history WHERE date(closed_at, 'unixepoch') = ?
            GROUP BY side
        ''', (date,))
        result = {'tp_real': 0, 'sl_real': 0, 'entry': 0, 'tp_list': [], 'sl_list': []}
        for r in await rows.fetchall():
            side, count, pnl = r
            if side == 'Buy':
                result['entry'] = count
            elif pnl and pnl > 0:
                result['tp_real'] += count
            else:
                result['sl_real'] += count
        # Детализация: какие монеты TP / SL
        trades = await db.execute('''
            SELECT symbol, pnl FROM trade_history
            WHERE date(closed_at, 'unixepoch') = ?
            ORDER BY closed_at DESC
        ''', (date,))
        tp_list = []
        sl_list = []
        async for t in trades:
            symbol, pnl = t
            if pnl > 0:
                tp_list.append({'symbol': symbol, 'pnl': round(pnl, 4)})
            else:
                sl_list.append({'symbol': symbol, 'pnl': round(pnl, 4)})
        result['tp_list'] = tp_list
        result['sl_list'] = sl_list
        return result

    async def should_alert(self, key, cooldown_sec):
        db = await self._ensure_db()
        now = int(time.time())
        row = await db.execute('SELECT last_at FROM alert_dedup WHERE key = ?', (key,))
        existing = await row.fetchone()
        if existing and (now - existing[0]) < cooldown_sec:
            return False
        await db.execute('INSERT OR REPLACE INTO alert_dedup (key, last_at) VALUES (?, ?)', (key, now))
        await db.commit()
        return True

    async def set_kv(self, key, value):
        db = await self._ensure_db()
        await db.execute('INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)', (key, str(value)))
        await db.commit()

    async def get_kv(self, key, default=None):
        db = await self._ensure_db()
        row = await db.execute('SELECT value FROM kv_store WHERE key = ?', (key,))
        result = await row.fetchone()
        return result[0] if result else default

    async def vacuum(self):
        db = await self._ensure_db()
        await db.execute('VACUUM')

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None


# Глобальный async-экземпляр
adb = AsyncStateDB()
