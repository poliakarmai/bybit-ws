"""X10 Risk Limits — дневной стоп убытков, кулдаун, трекинг PnL.

Защищает от каскадных потерь на x10 стратегиях:
- Стоп после N убыточных сделок за день (max_daily_losses)
- Пауза на N часов после стопа (cooldown_after_stop_hours)
- Трекинг PnL по стратегиям: scalp, mean_revert, funding

Вызывается из main.py:
- record_x10_trade() — после каждого x10-закрытия
- x10_entry_allowed() — перед каждым x10-входом
"""

import json
import logging
import os
import time
from datetime import datetime

_log = logging.getLogger('bybit.x10_limits')

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
X10_STATE_FILE = os.path.join(DATA_DIR, 'x10_limits.json')
X10_POSITIONS_FILE = os.path.join(DATA_DIR, 'x10_positions.json')


def _load():
    try:
        if os.path.exists(X10_STATE_FILE):
            with open(X10_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        _log.warning(f'⚠️ x10_limits: {e}')
    return {}


def _save(state):
    os.makedirs(os.path.dirname(X10_STATE_FILE), exist_ok=True)
    with open(X10_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _today_key():
    return datetime.now().strftime('%Y-%m-%d')


def record_x10_trade(strategy: str, pnl: float):
    """Записать результат x10-сделки. strategy = 'scalp'|'mean_revert'|'funding'."""
    state = _load()
    today = _today_key()

    if today != state.get('date', ''):
        # Новый день — сброс
        state = {'date': today, 'losses': 0, 'trades': []}

    state['losses'] = state.get('losses', 0)
    state['trades'] = state.get('trades', [])

    state['trades'].append({
        'strategy': strategy,
        'pnl': round(float(pnl), 4),
        'ts': time.time(),
    })

    if pnl < 0:
        state['losses'] += 1

    _save(state)


def x10_entry_allowed(config) -> tuple:
    """Проверить разрешены ли x10-входы.

    Returns (allowed: bool, reason: str).
    """
    x10_cfg = config.strategy.x10
    max_losses = x10_cfg.get('max_daily_losses', 3)
    cooldown_hours = x10_cfg.get('cooldown_after_stop_hours', 24)

    state = _load()
    today = _today_key()

    if state.get('date', '') != today:
        # Новый день — разрешено
        return True, ''

    # Проверка превышения дневного лимита убытков
    losses = state.get('losses', 0)
    if losses >= max_losses:
        # Проверяем кулдаун
        trades = state.get('trades', [])
        if trades:
            last_loss_ts = max(t['ts'] for t in trades if t['pnl'] < 0)
            elapsed = (time.time() - last_loss_ts) / 3600
            if elapsed < cooldown_hours:
                remaining = cooldown_hours - elapsed
                return False, f'X10 стоп: {losses} убытков (лимит {max_losses}), кулдаун {remaining:.1f}ч'

            # Кулдаун истёк — разрешаем
            return True, ''

        return False, f'X10 стоп: {losses} убытков (лимит {max_losses})'

    return True, ''


def get_x10_stats() -> dict:
    """Получить статистику x10 за сегодня."""
    state = _load()
    today = _today_key()

    if state.get('date', '') != today:
        return {'date': today, 'losses': 0, 'trades': [], 'total_pnl': 0}

    trades = state.get('trades', [])
    return {
        'date': today,
        'losses': state.get('losses', 0),
        'trades': trades,
        'total_pnl': round(sum(t['pnl'] for t in trades), 4),
    }


def track_x10_entry(sym: str, strategy: str):
    """Запомнить что sym — это x10-позиция стратегии strategy."""
    try:
        if os.path.exists(X10_POSITIONS_FILE):
            with open(X10_POSITIONS_FILE) as f:
                data = json.load(f)
        else:
            data = {}
        data[sym] = {'strategy': strategy, 'ts': time.time()}
        with open(X10_POSITIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        _log.warning(f'⚠️ x10_limits: {e}')


def get_x10_strategy(sym: str) -> str:
    """Получить стратегию x10-позиции."""
    try:
        if os.path.exists(X10_POSITIONS_FILE):
            with open(X10_POSITIONS_FILE) as f:
                data = json.load(f)
            return data.get(sym, {}).get('strategy', '')
    except Exception as e:
        _log.warning(f'⚠️ x10_limits: {e}')
    return ''


def clear_x10_position(sym: str):
    """Очистить трекинг x10-позиции после закрытия."""
    try:
        if os.path.exists(X10_POSITIONS_FILE):
            with open(X10_POSITIONS_FILE) as f:
                data = json.load(f)
            if sym in data:
                del data[sym]
                with open(X10_POSITIONS_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
    except Exception as e:
        _log.warning(f'⚠️ x10_limits: {e}')
