"""
Unified SL Manager — one API call per position per cycle.
Consolidates: auto_sl, trailing_sl, simple_trailing_sl, tight_trailing_sl, breakeven_sl, check_and_fix_sl.

Priority: tight_trail > simple_trail > hard_trail > breakeven > auto_sl > default_fix
"""
import time
from .api import place_stop_loss, get_bb_data
from .alerts import log_event
from .manual_positions import is_manual_position
from . import TRAIL_SL_PERCENT

# Throttle: не чаще чем раз в N секунд на позицию (по имени символа)
_last_sl_update: dict[str, float] = {}
SL_THROTTLE = 120  # секунд между реальными обновлениями SL

# Безубыток — каждые 4 цикла (~2 мин), но не чаще throttling'а
SL_BREAKEVEN_CYCLES = 4

def _throttled(sym: str) -> bool:
    """True если обновление SL для этого символа разрешено."""
    now = time.time()
    last = _last_sl_update.get(sym, 0)
    if now - last < SL_THROTTLE:
        return False
    _last_sl_update[sym] = now
    return True


def manage_sl(positions: dict, cycle: int = 0) -> list[str]:
    """
    Единый SL-менеджер. Для каждой позиции вычисляет приоритетный SL
    и выставляет его ОДНИМ API-вызовом.

    Возвращает список alert-сообщений.
    """
    alerts = []

    for sym, p in positions.items():
        if is_manual_position(sym):
            continue

        entry = p.get('entry', 0)
        mark = p.get('mark', 0)
        side = p.get('side', 'Buy')
        idx = p.get('positionIdx', 0)
        current_sl = p.get('stopLoss')
        size = p.get('size', 0)
        leverage = p.get('leverage', 1)

        if size <= 0 or entry <= 0 or mark <= 0:
            continue

        # ── v8.1: Time-stop: закрыть если в минусе > 48ч ──
        created_ms = p.get('createdTime', 0)
        if created_ms and created_ms > 0:
            upnl = float(p.get("unrealisedPnl", p.get("upnl", 0)))
            hold_h = (time.time() * 1000 - created_ms) / (3600 * 1000)
            if hold_h > 48 and upnl < 0:
                from .alerts import log_event, send_high_alert
                try:
                    from .api import bybit
                    close_side = "Sell" if side == "Buy" else "Buy"
                    res = bybit("POST", "/v5/order/create", {
                        "category": "linear", "symbol": sym, "side": close_side,
                        "orderType": "Market", "qty": str(size),
                        "positionIdx": idx, "reduceOnly": True,
                    })
                    if res and res.get("retCode") == 0:
                        msg = f"⏰ TIME-STOP {sym}: {hold_h:.0f}h, -${abs(upnl):.2f} — closed"
                        log_event(msg)
                        send_high_alert(msg, level="CLOSE")
                        alerts.append(msg)
                    else:
                        err = res.get("retMsg", "?") if res else "?"
                        log_event(f"⚠️ TIME-STOP {sym}: close failed — {err}")
                except Exception as e:
                    log_event(f"⚠️ TIME-STOP {sym}: {e}")
                continue  # skip SL for closed position

        # ── v8.1: Time-stop — закрыть если в минусе > 48ч ──
        created_ms = p.get('createdTime', 0)
        upnl = float(p.get('unrealisedPnl', p.get('upnl', 0)))
        hold_h = (time.time() * 1000 - created_ms) / (3600 * 1000) if created_ms else 0
        if hold_h > 48 and upnl < 0:
            from .alerts import log_event, send_high_alert
            try:
                from .api import bybit
                close_side = 'Sell' if is_long else 'Buy'
                res = bybit('POST', '/v5/order/create', {
                    'category': 'linear', 'symbol': sym, 'side': close_side,
                    'orderType': 'Market', 'qty': str(size),
                    'positionIdx': idx, 'reduceOnly': True,
                })
                if res and res.get('retCode') == 0:
                    msg = f'⏰ TIME-STOP {sym}: {hold_h:.0f}ч в минусе (${upnl:.2f}) — закрыто'
                    log_event(msg)
                    send_high_alert(msg, level='CLOSE')
                    alerts.append(msg)
                else:
                    log_event(f'⚠️ TIME-STOP {sym}: не удалось закрыть — {res.get('retMsg', '?')}')
            except Exception as e:
                log_event(f'⚠️ TIME-STOP {sym}: {e}')
            continue  # позиция закрыта, SL не нужен

        is_long = side == 'Buy'
        sl_target = None
        sl_desc = ''

        # ── Priority 1: Tight trailing (3%→2%→0.99×mark) ──
        sl_target, sl_desc = _calc_tight_trail(p, is_long, mark, entry)
        if sl_target is not None:
            pass  # highest priority
        else:
            # ── Priority 2: Simple trailing (every +5%, no BB) ──
            sl_target, sl_desc = _calc_simple_trail(p, is_long, mark, entry)
            if sl_target is None:
                # ── Priority 3: Hard trailing (BB >75%/<25% AND pnl >15%) ──
                sl_target, sl_desc = _calc_hard_trail(sym, is_long, mark, entry, current_sl)
                if sl_target is None:
                    # ── Priority 4: Breakeven (every 4th cycle) ──
                    if cycle % SL_BREAKEVEN_CYCLES == 0:
                        sl_target, sl_desc = _calc_breakeven(p, is_long, mark, entry)
                    if sl_target is None:
                        # ── Priority 5: Default fix (ensure SL exists) ──
                        sl_target, sl_desc = _calc_default_sl(p, is_long, mark, entry, leverage)

        if sl_target is None:
            continue

        # Skip if SL hasn't changed meaningfully (>0.1% от entry)
        if current_sl is not None and abs(sl_target - current_sl) / entry < 0.001:
            continue

        # Throttle check — для ВСЕХ типов SL (включая tight)
        if not _throttled(sym):
            continue

        # Place SL
        try:
            result = place_stop_loss(sym, idx, side, size, sl_target)
            if result:
                alerts.append(f'🛡 SL {sym}: ${sl_target:.4f} ({sl_desc})')
            else:
                alerts.append(f'⚠️ SL {sym} НЕ встал: API error')
        except Exception as e:
            err = str(e)
            if 'not modified' not in err.lower():
                alerts.append(f'⚠️ SL {sym} НЕ встал: {err}')

    return alerts


def _calc_tight_trail(p: dict, is_long: bool, mark: float, entry: float):
    """Tight trailing: +3% → SL=entry+2%, затем mark×0.99"""
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)

    if pnl_pct < 3:
        return None, ''

    if is_long:
        # LONG: SL подтягиваем вверх
        target = round(mark * 0.99, 4)
        if target > entry * 1.02:  # минимум +2% от входа
            return target, f'tight trail LONG +{pnl_pct:.1f}%'
    else:
        # SHORT: SL подтягиваем вниз
        target = round(mark * 1.01, 4)
        if target < entry * 0.98:  # минимум -2% от входа
            return target, f'tight trail SHORT +{pnl_pct:.1f}%'

    return None, ''


def _calc_simple_trail(p: dict, is_long: bool, mark: float, entry: float):
    """Simple trailing: every +5% profit, move SL halfway"""
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)
    current_sl = p.get('stopLoss')

    if pnl_pct < 5:
        return None, ''

    if is_long and current_sl and current_sl > entry:
        # SL уже в плюсе — подтягиваем
        target = round(entry + 0.5 * (mark - entry), 4)
        if target > current_sl:
            return target, f'simple trail LONG +{pnl_pct:.1f}%'
    elif not is_long and current_sl and current_sl < entry:
        target = round(entry - 0.5 * (entry - mark), 4)
        if target < current_sl:
            return target, f'simple trail SHORT +{pnl_pct:.1f}%'

    return None, ''


def _calc_hard_trail(sym: str, is_long: bool, mark: float, entry: float, current_sl):
    """Hard trailing: BB >75% AND pnl >15% (LONG), BB <25% AND pnl >15% (SHORT)"""
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)

    if pnl_pct < 15:
        return None, ''

    try:
        bb = get_bb_data(sym, 'W')
        if not bb or bb.get('bb_pos') is None:
            return None, ''
        bb_pos = bb['bb_pos']
    except Exception:
        return None, ''

    if is_long and bb_pos > 75:
        target = round(entry + TRAIL_SL_PERCENT * (mark - entry), 4)
        if current_sl is None or target > current_sl:
            return target, f'hard trail LONG BB{bb_pos:.0f}%'
    elif not is_long and bb_pos < 25:
        target = round(entry - TRAIL_SL_PERCENT * (entry - mark), 4)
        if current_sl is None or target < current_sl:
            return target, f'hard trail SHORT BB{bb_pos:.0f}%'

    return None, ''


def _calc_breakeven(p: dict, is_long: bool, mark: float, entry: float):
    """Breakeven: +10% → SL = entry + 1% (LONG) / -10% → SL = entry - 1% (SHORT)"""
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)

    if pnl_pct < 10:
        return None, ''

    if is_long and mark > entry * 1.03:
        target = round(entry * 1.01, 4)
        return target, f'BE LONG +{pnl_pct:.1f}%'
    elif not is_long and mark < entry * 0.97:
        target = round(entry * 0.99, 4)
        return target, f'BE SHORT +{pnl_pct:.1f}%'

    return None, ''


def _calc_default_sl(p: dict, is_long: bool, mark: float, entry: float, leverage: float):
    """Default SL: -10% от входа если SL не установлен"""
    current_sl = p.get('stopLoss')
    if current_sl is not None:
        return None, ''

    if is_long:
        target = round(entry * 0.90, 4)
    else:
        target = round(entry * 1.10, 4)

    return target, 'default -10%'
