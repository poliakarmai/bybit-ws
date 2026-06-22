"""Запись сделок в trade_history для самообучения монитора."""
import sqlite3, time, os
from pathlib import Path

DB = Path(os.environ.get('BYBIT_DATA_DIR', os.path.expanduser('~/.local/share/bybit-ws'))) / 'state.db'

def record_trade(symbol, side, entry_price, exit_price, size, pnl, 
                 strategy='live', fees=0.0, entry_at=None, closed_at=None):
    """Записать закрытую сделку в trade_history."""
    now = int(time.time())
    try:
        db = sqlite3.connect(str(DB))
        db.execute("""
            INSERT INTO trade_history (symbol, side, strategy, entry_price, exit_price, 
                                       size, pnl, fees, entry_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, strategy, entry_price, exit_price, size, 
              pnl, fees, entry_at or now, closed_at or now))
        db.commit()
        db.close()
    except Exception:
        pass  # не ронять монитор из-за ошибки записи
