"""Auto-TP с retry с exponential backoff."""
import time, math
from datetime import datetime
from . import TP_FAIL_COUNT, TP_FAIL_BACKOFF, TP_FAIL_DELAYS, TP_MAX_FAILS, TP_PERM_SKIP, TP_PERM_SKIP_SIZES, TP_SKIP_FILE, DATA_DIR
from .api import bybit, get_bb_data
from .alerts import log_event

import json, os

def _load_tp_skip():
    """Загрузить персистентный PERM_SKIP с диска."""
    try:
        if os.path.exists(TP_SKIP_FILE):
            with open(TP_SKIP_FILE) as f:
                data = json.load(f)
            for sym in data.get('skip', []):
                TP_PERM_SKIP.add(sym)
            TP_PERM_SKIP_SIZES.update(data.get('sizes', {}))
    except Exception:
        pass

def _save_tp_skip():
    """Сохранить PERM_SKIP на диск."""
    try:
        with open(TP_SKIP_FILE, 'w') as f:
            json.dump({'skip': list(TP_PERM_SKIP), 'sizes': TP_PERM_SKIP_SIZES}, f)
    except Exception:
        pass

# Загружаем при импорте
_load_tp_skip()

def auto_take_profit(positions, orders, skip_syms=None):
    """Поставить TP 20% на Middle BB + 80% на Upper BB. Округляет qty до шага лота."""
    skip_syms = skip_syms or set()
    existing_tp = {}
    for o in orders.values():
        if o['kind'] == 'TP' and o['status'] in ('New', 'PartiallyFilled', 'Untriggered'):
            sym = o['symbol']
            if sym not in existing_tp:
                existing_tp[sym] = []
            existing_tp[sym].append((float(o.get('qty', 0)), float(o.get('price', 0))))

    actions = []
    for sym, p in positions.items():
        # Перманентный скип: проверяем, не выросла ли позиция
        if sym in TP_PERM_SKIP:
            prev_size = TP_PERM_SKIP_SIZES.get(sym, 0)
            if p['size'] > prev_size * 1.2:  # выросла на 20%+
                TP_PERM_SKIP.discard(sym)
                TP_PERM_SKIP_SIZES.pop(sym, None)
                _save_tp_skip()
                log_event(f'♻️ TP {sym}: позиция выросла {prev_size:.1f}->{p["size"]:.1f}, снимаю перманентный скип')
            else:
                continue
        if sym in skip_syms:
            continue
        if p['side'] != 'Buy' or p['size'] <= 0:
            continue
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        middle, upper, cur = bb['middle'], bb['upper'], bb['cur']
        pos_size = p['size']
        # Округляем до шага лота: для позиций < 10 шт используем 1 знак после запятой
        rounding = 0 if pos_size >= 10 else 1
        need_mid = round(pos_size * 0.2, rounding)
        need_up = round(pos_size * 0.8, rounding)
        # Если Mid-часть < 1 → пропускаем Middle, всё на Upper
        if need_mid < 0.5:
            need_up = round(pos_size, rounding)
            need_mid = 0

        existing = existing_tp.get(sym, [])
        has_mid = sum(q for q, pr in existing if abs(pr - middle) / middle < 0.02) if middle > 0 else 0
        has_up = sum(q for q, pr in existing if abs(pr - upper) / upper < 0.02) if upper > 0 else 0

        if need_mid > 0 and middle > cur and has_mid < need_mid * 0.9:
            gap = round(need_mid - has_mid, rounding)
            if gap > 0:
                actions.append((sym, p['positionIdx'], p['side'], gap, middle, pos_size))

        if upper > cur and has_up < need_up * 0.9:
            gap = round(need_up - has_up, rounding)
            if gap > 0:
                actions.append((sym, p['positionIdx'], p['side'], gap, upper, pos_size))

    return actions

def apply_auto_tp(actions):
    """Применить auto-TP с retry backoff. Маленький qty → перманентный скип."""
    from .api import place_take_profit
    now = time.time()

    for sym, idx, side, qty, price, pos_size in actions:
        # Очевидно слишком маленький qty — даже не пробуем
        if qty < 0.5:
            TP_FAIL_COUNT.pop(sym, None)
            TP_FAIL_BACKOFF.pop(sym, None)
            continue

        if sym in TP_FAIL_BACKOFF and now < TP_FAIL_BACKOFF[sym]:
            continue

        result = place_take_profit(sym, idx, side, qty, price)
        if result is False:
            TP_FAIL_COUNT[sym] = TP_FAIL_COUNT.get(sym, 0) + 1
            fails = TP_FAIL_COUNT[sym]
            if fails >= TP_MAX_FAILS:
                # Перманентный скип — qty меньше минимального лота, позиция не растёт
                TP_PERM_SKIP.add(sym)
                TP_PERM_SKIP_SIZES[sym] = pos_size
                _save_tp_skip()
                TP_FAIL_COUNT.pop(sym, None)
                TP_FAIL_BACKOFF.pop(sym, None)
                log_event(f'🔇 TP {sym}: перманентный скип (qty={qty:.1f} < мин. лота, жду докупки)')
            else:
                delay = TP_FAIL_DELAYS[fails - 1]
                TP_FAIL_BACKOFF[sym] = now + delay
                log_event(f'❌ TP ошибка {sym}: {fails}/{TP_MAX_FAILS} фейлов, retry через {delay}с')
        else:
            TP_FAIL_COUNT.pop(sym, None)
            TP_FAIL_BACKOFF.pop(sym, None)
            # Если TP встал — убираем из перманентного скипа (на случай докупки)
            TP_PERM_SKIP.discard(sym)
            _save_tp_skip()
