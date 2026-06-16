"""Авто-SL: ставит SL по стратегии (BB-based, Tier-based) вместо жестких -7%.

DESIGN.md §Стратегия:
- LONG: SL = Lower BB Daily * 0.93 (−7% от Lower BB)
- SHORT Tier A/B: SL = +5% от входа
- SHORT Tier C/D: SL = +7% от входа
Фикс код-ревью Manus AI.
"""

import os, json
from .api import bybit, fetch_positions, get_bb_data
from .config import get_config
from .alerts import log_event
from .manual_positions import is_manual_position


def _get_tiers(cfg):
    """Вернуть (TIER_AB, ONE_WAY) из конфига."""
    tier_ab = set()
    one_way = set()
    try:
        tier_ab = set(cfg.tiers.A) | set(cfg.tiers.B)
    except Exception:
        tier_ab = set()
    try:
        one_way = set(cfg.tiers.one_way)
    except Exception:
        one_way = set()
    return tier_ab, one_way


def check_and_fix_sl():
    """Проверить все позиции, поставить SL тем у кого нет. Возвращает список алертов."""
    alerts = []
    positions = fetch_positions()
    if not positions:
        return alerts

    cfg = get_config()
    tier_ab, one_way = _get_tiers(cfg)

    for sym, p in positions.items():
        # Ручные позиции — не трогаем SL (пользователь управляет сам)
        if is_manual_position(sym):
            continue

        sl = p.get('stopLoss')
        if sl is not None and sl != '' and sl != '0' and float(sl or 0) > 0:
            continue  # SL уже есть

        mark = p['mark']
        side = p['side']
        idx = p['positionIdx']
        size = p['size']
        entry = p['entry']

        # Не ставить SL на прибыльные позиции — пусть работает TP
        if side == 'Buy' and mark > entry:
            continue
        if side == 'Sell' and mark < entry:
            continue

        if side == 'Buy':
            # LONG: SL = -7% от Lower BB Daily
            bb = get_bb_data(sym, 'D')
            if bb and bb['lower'] > 0:
                sl_price = bb['lower'] * 0.93
                sl_desc = f'-7% от Lower BB (${bb["lower"]:.4f})'
            else:
                # Fallback: -7% от mark
                sl_price = mark * 0.93
                sl_desc = f'-7% от Mark (нет BB)'
        else:
            # SHORT: проверка на JUNK через pumps.json (помечены pump_detect/auto_short)
            try:
                state_file = os.path.join(os.path.expanduser('~/.local/share/bybit-ws'), 'pumps.json')
                with open(state_file) as f:
                    pump_state = json.loads(f.read())
                entry = pump_state.get(sym, {})
                # Прямой short_entry_ts (от auto_short / _place_pump_short)
                if entry.get('short_entry_ts'):
                    log_event(f'⏭️ JUNK {sym}: пропуск авто-SL (short_entry_ts)')
                    continue
                # Pump detector tracking (first_seen_ts + alerts) — ручные JUNK-входы
                # pump_detect перезаписывает pumps.json каждый цикл, стирая short_entry_ts
                if entry.get('first_seen_ts') and entry.get('alerts'):
                    log_event(f'⏭️ JUNK {sym}: пропуск авто-SL (pump_detect tracking)')
                    continue
                # Ручной JUNK без стопа (daily_pump / manual флаг)
                if entry.get('daily_pump') or entry.get('manual'):
                    log_event(f'⏭️ JUNK {sym}: пропуск авто-SL (manual/daily_pump)')
                    continue
            except Exception as e:
                log_event(f'⚠️ auto_sl: ошибка чтения pumps.json для {sym}: {e}')

            # Tier-based SL
            is_junk = sym not in tier_ab and sym not in one_way
            if is_junk:
                sl_price = entry * 1.07
                sl_desc = '+7% от входа (Tier C/D)'
            else:
                sl_price = entry * 1.05
                sl_desc = '+5% от входа (Tier A/B)'

        sl_price = round(sl_price, 4)
        # Проверка что SL на правильной стороне
        if side == 'Buy' and sl_price >= mark:
            continue
        if side == 'Sell' and sl_price <= mark:
            continue

        # Bybit v5 trading-stop: только category, symbol, positionIdx, stopLoss, slTriggerBy
        body = {
            'category': 'linear',
            'symbol': sym,
            'positionIdx': idx,
            'stopLoss': str(sl_price),
            'slTriggerBy': 'MarkPrice',
        }

        data = bybit('POST', '/v5/position/trading-stop', body)
        if data and data.get('retCode') == 0:
            msg = f'🛡 Авто-SL {sym}: ${sl_price:.4f} ({sl_desc}, вход ${entry:.4f})'
            alerts.append(msg)
        else:
            err = data.get('retMsg', '?') if data else 'no response'
            msg = f'⚠️ Авто-SL {sym} НЕ встал: {err}'
            alerts.append(msg)

    return alerts
