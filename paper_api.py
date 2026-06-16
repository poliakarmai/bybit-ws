"""Paper Trading API — симуляция биржи для бэктестинга и paper-trading.

Использование:
    from .paper_api import PaperExchange
    px = PaperExchange()
    positions = px.fetch_positions()  # возвращает мок-позиции
    px.place_order('BTCUSDT', 'Buy', 'Market', 0.01)  # симулирует ордер
"""
import json
import os
import time
import sqlite3
import math
import random
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
PAPER_DB = DATA_DIR / "paper_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    mark REAL NOT NULL,
    size REAL NOT NULL,
    leverage REAL DEFAULT 3,
    stop_loss REAL,
    take_profit REAL,
    position_idx INTEGER DEFAULT 0,
    upnl REAL DEFAULT 0,
    liq_price REAL,
    open_time INTEGER,
    margin REAL DEFAULT 0,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price REAL,
    qty REAL NOT NULL,
    status TEXT DEFAULT 'New',
    reduce_only INTEGER DEFAULT 0,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    fees REAL DEFAULT 0,
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS paper_balance (
    currency TEXT PRIMARY KEY,
    amount REAL DEFAULT 0
);
"""


class PaperExchange:
    """Симуляция биржи — тот же интерфейс что api.py, но без реальных денег."""

    def __init__(self, db_path=None, slippage_pct=0.05):
        self.db_path = str(db_path or PAPER_DB)
        self.slippage_pct = slippage_pct  # 0.05% по умолчанию
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = None
        self._init_db()
        self._seed_balance()

    @property
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _init_db(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _seed_balance(self):
        row = self.conn.execute("SELECT amount FROM paper_balance WHERE currency='USDT'").fetchone()
        if not row:
            self.conn.execute("INSERT INTO paper_balance (currency, amount) VALUES ('USDT', 10000)")
            self.conn.commit()

    def _simulate_slippage(self, price, side):
        """Добавить проскальзывание: Buy → чуть дороже, Sell → чуть дешевле."""
        slip = price * (self.slippage_pct / 100) * random.uniform(0.3, 1.0)
        if side == 'Buy':
            return price + slip
        return price - slip

    def _tick_size(self, price):
        if price < 1: return 0.0001
        if price < 10: return 0.001
        if price < 100: return 0.01
        if price < 1000: return 0.1
        return 1.0

    def _round_price(self, price):
        tick = self._tick_size(price)
        return round(round(price / tick) * tick, 8)

    # ── API-совместимые методы ──

    def bybit(self, method, path, body=None):
        """Заглушка: все вызовы API игнорируются в paper-режиме."""
        return {'retCode': 0, 'retMsg': 'OK (paper)', 'result': {}}

    def fetch_positions(self):
        """Получить все открытые paper-позиции."""
        rows = self.conn.execute("SELECT * FROM paper_positions").fetchall()
        positions = {}
        for r in rows:
            cols = [d[1] for d in self.conn.execute("PRAGMA table_info(paper_positions)")]
            p = dict(zip(cols, r))
            positions[p['symbol']] = {
                'size': p['size'],
                'entry': p['entry'],
                'mark': p['mark'],
                'upnl': p['upnl'],
                'side': p['side'],
                'stopLoss': p['stop_loss'],
                'positionIdx': p['position_idx'],
                'liqPrice': p.get('liq_price'),
                'leverage': p['leverage'],
                'positionIM': p.get('margin', 0),
                'cumRealisedPnl': 0,
                'openTime': p.get('open_time', 0),
                'margin': p.get('margin', 0),
            }
        return positions

    def fetch_orders(self):
        """Получить все активные paper-ордера."""
        rows = self.conn.execute(
            "SELECT * FROM paper_orders WHERE status IN ('New', 'PartiallyFilled', 'Untriggered')"
        ).fetchall()
        orders = {}
        for r in rows:
            cols = [d[1] for d in self.conn.execute("PRAGMA table_info(paper_orders)")]
            o = dict(zip(cols, r))
            key = f"{o['symbol']}_{o['order_id']}"
            # Определяем kind
            if o['reduce_only'] and o['order_type'] == 'Market':
                kind = 'SL'
            elif o['reduce_only'] and o['order_type'] == 'Limit':
                kind = 'TP'
            elif o['side'] == 'Buy':
                kind = 'LIMIT_ENTRY'
            else:
                kind = 'OTHER'
            orders[key] = {
                'symbol': o['symbol'],
                'orderId': o['order_id'],
                'status': o['status'],
                'kind': kind,
                'price': o['price'] or 0,
                'trigger': 0,
                'qty': o['qty'],
                'side': o['side'],
                'createdTime': str(o.get('created_at', '')),
                'cumExecQty': 0,
            }
        return orders

    def place_order(self, symbol, side, order_type, qty, price=None, reduce_only=False, position_idx=0):
        """Создать ордер в paper-режиме."""
        order_id = f"paper_{int(time.time()*1000)}_{random.randint(1000,9999)}"
        self.conn.execute(
            """INSERT INTO paper_orders (order_id, symbol, side, order_type, price, qty, status, reduce_only, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'New', ?, ?)""",
            (order_id, symbol, side, order_type, price, qty, int(reduce_only), int(time.time()))
        )
        self.conn.commit()

        # Симуляция исполнения Market-ордеров
        if order_type == 'Market':
            self._execute_market_order(symbol, side, qty, price or 0, reduce_only, position_idx)

        return {'retCode': 0, 'result': {'orderId': order_id}}

    def _execute_market_order(self, symbol, side, qty, price, reduce_only, position_idx):
        """Симулировать исполнение market-ордера."""
        fill_price = self._simulate_slippage(price or 100.0, side)
        fill_price = self._round_price(fill_price)

        if reduce_only:
            # Закрытие позиции
            row = self.conn.execute(
                "SELECT * FROM paper_positions WHERE symbol=? AND side!=?",
                (symbol, side)
            ).fetchone()
            if row:
                cols = [d[1] for d in self.conn.execute("PRAGMA table_info(paper_positions)")]
                pos = dict(zip(cols, row))
                close_size = min(qty, pos['size'])
                if pos['side'] == 'Buy':
                    pnl = close_size * (fill_price - pos['entry'])
                else:
                    pnl = close_size * (pos['entry'] - fill_price)
                fees = fill_price * close_size * 0.0006  # 0.06% taker fee

                # Записать трейд
                self.conn.execute(
                    "INSERT INTO paper_trades (symbol, side, entry_price, exit_price, size, pnl, fees, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, pos['side'], pos['entry'], fill_price, close_size, pnl, fees, int(time.time()))
                )

                # Обновить баланс
                self.conn.execute(
                    "UPDATE paper_balance SET amount = amount + ? WHERE currency='USDT'",
                    (pnl - fees,)
                )

                # Уменьшить или удалить позицию
                remaining = pos['size'] - close_size
                if remaining <= 0:
                    self.conn.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
                else:
                    self.conn.execute(
                        "UPDATE paper_positions SET size=?, mark=?, upnl=?, updated_at=? WHERE symbol=?",
                        (remaining, fill_price, 0, int(time.time()), symbol)
                    )
        else:
            # Открытие позиции
            existing = self.conn.execute(
                "SELECT * FROM paper_positions WHERE symbol=? AND side=?",
                (symbol, side)
            ).fetchone()

            if existing:
                # Добавка к существующей
                cols = [d[1] for d in self.conn.execute("PRAGMA table_info(paper_positions)")]
                pos = dict(zip(cols, existing))
                new_size = pos['size'] + qty
                new_entry = (pos['entry'] * pos['size'] + fill_price * qty) / new_size
                self.conn.execute(
                    "UPDATE paper_positions SET size=?, entry=?, mark=?, updated_at=? WHERE symbol=? AND side=?",
                    (new_size, new_entry, fill_price, int(time.time()), symbol, side)
                )
            else:
                # Новая позиция
                leverage = 3
                margin = (fill_price * qty) / leverage
                liq_price = fill_price * 0.9 if side == 'Buy' else fill_price * 1.1
                self.conn.execute(
                    """INSERT INTO paper_positions
                       (symbol, side, entry, mark, size, leverage, position_idx, upnl, liq_price, open_time, margin, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (symbol, side, fill_price, fill_price, qty, leverage, position_idx,
                     liq_price, int(time.time()), margin, int(time.time()))
                )

        self.conn.commit()

    def place_stop_loss(self, symbol, position_idx, side, qty, stop_price):
        """Обновить SL в paper-режиме."""
        self.conn.execute(
            "UPDATE paper_positions SET stop_loss=?, updated_at=? WHERE symbol=?",
            (stop_price, int(time.time()), symbol)
        )
        self.conn.commit()
        return True

    def place_take_profit(self, symbol, position_idx, side, qty, tp_price):
        """Поставить TP в paper-режиме."""
        self.place_order(symbol, 'Sell' if side == 'Buy' else 'Buy', 'Limit', qty, tp_price, reduce_only=True)
        self.conn.execute(
            "UPDATE paper_positions SET take_profit=?, updated_at=? WHERE symbol=?",
            (tp_price, int(time.time()), symbol)
        )
        self.conn.commit()
        return True

    def cancel_order(self, symbol, order_id):
        """Отменить ордер."""
        self.conn.execute("DELETE FROM paper_orders WHERE order_id=?", (order_id,))
        self.conn.commit()
        return True

    # ── Утилиты ──

    def get_balance(self):
        row = self.conn.execute("SELECT amount FROM paper_balance WHERE currency='USDT'").fetchone()
        return row[0] if row else 10000.0

    def get_pnl_summary(self):
        pnl_row = self.conn.execute("SELECT SUM(pnl), SUM(fees), COUNT(*) FROM paper_trades").fetchone()
        return {
            'total_pnl': pnl_row[0] or 0,
            'total_fees': pnl_row[1] or 0,
            'trades': pnl_row[2] or 0,
        }

    def update_mark_prices(self, price_map):
        """Обновить mark-цены и пересчитать unrealized PnL."""
        for sym, mark in price_map.items():
            row = self.conn.execute("SELECT * FROM paper_positions WHERE symbol=?", (sym,)).fetchone()
            if row:
                cols = [d[1] for d in self.conn.execute("PRAGMA table_info(paper_positions)")]
                pos = dict(zip(cols, row))
                if pos['side'] == 'Buy':
                    upnl = pos['size'] * (mark - pos['entry'])
                else:
                    upnl = pos['size'] * (pos['entry'] - mark)
                self.conn.execute(
                    "UPDATE paper_positions SET mark=?, upnl=?, updated_at=? WHERE symbol=?",
                    (mark, upnl, int(time.time()), sym)
                )
        self.conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
