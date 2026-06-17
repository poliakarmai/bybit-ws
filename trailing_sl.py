"""Trailing SL для разогнанных позиций."""
import math
from . import TRAIL_SL_PERCENT
from .api import bybit, get_bb_data, place_stop_loss
from .alerts import log_event
from .manual_positions import is_manual_position

def trailing_sl(positions):
    """Подтянуть SL:
    LONG: Weekly BB >75% И профит >15% → SL подтягивается вверх.
    SHORT: Weekly BB <25% И профит >15% → SL подтягивается вниз.
    """
    actions = []
    for sym, p in positions.items():
        # Ручные позиции — не подтягиваем SL
        if is_manual_position(sym):
            continue
        entry, mark, side, size, idx = p['entry'], p['mark'], p['side'], p['size'], p['positionIdx']
        current_sl = p.get('stopLoss')
        if size <= 0:
            continue

        if side == 'Buy':
            # LONG: цена у верхней полосы, профит >15%
            pnl_pct = (mark - entry) / entry * 100
            bb = get_bb_data(sym, 'W')
            if not bb or bb['bb_pos'] is None:
                continue
            bb_pos = bb['bb_pos']
            if bb_pos > 75 and pnl_pct > 15:
                sl_target = entry + TRAIL_SL_PERCENT * (mark - entry)
                sl_target = round(sl_target, 4)
                if current_sl is None or abs(current_sl - sl_target) > (mark * 0.005):
                    if mark > sl_target > entry:
                        actions.append((sym, idx, side, size, sl_target))
                        log_event(f'🔺 Trailing SL {sym}: entry=${entry:.4f} mark=${mark:.4f} pnl={pnl_pct:.1f}% W_bb={bb_pos:.0f}% → SL=${sl_target:.4f}')
        elif side == 'Sell':
            # SHORT: цена у нижней полосы, профит >15%
            pnl_pct = (entry - mark) / entry * 100
            bb = get_bb_data(sym, 'W')
            if not bb or bb['bb_pos'] is None:
                continue
            bb_pos = bb['bb_pos']
            if bb_pos < 25 and pnl_pct > 15:
                sl_target = entry - TRAIL_SL_PERCENT * (entry - mark)
                sl_target = round(sl_target, 4)
                if current_sl is None or abs(current_sl - sl_target) > (mark * 0.005):
                    if entry > sl_target > mark:
                        actions.append((sym, idx, side, size, sl_target))
                        log_event(f'🔻 Trailing SL {sym}: entry=${entry:.4f} mark=${mark:.4f} pnl={pnl_pct:.1f}% W_bb={bb_pos:.0f}% → SL=${sl_target:.4f}')
    return actions

def apply_trailing_sl(actions):
    for sym, idx, side, size, price in actions:
        place_stop_loss(sym, idx, side, size, price)


# ── Phase 3: x10 Trailing SL (агрессивный) ──

def trailing_sl_x10(positions):
    """Агрессивный трейлинг для x10 позиций:
    +10% → SL = безубыток (entry)
    +20% → SL = entry + 50% прибыли
    +30% → SL = entry + 75% прибыли
    Не требует BB-проверки — x10 работает на моментуме.
    """
    actions = []
    for sym, p in positions.items():
        if is_manual_position(sym):
            continue
        entry, mark, side, size, idx = (
            p['entry'], p['mark'], p['side'], p['size'], p['positionIdx']
        )
        current_sl = p.get('stopLoss')
        if size <= 0:
            continue

        if side == 'Buy':
            pnl_pct = (mark - entry) / entry * 100
            if pnl_pct <= 10:
                continue

            if pnl_pct > 30:
                # Фиксируем 75% прибыли
                sl_target = entry + 0.75 * (mark - entry)
            elif pnl_pct > 20:
                # Фиксируем 50% прибыли
                sl_target = entry + 0.5 * (mark - entry)
            else:
                # Безубыток
                sl_target = entry * 1.005  # +0.5% буфер

            sl_target = round(sl_target, 4)

            if current_sl is None or abs(current_sl - sl_target) > (mark * 0.005):
                if mark > sl_target > entry:
                    actions.append((sym, idx, side, size, sl_target))
                    log_event(
                        f'⚡ x10 Trail SL {sym}: entry=${entry:.4f} '
                        f'mark=${mark:.4f} pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                    )

        elif side == 'Sell':
            pnl_pct = (entry - mark) / entry * 100
            if pnl_pct <= 10:
                continue

            if pnl_pct > 30:
                sl_target = entry - 0.75 * (entry - mark)
            elif pnl_pct > 20:
                sl_target = entry - 0.5 * (entry - mark)
            else:
                sl_target = entry * 0.995  # -0.5% буфер

            sl_target = round(sl_target, 4)

            if current_sl is None or abs(current_sl - sl_target) > (mark * 0.005):
                if entry > sl_target > mark:
                    actions.append((sym, idx, side, size, sl_target))
                    log_event(
                        f'⚡ x10 Trail SL {sym}: entry=${entry:.4f} '
                        f'mark=${mark:.4f} pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                    )

    return actions
