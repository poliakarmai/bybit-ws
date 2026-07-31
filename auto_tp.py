"""Auto-TP v2 — ATR-адаптивные тейк-профиты (27.06.2026).

Заменяет фиксированные 20/80 на ATR-адаптивный сплит:
- Высокая волатильность (ATR > 5% от цены) → 40% ближний TP, 60% дальний
- Нормальная (2-5%) → 25% ближний, 75% дальний
- Низкая (<2%) → 15% ближний, 85% дальний

PERM_SKIP с time-decay: через 24ч сбрасывается автоматически.
"""
import time
from . import TP_FAIL_COUNT, TP_FAIL_BACKOFF, TP_FAIL_DELAYS, TP_MAX_FAILS, TP_PERM_SKIP, TP_PERM_SKIP_SIZES, TP_SKIP_FILE
from .api import get_bb_data
from .alerts import log_event
from .manual_positions import is_manual_position
from .file_utils import safe_json_write

import json, os, math

# ── ATR-adaptive split thresholds ──
ATR_HIGH_THRESHOLD = 0.05    # >5% ATR/price → high vol
ATR_LOW_THRESHOLD = 0.02     # <2% → low vol
# Split ratios: (near_tp_pct, far_tp_pct)
SPLIT_HIGH_VOL = (0.40, 0.60)
SPLIT_NORMAL = (0.25, 0.75)
SPLIT_LOW_VOL = (0.15, 0.85)

# ── ATR-based TP levels (28.06) ──
ATR_TP_ENABLED = os.environ.get('BYBIT_ATR_TP_ENABLED', '1') == '1'
# Множители ATR для TP уровней (LONG: entry + k×ATR, SHORT: entry - k×ATR)
ATR_TP_LEVELS = [1.0, 2.0, 3.0]  # TP1, TP2, TP3
ATR_TP_SPLITS = [0.40, 0.35, 0.25]  # % объёма на каждый уровень

# PERM_SKIP time-decay: 24 часа
PERM_SKIP_TTL = 86400

def _load_tp_skip():
    """Загрузить PERM_SKIP с диска + time-decay очистка."""
    try:
        if os.path.exists(TP_SKIP_FILE):
            with open(TP_SKIP_FILE) as f:
                data = json.load(f)
            now = time.time()
            expired = []
            for sym, ts in data.get('timestamps', {}).items():
                if now - ts > PERM_SKIP_TTL:
                    expired.append(sym)
            for sym in expired:
                data.get('skip', []).remove(sym) if sym in data.get('skip', []) else None
                data.get('sizes', {}).pop(sym, None)
                data.get('timestamps', {}).pop(sym, None)
            if expired:
                log_event(f'♻️ TP PERM_SKIP: time-decay очистил {", ".join(expired)}')
                safe_json_write(TP_SKIP_FILE, data)
            for sym in data.get('skip', []):
                TP_PERM_SKIP.add(sym)
            TP_PERM_SKIP_SIZES.update(data.get('sizes', {}))
    except Exception as e:
        log_event(f'⚠️ auto_tp load: {e}')

def _save_tp_skip():
    """Сохранить PERM_SKIP на диск с timestamps."""
    try:
        data = {
            'skip': list(TP_PERM_SKIP),
            'sizes': dict(TP_PERM_SKIP_SIZES),
            'timestamps': {s: time.time() for s in TP_PERM_SKIP},
        }
        safe_json_write(TP_SKIP_FILE, data)
    except Exception as e:
        log_event(f'⚠️ auto_tp save: {e}')

_load_tp_skip()


def _get_atr_split(sym: str) -> tuple:
    """Рассчитать ATR-адаптивный сплит TP.
    
    Returns:
        (near_pct, far_pct) — доли для ближнего и дальнего TP
    """
    try:
        from .api import bybit
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=15')
        if kline and kline.get('retCode') == 0:
            candles = kline['result']['list']
            if len(candles) >= 14:
                # ATR(14) simplified: average high-low range
                ranges = []
                for c in candles[:14]:
                    high = float(c[2])
                    low = float(c[3])
                    ranges.append(high - low)
                atr = sum(ranges) / len(ranges)
                price = float(candles[0][4])  # close
                atr_pct = atr / price if price > 0 else 0.03

                if atr_pct > ATR_HIGH_THRESHOLD:
                    return SPLIT_HIGH_VOL
                elif atr_pct < ATR_LOW_THRESHOLD:
                    return SPLIT_LOW_VOL
    except Exception:
        pass
    return SPLIT_NORMAL


def _get_atr_value(sym: str) -> float:
    """ATR(14) для символа — сырое значение, не процент."""
    try:
        from .api import bybit
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=15')
        if kline and kline.get('retCode') == 0:
            candles = kline['result']['list']
            if len(candles) >= 14:
                ranges = []
                for c in candles[:14]:
                    high = float(c[2])
                    low = float(c[3])
                    ranges.append(high - low)
                return sum(ranges) / len(ranges)
    except Exception:
        pass
    return 0.0


def auto_take_profit(positions, orders, skip_syms=None):
    """Поставить TP: BB-уровни + ATR-based fallback (entry ± k×ATR)."""
    skip_syms = skip_syms or set()
    existing_tp = {}
    if isinstance(orders, dict):
        order_iter = orders.values()
    elif isinstance(orders, list):
        order_iter = orders
    else:
        order_iter = []
    for o in order_iter:
        if o.get('kind') == 'TP' and o.get('status') in ('New', 'PartiallyFilled', 'Untriggered'):
            sym = o['symbol']
            if sym not in existing_tp:
                existing_tp[sym] = []
            existing_tp[sym].append((float(o.get('qty', 0)), float(o.get('price', 0))))

    actions = []
    bb_covered = set()  # символы получившие TP через BB

    for sym, p in positions.items():
        if is_manual_position(sym):
            continue

        # PERM_SKIP: проверяем рост позиции ИЛИ time-decay (загружен при init)
        if sym in TP_PERM_SKIP:
            prev_size = TP_PERM_SKIP_SIZES.get(sym, 0)
            if p['size'] > prev_size * 1.2:
                TP_PERM_SKIP.discard(sym)
                TP_PERM_SKIP_SIZES.pop(sym, None)
                _save_tp_skip()
                log_event(f'♻️ TP {sym}: позиция выросла {prev_size:.1f}->{p["size"]:.1f}, снимаю скип')
            else:
                continue

        if sym in skip_syms:
            continue

        side = p.get('side', 'Buy')
        pos_size = p['size']
        if pos_size <= 0:
            continue

        bb = get_bb_data(sym, 'D')
        rounding = 0 if pos_size >= 10 else 1

        # ── BB-based TP (существующая логика) ──
        bb_used = False
        if bb:
            middle, upper, lower, cur = bb['middle'], bb['upper'], bb.get('lower', 0), bb['cur']
            near_pct, far_pct = _get_atr_split(sym)

            if side == 'Buy':
                need_mid = round(pos_size * near_pct, rounding)
                need_far = round(pos_size * far_pct, rounding)
                if need_mid < 0.5:
                    need_far = round(pos_size, rounding)
                    need_mid = 0

                existing = existing_tp.get(sym, [])
                has_mid = sum(q for q, pr in existing if abs(pr - middle) / middle < 0.02) if middle > 0 else 0
                has_far = sum(q for q, pr in existing if abs(pr - upper) / upper < 0.02) if upper > 0 else 0

                if need_mid > 0 and middle > cur and has_mid < need_mid * 0.9:
                    gap = round(need_mid - has_mid, rounding)
                    if gap > 0:
                        actions.append((sym, p['positionIdx'], side, gap, middle, pos_size))
                        bb_used = True
                if upper > cur and has_far < need_far * 0.9:
                    gap = round(need_far - has_far, rounding)
                    if gap > 0:
                        actions.append((sym, p['positionIdx'], side, gap, upper, pos_size))
                        bb_used = True

            elif side == 'Sell':
                need_mid = round(pos_size * near_pct, rounding)
                need_far = round(pos_size * far_pct, rounding)
                if need_mid < 0.5:
                    need_far = round(pos_size, rounding)
                    need_mid = 0

                existing = existing_tp.get(sym, [])
                has_mid = sum(q for q, pr in existing if abs(pr - middle) / middle < 0.02) if middle > 0 else 0
                has_lo = sum(q for q, pr in existing if abs(pr - lower) / lower < 0.02) if lower > 0 else 0

                if need_mid > 0 and middle < cur and has_mid < need_mid * 0.9:
                    gap = round(need_mid - has_mid, rounding)
                    if gap > 0:
                        actions.append((sym, p['positionIdx'], side, gap, middle, pos_size))
                        bb_used = True
                if lower > 0 and lower < cur and has_lo < need_far * 0.9:
                    gap = round(need_far - has_lo, rounding)
                    if gap > 0:
                        actions.append((sym, p['positionIdx'], side, gap, lower, pos_size))
                        bb_used = True

        # ── ATR-based TP fallback (entry ± k×ATR) ──
        # Срабатывает когда BB не смог поставить TP (цена выше middle BB для LONG и т.д.)
        if ATR_TP_ENABLED and not bb_used and sym not in TP_PERM_SKIP:
            atr = _get_atr_value(sym)
            if atr > 0:
                entry = p['entry']
                existing_qty = sum(q for q, _ in existing_tp.get(sym, []))
                uncovered = pos_size - existing_qty
                if uncovered >= 0.5:
                    for k, split in zip(ATR_TP_LEVELS, ATR_TP_SPLITS):
                        qty = round(uncovered * split, rounding)
                        if qty < 0.5:
                            continue
                        if side == 'Buy':
                            tp_price = entry + k * atr
                        else:
                            tp_price = entry - k * atr
                        # Precision rounding
                        if tp_price >= 100:
                            tp_price = round(tp_price, 1)
                        elif tp_price >= 1:
                            tp_price = round(tp_price, 3)
                        else:
                            tp_price = round(tp_price, 5)
                        actions.append((sym, p['positionIdx'], side, qty, tp_price, pos_size))
                        bb_covered.add(sym)  # отмечаем что ATR отработал

    return actions


def _get_lot_step(sym):
    """Получить минимальный шаг лота для символа (lotSizeFilter.qtyStep)."""
    try:
        from .api import fetch_instruments_info
        info = fetch_instruments_info()
        if info and sym in info:
            lot_filter = info[sym].get('lotSizeFilter', {})
            return float(lot_filter.get('qtyStep', 0.001))
    except Exception:
        pass
    return 0.001

def apply_auto_tp(actions):
    """Применить auto-TP с retry backoff."""
    from .api import place_take_profit
    now = time.time()

    for sym, idx, side, qty, price, pos_size in actions:
        if qty < 0.5:
            TP_FAIL_COUNT.pop(sym, None)
            TP_FAIL_BACKOFF.pop(sym, None)
            continue

        # Проверка qty_step: не отправлять ордер если qty округляется в 0
        qty_step = _get_lot_step(sym)
        if qty_step > 0:
            qty = math.floor(qty / qty_step) * qty_step
            qty_decimals = len(str(qty_step).split('.')[1]) if '.' in str(qty_step) else 0
            qty = round(qty, qty_decimals)
        if qty <= 0:
            continue

        if sym in TP_FAIL_BACKOFF and now < TP_FAIL_BACKOFF[sym]:
            continue

        result = place_take_profit(sym, idx, side, qty, price)
        if result is False:
            TP_FAIL_COUNT[sym] = TP_FAIL_COUNT.get(sym, 0) + 1
            fails = TP_FAIL_COUNT[sym]
            if fails >= TP_MAX_FAILS:
                TP_PERM_SKIP.add(sym)
                TP_PERM_SKIP_SIZES[sym] = pos_size
                _save_tp_skip()
                TP_FAIL_COUNT.pop(sym, None)
                TP_FAIL_BACKOFF.pop(sym, None)
                log_event(f'🔇 TP {sym}: PERM_SKIP (фейлов={fails}, time-decay 24ч)')
            else:
                delay = TP_FAIL_DELAYS[fails - 1]
                TP_FAIL_BACKOFF[sym] = now + delay
                log_event(f'❌ TP ошибка {sym}: {fails}/{TP_MAX_FAILS} фейлов, retry через {delay}с')
        else:
            TP_FAIL_COUNT.pop(sym, None)
            TP_FAIL_BACKOFF.pop(sym, None)
            TP_PERM_SKIP.discard(sym)
            _save_tp_skip()
