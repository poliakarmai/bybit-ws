"""Авто-фандинг-ротация: перекладка позиций при невыгодном фандинге.

Стратегия: если позиция платит фандинг (лонг при +ставке, шорт при -ставке),
и есть альтернативный сигнал с выгодным фандингом — ротируем.

Пороги:
- Ротация LONG: funding > +0.01% → ищем LONG с funding < -0.01%
- Ротация SHORT: funding < -0.01% → ищем SHORT с funding > +0.01%
- Мин. улучшение фандинга: 0.03% (разница ставок)
- Кулдаун на монету: 24 часа (после ротации)
- Макс. ротаций в день: 3
"""

import json
import math
import os
import time
from datetime import datetime

from .api import bybit
from .alerts import log_event, add_alert
from .manual_positions import is_manual_position
from .position_sizing import margin_for_strategy

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
ROTATION_STATE_FILE = os.path.join(DATA_DIR, 'funding_rotation_state.json')

# Пороги
ROTATION_COOLDOWN = 86400       # 24 часа на монету после ротации
MAX_ROTATIONS_PER_DAY = 3
FUNDING_DELTA_MIN = 0.0003      # мин. улучшение ставки (0.03%)

# Тир для ротации — Tier A/B ликвидные
ROTATION_TIERS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT',
    'ADAUSDT', 'DOTUSDT', 'LTCUSDT', 'XRPUSDT', 'UNIUSDT',
    'NEARUSDT', 'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT',
    'SUIUSDT', 'ATOMUSDT', 'FETUSDT', 'RUNEUSDT', 'WLDUSDT',
    'ENAUSDT', 'ALGOUSDT',
}


def _load_state():
    try:
        if os.path.exists(ROTATION_STATE_FILE):
            with open(ROTATION_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log_event(f'⚠️ funding_rotation state: {e}')
    return {'cooldowns': {}, 'daily_count': 0, 'last_reset': ''}


def _save_state(state):
    os.makedirs(os.path.dirname(ROTATION_STATE_FILE), exist_ok=True)
    with open(ROTATION_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _reset_daily(state):
    today = datetime.now().strftime('%Y-%m-%d')
    if state.get('last_reset') != today:
        state['daily_count'] = 0
        state['last_reset'] = today


def _get_funding_map():
    """Получить словарь {symbol: funding_rate} для всех тикеров."""
    data = bybit('GET', '/v5/market/tickers?category=linear')
    if not data or data.get('retCode') != 0:
        return {}
    tickers = data.get('result', {}).get('list', [])
    fmap = {}
    for t in tickers:
        sym = t.get('symbol', '')
        fr = t.get('fundingRate', '')
        if sym and fr:
            try:
                fmap[sym] = float(fr)
            except ValueError:
                continue
    return fmap


def _get_bb_signal(sym):
    """Получить Daily BB% для символа. Возвращает bb_pct или None."""
    try:
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=20')
        closes = [float(c[4]) for c in kline.get('result', {}).get('list', [])]
        if len(closes) < 5:
            return None
        sma = sum(closes) / len(closes)
        variance = sum((c - sma) ** 2 for c in closes) / len(closes)
        std = math.sqrt(variance)
        lower = sma - 2 * std
        upper = sma + 2 * std
        if upper == lower:
            return None
        bb_pct = (closes[0] - lower) / (upper - lower) * 100
        return {
            'bb_pct': round(bb_pct, 1),
            'lower': round(lower, 8),
            'upper': round(upper, 8),
            'price': closes[0],
        }
    except Exception:
        return None


def check_funding_rotation(positions) -> list[dict]:
    """Проверить все позиции на невыгодный фандинг и предложить ротации.

    Возвращает список действий (пока только алерты, без авто-исполнения).
    Каждое действие: {'action': 'rotate', 'from': sym, 'to': sym, 'reason': str}
    """
    if not positions:
        return []

    state = _load_state()
    _reset_daily(state)

    if state['daily_count'] >= MAX_ROTATIONS_PER_DAY:
        return []

    now = time.time()
    actions = []

    # Получаем текущий фандинг для всех тикеров
    fmap = _get_funding_map()
    if not fmap:
        return []

    # Собираем позиции с невыгодным фандингом
    bad_positions = []
    for sym, p in positions.items():
        if is_manual_position(sym):
            continue
        if sym not in fmap:
            continue

        funding = fmap[sym]
        side = p.get('side', '')
        size = p.get('size', 0)
        if size <= 0:
            continue

        # Проверяем кулдаун
        cooldown_until = state['cooldowns'].get(sym, 0)
        if now < cooldown_until:
            continue

        is_bad = False
        reason = ''
        if side == 'Buy' and funding > 0.0001:   # > 0.01% — лонгист платит
            is_bad = True
            reason = f'LONG платит фандинг {funding * 100:.3f}%'
        elif side == 'Sell' and funding < -0.0001:  # < -0.01% — шортист платит
            is_bad = True
            reason = f'SHORT платит фандинг {funding * 100:.3f}%'

        if is_bad:
            bad_positions.append({
                'symbol': sym,
                'side': side,
                'funding': funding,
                'reason': reason,
                'size': size,
            })

    if not bad_positions:
        return actions

    # Для каждой плохой позиции ищем альтернативу
    for bp in bad_positions:
        sym = bp['symbol']
        side = bp['side']
        current_fr = bp['funding']
        best_candidate = None
        best_score = -999

        for candidate in ROTATION_TIERS:
            if candidate == sym:
                continue
            if candidate in positions:
                continue
            if candidate not in fmap:
                continue

            cand_fr = fmap[candidate]

            # LONG → ищем отрицательный фандинг (шортисты платят лонгистам)
            if side == 'Buy' and cand_fr >= -0.0001:
                continue
            # SHORT → ищем положительный фандинг (лонгисты платят шортистам)
            if side == 'Sell' and cand_fr <= 0.0001:
                continue

            # Улучшение фандинга
            if side == 'Buy':
                delta = current_fr - cand_fr  # чем отрицательнее cand_fr, тем лучше
            else:
                delta = cand_fr - current_fr  # чем положительнее cand_fr, тем лучше

            if delta < FUNDING_DELTA_MIN:
                continue

            # BB-проверка
            bb = _get_bb_signal(candidate)
            if bb is None:
                continue

            # Скоринг: delta фандинга + BB-позиция
            score = delta * 10000  # base: разница фандинга
            if side == 'Buy' and bb['bb_pct'] < 20:
                score += 3  # хороший LONG-сигнал
            elif side == 'Sell' and bb['bb_pct'] > 80:
                score += 3  # хороший SHORT-сигнал

            if score > best_score:
                best_score = score
                best_candidate = {
                    'symbol': candidate,
                    'funding': cand_fr,
                    'bb_pct': bb['bb_pct'],
                    'price': bb['price'],
                    'delta': delta,
                }

        if best_candidate:
            actions.append({
                'from': bp['symbol'],
                'to': best_candidate['symbol'],
                'side': side,
                'current_funding': round(current_fr * 100, 4),
                'new_funding': round(best_candidate['funding'] * 100, 4),
                'delta': round(best_candidate['delta'] * 100, 4),
                'bb_pct': best_candidate['bb_pct'],
                'price': best_candidate['price'],
            })

    return actions


def execute_rotation(rotation: dict, positions: dict) -> bool:
    """Исполнить ротацию: открыть to + SL, затем закрыть from.

    Порядок: сначала открываем новую → ставим SL → закрываем старую.
    При сбое на любом шаге — rollback: закрываем новую, старую оставляем.
    """
    from_sym = rotation['from']
    to_sym = rotation['to']
    side = rotation['side']

    if from_sym not in positions:
        log_event(f'⚠️ Ротация {from_sym}→{to_sym}: исходная позиция не найдена')
        return False

    p = positions[from_sym]
    size = p.get('size', 0)
    if size <= 0:
        return False

    try:
        # 1. Открываем новую позицию (СНАЧАЛА!)
        qty = str(size)
        new_result = bybit('POST', '/v5/order/create', {
            'category': 'linear',
            'symbol': to_sym,
            'side': side,
            'orderType': 'Market',
            'qty': qty,
            'positionIdx': 0,
        })
        if new_result.get('retCode') != 0:
            log_event(f'❌ Ротация: не удалось открыть {to_sym}: {new_result.get("retMsg")}')
            return False  # старая позиция НЕ тронута

        log_event(f'🔄 Ротация: открыт {to_sym} ({side}, {size} контрактов)')

        # 2. Немедленно ставим SL на новую позицию
        sl_price = _calc_rotation_sl(rotation, side)
        from .api import place_stop_loss
        sl_ok = place_stop_loss(to_sym, 0, side, size, sl_price)
        if not sl_ok:
            log_event(f'⚠️ Ротация: SL не выставлен на {to_sym}, закрываю новую')
            # Rollback: закрываем новую
            close_new_side = 'Sell' if side == 'Buy' else 'Buy'
            bybit('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': to_sym,
                'side': close_new_side,
                'orderType': 'Market',
                'qty': qty,
                'positionIdx': 0,
                'reduceOnly': True,
            })
            return False

        # 3. Закрываем старую позицию
        close_side = 'Sell' if side == 'Buy' else 'Buy'
        idx = p.get('positionIdx', 0)
        result = bybit('POST', '/v5/order/create', {
            'category': 'linear',
            'symbol': from_sym,
            'side': close_side,
            'orderType': 'Market',
            'qty': str(size),
            'positionIdx': idx,
            'reduceOnly': True,
        })
        if result.get('retCode') != 0:
            log_event(f'⚠️ Ротация: не удалось закрыть {from_sym}: {result.get("retMsg")}')
            # Новая уже открыта и со SL — оставляем, старая висит (дублирование exposure)
            # Это лучше чем потеря обеих позиций
            add_alert('STOP', f'⚠️ Ротация {from_sym}→{to_sym}: дублирование! Старая не закрыта.')
            return False

        log_event(
            f'✅ Ротация {from_sym}→{to_sym}: '
            f'фандинг {rotation["current_funding"]}%→{rotation["new_funding"]}% '
            f'(Δ{rotation["delta"]}%), BB={rotation["bb_pct"]}%, SL={sl_price:.4f}'
        )

        # Обновляем состояние
        state = _load_state()
        state['cooldowns'][from_sym] = time.time() + ROTATION_COOLDOWN
        state['cooldowns'][to_sym] = time.time() + ROTATION_COOLDOWN
        state['daily_count'] = state.get('daily_count', 0) + 1
        _save_state(state)

        add_alert(
            'INFO',
            f'🔄 Ротация {from_sym}→{to_sym}: '
            f'фандинг {rotation["current_funding"]}%→{rotation["new_funding"]}%'
        )
        return True

    except Exception as e:
        log_event(f'❌ Ротация {from_sym}→{to_sym}: {e}')
        return False


def _calc_rotation_sl(rotation: dict, side: str) -> float:
    """Рассчитать SL для новой позиции после ротации: 2% от цены входа."""
    price = rotation.get('price', 0)
    if price <= 0:
        return 0.0
    if side == 'Buy':
        return round(price * 0.98, 4)  # -2% SL
    else:
        return round(price * 1.02, 4)  # +2% SL
