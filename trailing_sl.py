"""Trailing SL для разогнанных позиций."""
import os
from . import TRAIL_SL_PERCENT
from .api import get_bb_data, place_stop_loss
from .alerts import log_event
from .manual_positions import is_manual_position

# WebSocket BB-кеш (Фаза 6) — feature flag BYBIT_WS_BB_ENABLED
_WS_BB_ENABLED = os.environ.get('BYBIT_WS_BB_ENABLED', '1') == '1'

def _get_bb_ws(symbol, interval='W'):
    """Получить BB: сначала WS-кеш, fallback на REST."""
    if _WS_BB_ENABLED:
        try:
            from .ws_client import get_bb as ws_get_bb, is_connected as ws_alive, is_stale as ws_stale
            if ws_alive() and not ws_stale(300):
                bb = ws_get_bb(symbol, interval)
                if bb and bb.get('lower', 0) > 0:
                    return bb
        except Exception:
            pass
    # Fallback: REST
    return get_bb_data(symbol, interval)

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
        # X10 позиции — обслуживаются trailing_sl_x10, не трогаем
        if p.get('leverage', 0) >= 10:
            continue
        entry, mark, side, size, idx = p['entry'], p['mark'], p['side'], p['size'], p['positionIdx']
        current_sl = p.get('stopLoss')
        if size <= 0:
            continue

        if side == 'Buy':
            # LONG: цена у верхней полосы, профит >15%
            pnl_pct = (mark - entry) / entry * 100
            bb = _get_bb_ws(sym, 'W')
            if not bb or bb['bb_pos'] is None:
                continue
            bb_pos = bb['bb_pos']
            if bb_pos > 75 and pnl_pct > 15:
                sl_target = entry + TRAIL_SL_PERCENT * (mark - entry)
                sl_target = round(sl_target, 4)
                # Защита: не опускаем SL, если он уже в прибыли
                if current_sl is not None and current_sl > entry:
                    continue  # SL уже зафиксирован выше входа — не трогаем
                if current_sl is None or sl_target > (current_sl or 0):
                    if mark > sl_target > entry:
                        actions.append((sym, idx, side, size, sl_target))
                        log_event(f'🔺 Trailing SL {sym}: entry=${entry:.4f} mark=${mark:.4f} pnl={pnl_pct:.1f}% W_bb={bb_pos:.0f}% → SL=${sl_target:.4f}')
        elif side == 'Sell':
            # SHORT: цена у нижней полосы, профит >15%
            pnl_pct = (entry - mark) / entry * 100
            bb = _get_bb_ws(sym, 'W')
            if not bb or bb['bb_pos'] is None:
                continue
            bb_pos = bb['bb_pos']
            if bb_pos < 25 and pnl_pct > 15:
                sl_target = entry - TRAIL_SL_PERCENT * (entry - mark)
                sl_target = round(sl_target, 4)
                # Защита: не поднимаем SL для SHORT, если он уже в прибыли
                if current_sl is not None and current_sl < entry:
                    continue  # SL уже зафиксирован ниже входа — не трогаем
                if current_sl is None or sl_target < (current_sl or float('inf')):
                    if entry > sl_target > mark:
                        actions.append((sym, idx, side, size, sl_target))
                        log_event(f'🔻 Trailing SL {sym}: entry=${entry:.4f} mark=${mark:.4f} pnl={pnl_pct:.1f}% W_bb={bb_pos:.0f}% → SL=${sl_target:.4f}')
    return actions

def apply_trailing_sl(actions):
    for sym, idx, side, size, price in actions:
        place_stop_loss(sym, idx, side, size, price)


# ── Simple Trailing SL (28.06.2026) ──
# Подтягивает SL за ценой без жёстких условий.
# Каждые +5% прибыли → SL = безубыток + половина прибыли.

def simple_trailing_sl(positions):
    """Простой трейлинг: каждые +5% прибыли подтягиваем SL.
    LONG: pnl > 5% → SL = entry + 0.5 * (mark - entry)
    SHORT: pnl > 5% → SL = entry - 0.5 * (entry - mark)
    Не требует BB-проверки.
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
            if pnl_pct < 5:
                continue
            # SL = вход + 50% прибыли
            sl_target = round(entry + 0.5 * (mark - entry), 4)
            if current_sl is not None and float(current_sl or 0) >= sl_target:
                continue  # уже лучше
            if sl_target > entry and sl_target < mark:
                actions.append((sym, idx, side, size, sl_target))
                log_event(
                    f'📈 Trail SL {sym}: entry=${entry:.4f} mark=${mark:.4f} '
                    f'pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                )
        elif side == 'Sell':
            pnl_pct = (entry - mark) / entry * 100
            if pnl_pct < 5:
                continue
            sl_target = round(entry - 0.5 * (entry - mark), 4)
            if current_sl is not None and float(current_sl or 0) <= sl_target:
                continue
            if sl_target < entry and sl_target > mark:
                actions.append((sym, idx, side, size, sl_target))
                log_event(
                    f'📉 Trail SL {sym}: entry=${entry:.4f} mark=${mark:.4f} '
                    f'pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                )
    return actions


# ── Tight Trailing SL (01.07.2026) ──
# Идея Алексея: агрессивный трейлинг для лонгов.
# +3% → SL = entry + 2% (фиксация 66% прибыли на старте).
# Далее SL = mark × 0.99 (всегда 1% ниже рынка).
# Только вверх — никогда не опускаем SL.

def tight_trailing_sl(positions):
    """Агрессивный трейлинг (идея Алексея):
    LONG: +3% → SL = entry * 1.02 (первая активация), далее SL = max(текущий_SL, mark * 0.99).
    SHORT: -3% → SL = entry * 0.98 (первая активация), далее SL = min(текущий_SL, mark * 1.01).
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

            # Первая активация: +3%
            if pnl_pct < 3:
                continue

            if current_sl is None or float(current_sl or 0) <= entry:
                sl_target = round(entry * 1.02, 4)
            else:
                sl_target = round(mark * 0.99, 4)
                if sl_target <= float(current_sl or 0):
                    continue

            if sl_target > entry and sl_target < mark:
                actions.append((sym, idx, side, size, sl_target))
                log_event(
                    f'🎯 Tight SL {sym}: entry=${entry:.4f} mark=${mark:.4f} '
                    f'pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                )

        elif side == 'Sell':
            pnl_pct = (entry - mark) / entry * 100

            # Первая активация: -3%
            if pnl_pct < 3:
                continue

            if current_sl is None or float(current_sl or float('inf')) >= entry:
                # Первый спуск: SL = entry - 2%
                sl_target = round(entry * 0.98, 4)
            else:
                # SL уже активирован — подтягиваем до mark * 1.01
                sl_target = round(mark * 1.01, 4)
                # Не поднимаем
                if sl_target >= float(current_sl or 0):
                    continue

            if sl_target < entry and sl_target > mark:
                actions.append((sym, idx, side, size, sl_target))
                log_event(
                    f'🎯 Tight SL {sym}: entry=${entry:.4f} mark=${mark:.4f} '
                    f'pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                )

    return actions


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
        # Только x10 позиции (плечо ≥ 10)
        if p.get('leverage', 0) < 10:
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

            # Защита: не опускаем SL, если он уже в прибыли (ручная фиксация)
            if current_sl is not None and current_sl > entry:
                continue
            if current_sl is None or sl_target > (current_sl or 0):
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

            # Защита: не поднимаем SL для SHORT, если он уже в прибыли
            if current_sl is not None and current_sl < entry:
                continue
            if current_sl is None or sl_target < (current_sl or float('inf')):
                if entry > sl_target > mark:
                    actions.append((sym, idx, side, size, sl_target))
                    log_event(
                        f'⚡ x10 Trail SL {sym}: entry=${entry:.4f} '
                        f'mark=${mark:.4f} pnl={pnl_pct:.1f}% → SL=${sl_target:.4f}'
                    )

    return actions
