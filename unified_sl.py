"""
Unified SL Manager — one API call per position per cycle.
Consolidates: auto_sl, trailing_sl, simple_trailing_sl, tight_trailing_sl, breakeven_sl, check_and_fix_sl.

Priority: tight_trail > simple_trail > hard_trail > breakeven > auto_sl > default_fix
"""
import time
from .api import place_stop_loss, get_bb_data
from .alerts import log_event
from . import TRAIL_SL_PERCENT

# Throttle: не чаще чем раз в N секунд на позицию (по имени символа).
# Храним ВРЕМЯ РАЗРЕШЕНИЯ, а не время последней попытки: слот занимается
# только после успешного вызова API (раньше ошибка вешала позицию без SL на 120с).
_next_sl_update: dict[str, float] = {}
SL_THROTTLE = 120  # секунд между реальными обновлениями SL
SL_ERROR_BACKOFF = 15  # быстрый ретрай, если API не принял SL

# Безубыток — каждые 4 цикла (~2 мин), но не чаще throttling'а
SL_BREAKEVEN_CYCLES = 4

def _throttled(sym: str) -> bool:
    """True если обновление SL разрешено. Слот НЕ занимает — см. _mark()."""
    return time.time() >= _next_sl_update.get(sym, 0)


def _mark(sym: str, delay: int = SL_THROTTLE) -> None:
    """Занять слот: следующая попытка для символа не раньше now+delay."""
    _next_sl_update[sym] = time.time() + delay


def manage_sl(positions: dict, cycle: int = 0) -> list[str]:
    """
    Единый SL-менеджер. Для каждой позиции вычисляет приоритетный SL
    и выставляет его ОДНИМ API-вызовом.

    Возвращает список alert-сообщений.
    """
    alerts = []

    for sym, p in positions.items():

        entry = p.get('entry', 0)
        mark = p.get('mark', 0)
        side = p.get('side', 'Buy')
        idx = p.get('positionIdx', 0)
        current_sl = p.get('stopLoss')
        size = p.get('size', 0)
        leverage = p.get('leverage', 1)

        if size <= 0 or entry <= 0 or mark <= 0:
            continue

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
                        sl_target, sl_desc = _calc_default_sl(sym, p, is_long, mark, entry, leverage)

        if sl_target is None:
            continue

        # Skip if SL hasn't changed meaningfully (>0.1% от entry)
        if current_sl is not None and abs(sl_target - current_sl) / entry < 0.001:
            continue

        # Throttle check — не дёргаем API чаще чем раз в 2 минуты на символ
        if not _throttled(sym):
            continue

        # Place SL
        try:
            result = place_stop_loss(sym, idx, side, size, sl_target)
            if result:
                _mark(sym)  # успех — обычный троттл
                alerts.append(f'🛡 SL {sym}: ${sl_target:.4f} ({sl_desc})')
            else:
                _mark(sym, SL_ERROR_BACKOFF)  # API не принял — пробуем скоро
                alerts.append(f'⚠️ SL {sym} НЕ встал: API error')
        except Exception as e:
            err = str(e)
            if 'not modified' in err.lower():
                _mark(sym)  # SL уже стоит — дёргать бессмысленно
            else:
                _mark(sym, SL_ERROR_BACKOFF)
                alerts.append(f'⚠️ SL {sym} НЕ встал: {err}')

    return alerts


def _calc_tight_trail(p: dict, is_long: bool, mark: float, entry: float):
    """Tight trailing: +3% → SL=mark×0.99 (LONG) / mark×1.01 (SHORT).

    Монотонность (17.09.2026): SL двигается ТОЛЬКО в прибыльную сторону —
    LONG вверх (target > current_sl), SHORT вниз (target < current_sl).
    Раньше target=mark×0.99 пересчитывался каждый цикл без сверки с текущим
    SL → при микро-падении mark SL ехал вниз (осцилляция $0.0088↔$0.0089).
    """
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)
    current_sl = p.get('stopLoss')

    if pnl_pct < 3:
        return None, ''

    if is_long:
        # LONG: SL подтягиваем вверх
        target = round(mark * 0.99, 4)
        if target > entry * 1.02 and (current_sl is None or target > current_sl):
            return target, f'tight trail LONG +{pnl_pct:.1f}%'
    else:
        # SHORT: SL подтягиваем вниз
        target = round(mark * 1.01, 4)
        if target < entry * 0.98 and (current_sl is None or target < current_sl):
            return target, f'tight trail SHORT +{pnl_pct:.1f}%'

    return None, ''


def _calc_simple_trail(p: dict, is_long: bool, mark: float, entry: float):
    """Simple trailing: каждые +5% прибыли двигают SL на половину пройденного пути.

    25.10: убрано требование «SL уже выше entry». Из-за него трейлинг не включался
    никогда, пока SL не подтянут другими приоритетами: при +5..15% и дефолтном SL
    ниже entry позиция получала SL −5..−10% вместо половинного трейлинга.
    Монотонность сохранена: target двигается только в прибыльную сторону.
    """
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)
    current_sl = p.get('stopLoss')

    if pnl_pct < 5:
        return None, ''

    if is_long:
        target = round(entry + 0.5 * (mark - entry), 4)
        if target > entry and (current_sl is None or target > current_sl):
            return target, f'simple trail LONG +{pnl_pct:.1f}%'
    else:
        target = round(entry - 0.5 * (entry - mark), 4)
        if target < entry and (current_sl is None or target < current_sl):
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
    """Breakeven: +10% → SL = entry + 1% (LONG) / -10% → SL = entry - 1% (SHORT).

    Монотонность (17.09.2026): не откатывать SL, уже подтянутый tight/simple
    trail'ом выше entry*1.01 — иначе BE откатит SL вниз.
    """
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)
    current_sl = p.get('stopLoss')

    if pnl_pct < 10:
        return None, ''

    if is_long and mark > entry * 1.03:
        target = round(entry * 1.01, 4)
        if current_sl is None or target > current_sl:
            return target, f'BE LONG +{pnl_pct:.1f}%'
    elif not is_long and mark < entry * 0.97:
        target = round(entry * 0.99, 4)
        if current_sl is None or target < current_sl:
            return target, f'BE SHORT +{pnl_pct:.1f}%'

    return None, ''


def _calc_default_sl(sym: str, p: dict, is_long: bool, mark: float, entry: float, leverage: float):
    """Default SL: ATR-адаптивный (1.5×ATR(14), clamp 3–10%), fallback −10%.

    04.10.2026: динамический шаг по волатильности вместо фикс −10%.
    Волатильная монета (SAND +65%/нед) → шире SL (до 10%), спокойная (BTC) → уже (3–5%).
    """
    current_sl = p.get('stopLoss')
    if current_sl is not None:
        return None, ''

    atr = 0.0
    try:
        from .auto_tp import _get_atr_value
        atr = _get_atr_value(sym)
    except Exception:
        atr = 0.0

    if atr > 0 and entry > 0:
        dist = max(0.03, min(0.10, 1.5 * atr / entry))
        if is_long:
            target = round(entry * (1 - dist), 4)
        else:
            target = round(entry * (1 + dist), 4)
        return target, f'ATR SL {dist*100:.1f}%'

    # fallback: фикс −10% (нет ATR / ошибка сети)
    if is_long:
        target = round(entry * 0.90, 4)
    else:
        target = round(entry * 1.10, 4)
    return target, 'default -10%'
