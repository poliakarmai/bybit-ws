"""SL Re-entry — лесенка лимиток после стоп-лосса.

Когда позиция закрывается по SL, монета часто продолжает падать.
Ставим 3 лимитки НИЖЕ уровня SL для ре-входа по лучшей цене.

Правила:
- Все Tier'ы (S/A/B/C/D) — автоперезаход после SL
- 3 лимитки: −5%, −10%, −15% от SL-цены
- Маржа: $10 каждая, плечо ×3
- Кулдаун: 4 часа на монету
- Не ставить при correlation_stop (>80% LONG)
- Только если монета не в позиции
"""

import json
import math
import os
import time
from datetime import datetime

from .api import bybit, get_bb_data
from .alerts import log_event, add_alert, _is_duplicate
from .config import Config
from .position_sizing import margin_for_strategy

SL_REENTRY_FILE = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.local', 'share', 'bybit-ws', 'sl_reentry.json')
# Fix path
SL_REENTRY_FILE = os.path.expanduser('~/.local/share/bybit-ws/sl_reentry.json')

# Tier A/B монеты (из scoring-system.md)
TIER_AB = {
    'SOLUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOTUSDT', 'LINKUSDT',
    'UNIUSDT', 'AVAXUSDT', 'SUIUSDT', 'NEARUSDT', 'APTUSDT',  # Tier A
    'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT', 'ONDOUSDT',
    'ENAUSDT', 'FETUSDT', 'WLDUSDT', 'ATOMUSDT', 'ALGOUSDT', 'RUNEUSDT',  # Tier B
}

# Параметры лесенки
REENTRY_LEVELS = [0.95, 0.90, 0.85]   # −5%, −10%, −15% от SL-цены
REENTRY_MARGIN = 10.0                   # $10 на лимитку
REENTRY_LEVERAGE = 3
REENTRY_COOLDOWN = 14400               # 4 часа
MAX_REENTRIES_PER_COIN = 2             # макс 2 лесенки на монету (после повторных SL)

# Известные one-way монеты
ONE_WAY = {'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT', 'AVAXUSDT', 'APTUSDT', 'SUIUSDT'}


def _load_state():
    try:
        if os.path.exists(SL_REENTRY_FILE):
            with open(SL_REENTRY_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(SL_REENTRY_FILE), exist_ok=True)
    with open(SL_REENTRY_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def notify_sl_hit(symbol, sl_price, entry_price):
    """Вызывается из main.py когда позиция закрыта по SL.
    Записывает монету в очередь на ре-вход."""
    state = _load_state()
    now = time.time()

    # Проверка кулдауна
    if symbol in state:
        last_ts = state[symbol].get('last_reentry_ts', 0)
        count = state[symbol].get('reentry_count', 0)
        if now - last_ts < REENTRY_COOLDOWN:
            log_event(f'🔕 SL re-entry: {symbol} кулдаун ({int((now - last_ts)/60)}мин из {REENTRY_COOLDOWN//60}мин)')
            return
        if count >= MAX_REENTRIES_PER_COIN:
            log_event(f'🔕 SL re-entry: {symbol} исчерпан лимит ({count}/{MAX_REENTRIES_PER_COIN})')
            return

    state[symbol] = {
        'sl_price': float(sl_price),
        'entry_price': float(entry_price),
        'sl_time': now,
        'sl_time_str': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'pending': True,
        'last_reentry_ts': state.get(symbol, {}).get('last_reentry_ts', 0),
        'reentry_count': state.get(symbol, {}).get('reentry_count', 0),
    }
    _save_state(state)
    log_event(f'📌 SL re-entry: {symbol} в очереди (SL=${sl_price:.4f}, Tier A/B)')


def check_sl_reentry(positions, correlation_stop=False):
    """Проверить очередь и выставить лимитки. Вызывается каждые 10 циклов."""
    if correlation_stop:
        # Rate-limit: не спамить каждый цикл
        if not _is_duplicate('SL re-entry blocked by correlation', 'STOP'):
            log_event('🔕 SL re-entry: correlation_stop активен')
        return []

    state = _load_state()
    if not state:
        return []

    actions = []
    now = time.time()
    active_syms = set(positions.keys()) if isinstance(positions, dict) else set()

    # Бан-лист из конфига
    try:
        cfg = Config()
        banned = set(cfg.risk.get('banned_symbols', []))
    except Exception:
        banned = set()

    for sym, info in list(state.items()):
        if sym in banned:
            info['pending'] = False
            state[sym] = info
            log_event(f'🚫 SL re-entry: {sym} в бане, пропущена')
            continue
        if not info.get('pending'):
            continue
        if sym in active_syms:
            # Монета уже в позиции — сбрасываем pending
            info['pending'] = False
            state[sym] = info
            log_event(f'🔕 SL re-entry: {sym} уже в позиции, пропущена')
            continue

        sl_price = info['sl_price']
        entry_price = info['entry_price']

        # Получаем текущую цену
        try:
            ticker_data = bybit('GET', f'/v5/market/tickers?category=linear&symbol={sym}')
            result = ticker_data.get('result', {})
            tickers = result.get('list', [])
            if not tickers:
                continue
            current = float(tickers[0].get('lastPrice', 0))
        except Exception as e:
            log_event(f'⚠️ SL re-entry {sym}: ошибка тикера — {e}')
            continue

        if current <= 0:
            continue

        # Проверяем что цена реально ушла ниже SL (иначе не ставим)
        if current > sl_price * 1.02:
            # Цена выше SL + 2% — отскок, неинтересно
            info['pending'] = False
            state[sym] = info
            log_event(f'🔕 SL re-entry: {sym} отскочила (${current:.4f} > SL ${sl_price:.4f})')
            continue

        # Считаем падение от входа для информации
        drop_from_entry = (1 - current / entry_price) * 100 if entry_price else 0

        # Формируем 3 лимитки
        qty_step = _get_lot_step(sym)
        orders_placed = 0
        for level_pct in REENTRY_LEVELS:
            price = round(sl_price * level_pct, 4)
            if price < 0.0001:
                continue
            price = _round_to_tick(price, sym)
            # Динамическая маржа для SL re-entry (score 6.5 — средняя уверенность после стопа)
            reentry_margin = margin_for_strategy('reentry', score=6.5)
            if reentry_margin <= 0:
                continue
            usdt_qty = reentry_margin * REENTRY_LEVERAGE
            qty = math.ceil(usdt_qty / price / qty_step) * qty_step
            qty = round(qty, _get_precision(qty_step))

            if qty <= 0:
                continue

            # Проверяем что лимитка реально ниже текущей цены
            if price >= current * 0.995:
                continue  # слишком близко к текущей — пропускаем этот уровень

            # Пробуем idx=0 первым, при 10001 → idx=1 (питфол #17)
            for pos_idx in (0, 1):
                try:
                    order = bybit('POST', '/v5/order/create', {
                        'category': 'linear',
                        'symbol': sym,
                        'side': 'Buy',
                        'orderType': 'Limit',
                        'qty': str(qty),
                        'price': str(price),
                        'positionIdx': pos_idx,
                        'timeInForce': 'GTC',
                    })
                    if order.get('retCode') == 0:
                        orders_placed += 1
                        log_event(f'📌 SL re-entry {sym}: лимитка ${price:.4f} ×{qty} (ур.{REENTRY_LEVELS.index(level_pct)+1}/3, idx={pos_idx})')
                        break
                    elif order.get('retCode') == 10001:
                        continue  # пробуем другой idx
                    else:
                        log_event(f'⚠️ SL re-entry {sym}: ошибка ${price:.4f} — {order.get("retMsg","?")}')
                        break
                except Exception as e:
                    log_event(f'⚠️ SL re-entry {sym}: исключение — {e}')
                    break

        if orders_placed > 0:
            add_alert('ENTRY', f'📌 SL re-entry {sym}: {orders_placed} лимиток ниже SL (падение {drop_from_entry:.0f}% от входа)')
            info['pending'] = False
            info['last_reentry_ts'] = now
            info['reentry_count'] = info.get('reentry_count', 0) + 1
            state[sym] = info
            actions.append(sym)
        else:
            # Не смогли поставить — оставляем в очереди до следующего цикла
            log_event(f'⏳ SL re-entry {sym}: не удалось поставить лимитки, ждём')

    _save_state(state)
    return actions


def _get_lot_step(sym):
    """Шаг лота для монеты."""
    try:
        data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
        instruments = data.get('result', {}).get('list', [])
        if instruments:
            return float(instruments[0].get('lotSizeFilter', {}).get('qtyStep', 0.1))
    except Exception:
        pass
    return 0.1


def _round_to_tick(price, sym):
    """Округлить до шага тика."""
    tick_size = 0.01
    if price < 1:
        tick_size = 0.0001
    elif price < 10:
        tick_size = 0.001
    elif price < 100:
        tick_size = 0.01
    elif price < 1000:
        tick_size = 0.1
    else:
        tick_size = 1
    return round(price / tick_size) * tick_size


def _get_precision(step):
    """Количество знаков после запятой для шага."""
    if step >= 1:
        return 0
    s = str(step)
    if '.' in s:
        return len(s.split('.')[1])
    return 0
