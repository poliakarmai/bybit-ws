"""Защита ручных позиций от автоматического вмешательства монитора.

Когда мы входим в обход авто-входа (ручной market/limit через bybit raw),
монитор не знает что позиция «наша» и может:
- переставить SL (auto_sl)
- воткнуть свой TP (auto_tp)
- закрыть при первом профите (instant_tp)
- орать «не по стратегии» (strategy_compliance)

Механизм: pumps.json с флагом "manual": true.
Все опасные модули проверяют is_manual_position() перед вмешательством.
"""

import os
import json
import time
from .alerts import log_event

PUMP_STATE_FILE = os.path.join(os.path.expanduser('~/.local/share/bybit-ws'), 'pumps.json')


def _load_state():
    """Загрузить pumps.json (безопасно)."""
    if os.path.exists(PUMP_STATE_FILE):
        try:
            with open(PUMP_STATE_FILE) as f:
                return json.loads(f.read())
        except Exception as e:
            log_event(f'⚠️ manual_positions: ошибка чтения pumps.json: {e}')
    return {}


def _save_state(state):
    """Сохранить pumps.json атомарно."""
    tmp = PUMP_STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, PUMP_STATE_FILE)


def is_manual_position(sym):
    """Проверить, помечена ли позиция как ручная."""
    state = _load_state()
    entry = state.get(sym, {})
    return entry.get('manual', False)


def mark_manual_position(sym, entry=None, side='Buy', note='', leverage=None, auto_sltp=False):
    """Пометить позицию как ручную — монитор её не трогает (кроме auto_sltp).

    Args:
        sym: символ (например 'NEARUSDT')
        entry: цена входа (опционально, для истории)
        side: 'Buy' или 'Sell'
        note: заметка (например 'x10 wide-SL')
        leverage: плечо (опционально)
        auto_sltp: True → SL/TP бот ставит сам (по стратегии), но self-learn исключает
    """
    state = _load_state()
    entry_data = state.get(sym, {})
    entry_data['manual'] = True
    entry_data['manual_ts'] = time.time()
    entry_data['auto_sltp'] = bool(auto_sltp)
    if entry is not None:
        entry_data['manual_entry'] = entry
    if side:
        entry_data['manual_side'] = side
    if note:
        entry_data['manual_note'] = note
    if leverage is not None:
        entry_data['manual_leverage'] = leverage
    state[sym] = entry_data
    _save_state(state)
    # логируем причину ручного входа → trade_history.entry_reason (через entry_diag)
    if note:
        try:
            from .state_db import db
            db.save_entry_diagnostic(sym, side, entry_reason=note)
        except Exception as e:
            log_event(f'⚠️ mark_manual_position entry_reason: {e}')


def is_auto_sltp(sym):
    """True если ручная позиция помечена auto_sltp — бот ставит SL/TP сам."""
    state = _load_state()
    return state.get(sym, {}).get('auto_sltp', False)


def unmark_manual_position(sym):
    """Снять ручную метку (позиция закрылась или передана авто)."""
    state = _load_state()
    if sym in state:
        state[sym]['manual'] = False
        state[sym].pop('manual_ts', None)
        state[sym].pop('manual_entry', None)
        state[sym].pop('manual_side', None)
        state[sym].pop('manual_note', None)
        state[sym].pop('manual_leverage', None)
        # Не удаляем запись целиком — могут быть другие поля (pump state)
        _save_state(state)
