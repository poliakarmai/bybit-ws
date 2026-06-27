"""
Time-Based Exit — закрытие позиций застрявших без движения (27.06.2026).

Позиция, которая не дала прибыли за 4-6 часов — мёртвый груз (маржа, funding).
Закрывает в рынок, освобождает слоты для свежих сигналов.
Особенно важно при лимите 12 позиций.
"""
import time
from typing import Optional

# Конфиг
TIME_EXIT_HOURS = 6  # максимум часов в позиции без движения
TIME_EXIT_MIN_PNL = 0.0  # минимальный PnL для «живой» позиции (в процентах от входа)
TIME_EXIT_ENABLED = True


def _position_age_hours(entry_ts: float) -> float:
    """Возраст позиции в часах."""
    return (time.time() - entry_ts) / 3600


def check_time_exit(positions: dict, open_orders: dict = None) -> list:
    """Проверить позиции на «застой» — закрыть если висят без движения.

    Триггеры:
    1. Позиция > TIME_EXIT_HOURS часов открыта
    2. PnL < TIME_EXIT_MIN_PNL (не пошла в плюс)
    3. Нет частичного TP (значит даже до middle BB не дошла)

    Returns:
        list of (symbol, reason) для закрытых/запланированных позиций
    """
    if not TIME_EXIT_ENABLED:
        return []

    exits = []
    now = time.time()

    for sym, p in positions.items():
        if not isinstance(p, dict):
            continue

        # Проверяем возраст позиции
        opened_at = p.get('opened_at') or p.get('entry_ts')
        if not opened_at:
            continue

        try:
            opened_ts = float(opened_at)
        except (ValueError, TypeError):
            continue

        age_hours = (now - opened_ts) / 3600
        if age_hours < TIME_EXIT_HOURS:
            continue

        # Проверяем PnL
        entry = float(p.get('entry', 0))
        mark = float(p.get('mark', 0))
        size = float(p.get('size', 0))

        if entry <= 0 or size <= 0:
            continue

        pnl_pct = (mark - entry) / entry * 100
        if pnl_pct > TIME_EXIT_MIN_PNL:
            continue  # позиция в плюсе — пусть живёт

        # Проверяем был ли частичный TP
        has_tp_fill = False
        if open_orders:
            for o in (open_orders.values() if isinstance(open_orders, dict) else open_orders):
                if isinstance(o, dict) and o.get('symbol') == sym:
                    if o.get('kind') == 'TP' and o.get('status') == 'PartiallyFilled':
                        has_tp_fill = True
                        break

        if has_tp_fill:
            continue  # был частичный TP — позиция двигалась

        reason = (
            f"TIME EXIT {sym}: {age_hours:.0f}h в позиции, "
            f"PnL={pnl_pct:+.1f}%, нет движения"
        )
        exits.append((sym, reason, size, p.get('positionIdx', 0), p.get('side', 'Buy')))

    return exits


def apply_time_exits(exits: list) -> dict:
    """Закрыть позиции по рынку.

    Returns:
        {'closed': int, 'failed': int}
    """
    if not exits:
        return {'closed': 0, 'failed': 0}

    from .api import bybit
    from .alerts import log_event

    result = {'closed': 0, 'failed': 0}

    for sym, reason, size, idx, side in exits:
        close_side = 'Sell' if side == 'Buy' else 'Buy'
        log_event(f'⏰ {reason}')

        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': sym,
                'side': close_side,
                'orderType': 'Market',
                'qty': str(size),
                'positionIdx': idx,
                'reduceOnly': True,
                'timeInForce': 'IOC',
            })
            if order and order.get('retCode') == 0:
                result['closed'] += 1
                log_event(f'⏰ TIME EXIT {sym}: closed {size} @ MARKET')
            else:
                result['failed'] += 1
                err = order.get('retMsg', '?') if order else 'no response'
                log_event(f'⏰ TIME EXIT {sym} FAILED: {err}')
        except Exception as e:
            result['failed'] += 1
            log_event(f'⏰ TIME EXIT {sym} ERROR: {e}')

    return result
