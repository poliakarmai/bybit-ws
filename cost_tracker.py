"""Логгирование торговых комиссий в Hermes cost-tracking БД.

Вызывается из главного цикла bybit-ws раз в N тиков.
Дёргает Bybit API /v5/position/closed-pnl, дедуплицирует по orderId,
пишет новые записи в ~/.hermes/data/costs.db.
"""
import json, os, sqlite3, time
from datetime import datetime
from .api import bybit
from . import EVENTS_LOG
from .file_utils import safe_json_write

COSTS_DB = os.path.expanduser('~/.hermes/data/costs.db')
SEEN_FILE = os.path.join(os.path.dirname(EVENTS_LOG), 'cost_tracker_seen.json')
CHECK_INTERVAL_SEC = 3600  # раз в час
_last_check = 0.0


def _log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(EVENTS_LOG, 'a') as f:
        f.write(f'[{ts}] [cost_tracker] {msg}\n')


def _load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except:
        return set()


def _save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    safe_json_write(SEEN_FILE, list(seen))


def _insert_fee(order_id, symbol, side, fee_usd, fee_asset, pnl, created_time):
    """Вставить запись в trading_costs."""
    try:
        conn = sqlite3.connect(COSTS_DB)
        conn.execute(
            """INSERT INTO trading_costs (timestamp, symbol, side, fee_usd, fee_asset, pnl_usd, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (created_time, symbol, side, fee_usd, fee_asset, pnl,
             f'orderId={order_id[:8]} pnl=${pnl:+.2f}')
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        _log(f'DB error: {e}')
        return False


def fetch_and_log_closed_pnl():
    """Основная функция: забрать закрытые PnL, записать новые комиссии."""
    global _last_check

    now = time.time()
    if now - _last_check < CHECK_INTERVAL_SEC:
        return 0  # ещё не пора

    _last_check = now
    seen = _load_seen()
    new_count = 0

    # Забираем закрытые PnL за последние 7 дней (Bybit максимум)
    data = bybit('GET',
        '/v5/position/closed-pnl?category=linear&limit=50&startTime='
        + str(int((now - 86400 * 7) * 1000)))

    if not data or data.get('retCode') != 0:
        return 0

    for p in data['result'].get('list', []):
        order_id = p.get('orderId', '')
        if not order_id or order_id in seen:
            continue

        symbol = p.get('symbol', '')
        side = p.get('side', '')
        qty = float(p.get('qty', 0))
        pnl = float(p.get('closedPnl', 0))
        created_time = datetime.fromtimestamp(
            int(p.get('createdTime', 0)) / 1000
        ).strftime('%Y-%m-%d %H:%M:%S')

        # Комиссию API не отдаёт явно в closed-pnl, но PnL уже net of fees.
        # fee_usd — оцениваем через cumExecFee из trade history, если доступно
        cum_fee = float(p.get('openFee', 0)) + float(p.get('closeFee', 0))

        if _insert_fee(order_id, symbol, side, cum_fee, 'USDT', pnl, created_time):
            seen.add(order_id)
            new_count += 1

    if new_count > 0:
        _save_seen(seen)
        _log(f'Записано {new_count} новых комиссий')

    return new_count


def check_cycle():
    """Вызывается из главного цикла монитора каждый тик."""
    return fetch_and_log_closed_pnl()
