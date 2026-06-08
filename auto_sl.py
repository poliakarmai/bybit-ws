"""Агрессивный авто-SL: каждые 2 минуты проверяет все позиции и ставит SL.

Не спрашивает, не ждёт. Позиция без SL = нарушение дисциплины.
SL ставится на -7% от текущей цены (Mark Price).
"""

from .api import bybit, fetch_positions
from .alerts import log_event

# Порог: сколько позиций без SL — алерт
NO_SL_ALERT_THRESHOLD = 1


def check_and_fix_sl():
    """Проверить все позиции, поставить SL тем у кого нет. Возвращает список алертов."""
    alerts = []
    positions = fetch_positions()
    if not positions:
        return alerts

    for sym, p in positions.items():
        if p.get('stopLoss') is not None:
            continue  # SL уже есть

        # Ставим SL на -7% от mark price
        mark = p['mark']
        side = p['side']
        idx = p['positionIdx']
        size = p['size']
        entry = p['entry']

        if side == 'Buy':
            sl_price = mark * 0.93
        else:
            sl_price = mark * 1.07

        sl_price = round(sl_price, 4)

        # Формируем запрос trading-stop
        sl_side = 'Sell' if side == 'Buy' else 'Buy'
        body = {
            'category': 'linear',
            'symbol': sym,
            'side': sl_side,
            'positionIdx': idx,
            'orderType': 'Market',
            'qty': str(size),
            'stopLoss': str(sl_price),
            'triggerBy': 'LastPrice',
            'slTriggerBy': 'MarkPrice',
        }

        data = bybit('POST', '/v5/position/trading-stop', body)
        if data and data.get('retCode') == 0:
            msg = f'🛡 Авто-SL {sym}: ${sl_price:.4f} (-7% от ${mark:.4f}, вход ${entry:.4f})'
            alerts.append(msg)
        else:
            err = data.get('retMsg', '?') if data else 'no response'
            msg = f'⚠️ Авто-SL {sym} НЕ встал: {err}'
            alerts.append(msg)

    return alerts
