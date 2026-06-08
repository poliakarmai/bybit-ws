"""Реинвест прибыли от частичного TP в новые лимитки на Lower BB.

Правило: TP 20% сработал → прибыль реинвестируется в лимитку на ту же монету.
Только на сумму прибыли, без дополнительного капитала.
"""

import time
from .api import bybit, get_bb_data

# Защита от дубликатов: {symbol: последний_ts_реинвеста}
_last_recycle = {}

def handle_tp_recycle(reduced_symbols, positions, orders):
    """
    Вызывается из main_loop при обнаружении REDUCE (частичный TP).
    Ставит лимитку на Lower BB на сумму полученной прибыли.
    
    Args:
        reduced_symbols: set символов с REDUCE
        positions: текущие позиции {sym: {...}}
        orders: текущие ордера {key: {...}}
    """
    now = time.time()
    actions = []
    
    for sym in reduced_symbols:
        # Защита от повторных срабатываний: не чаще раза в 30 мин
        if sym in _last_recycle and now - _last_recycle[sym] < 1800:
            continue
            
        pos = positions.get(sym, {})
        if pos.get('side') != 'Buy' or pos.get('size', 0) <= 0:
            continue
            
        # Проверяем что есть активные лимитки на докупку (не спамим)
        has_entry_order = any(
            o.get('kind') == 'LIMIT_ENTRY' and o.get('symbol') == sym
            for o in orders.values()
        )
        if has_entry_order:
            continue
        
        entry_price = pos.get('entry', 0)
        if entry_price <= 0:
            continue
            
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
            
        lower = bb['lower']
        cur = bb['cur']
        
        # Только если цена близка к Lower BB (< 30%)
        bb_pos = (cur - lower) / (bb['upper'] - lower) * 100 if bb['upper'] != lower else 50
        if bb_pos > 30:
            continue
            
        # Прибыль с последнего TP оцениваем через upnl позиции
        # upnl = (mark - entry) × size — это НЕреализованная прибыль оставшейся части
        # Но нам нужна реализованная прибыль от TP. 
        # Используем подход: 20% позиции продано по Middle BB
        # Прибыль ≈ (Middle - entry) × (size × 0.2 / 0.8) ≈ (Middle - entry) × size × 0.25
        middle = bb['middle']
        tp_profit_per_unit = middle - entry_price
        if tp_profit_per_unit <= 0:
            continue
        
        # Размер закрытой части: позиция уменьшилась на ~20% → оставшиеся 80% = current size
        # Значит закрыто было current_size / 0.8 × 0.2 = current_size × 0.25
        closed_qty = pos['size'] * 0.25
        realised_profit = tp_profit_per_unit * closed_qty
        
        if realised_profit <= 0.5:  # Минимум $0.50 прибыли
            continue
        
        # Новая лимитка: qty = прибыль × плечо / цена_входа
        # Вход на 5% ниже Lower BB
        entry_target = lower * 0.95
        leverage = 3
        new_qty = round(realised_profit * leverage / entry_target)
        
        if new_qty < 1:
            continue
        
        # Проверяем что цена входа выше текущей (лимитка ниже рынка)
        if entry_target >= cur:
            entry_target = round(cur * 0.97, 4)  # -3% от текущей
        
        actions.append((sym, pos.get('positionIdx', 1), 'Buy', new_qty, entry_target, realised_profit))
        _last_recycle[sym] = now
    
    return actions


def apply_recycle(actions):
    """Исполнить recycle-лимитки."""
    for sym, idx, side, qty, price, profit in actions:
        body = {
            'category': 'linear',
            'symbol': sym,
            'side': side,
            'orderType': 'Limit',
            'qty': str(qty),
            'price': str(price),
            'timeInForce': 'GTC',
            'positionIdx': idx,
            'orderLinkId': f'{sym.lower()}_recycle_{int(time.time())}'
        }
        data = bybit('POST', '/v5/order/create', body)
        if data and data.get('retCode') == 0:
            from .alerts import log_event
            log_event(f'♻️ Recycle {sym}: {qty} шт @ ${price:.4f} (из прибыли ${profit:.2f})')
            return True
    return False
