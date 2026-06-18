"""
Correlation risk matrix for bybit-ws.
Calculates 24h price correlation between all symbols currently in positions
using hourly klines from Bybit API. Flags pairs with >0.8 correlation as
concentration risk.
"""

import json
import math
import os
import re
import time
from datetime import datetime

from . import DATA_DIR
from .api import bybit, place_stop_loss

CORRELATION_SNAPSHOT = os.path.join(DATA_DIR, 'correlation.json')
CORRELATION_THRESHOLD = 0.80
MIN_CANDLES = 12  # minimum overlapping candles for valid correlation


def fetch_klines(symbol, interval='60', limit=24):
    """Fetch kline close prices for a symbol.

    Returns list of float close prices (chronological order), or None on failure.
    """
    path = f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}'
    data = bybit('GET', path)
    if not data or data.get('retCode') != 0:
        return None
    try:
        candles = data['result']['list']
        if not candles:
            return None
        # API returns newest-first; reverse to chronological, extract close (index 4)
        closes = [float(c[4]) for c in reversed(candles)]
        return closes
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def pearson_r(x, y):
    """Calculate Pearson correlation coefficient between two equal-length lists."""
    n = len(x)
    if n != len(y) or n < 2:
        return 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    sum_y2 = sum(yi * yi for yi in y)

    numerator = n * sum_xy - sum_x * sum_y
    denom_x = n * sum_x2 - sum_x * sum_x
    denom_y = n * sum_y2 - sum_y * sum_y
    denominator = math.sqrt(denom_x * denom_y)

    if denominator == 0:
        return 0.0

    return numerator / denominator


def price_returns(prices):
    """Convert price series to log returns for correlation."""
    if len(prices) < 2:
        return []
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def check_correlation(positions):
    """Calculate 24h price correlations between all position symbols.

    Args:
        positions: dict of {symbol: position_data} from fetch_positions()

    Returns:
        dict with:
            - 'pairs': list of (sym1, sym2, correlation) for all computed pairs
            - 'flagged': list of (sym1, sym2, correlation) for pairs > threshold
            - 'messages': list of warning strings for the main loop
            - 'timestamp': ISO timestamp of computation
            - 'position_count': number of symbols analyzed
    """
    if not positions or len(positions) < 2:
        return {
            'pairs': [],
            'flagged': [],
            'messages': [],
            'timestamp': datetime.now().isoformat(),
            'position_count': len(positions) if positions else 0,
        }

    symbols = list(positions.keys())

    # Fetch klines for all symbols
    prices = {}
    for sym in symbols:
        closes = fetch_klines(sym, interval='60', limit=24)
        if closes and len(closes) >= MIN_CANDLES:
            prices[sym] = closes

    if len(prices) < 2:
        return {
            'pairs': [],
            'flagged': [],
            'messages': [],
            'timestamp': datetime.now().isoformat(),
            'position_count': len(positions),
            'symbols_fetched': len(prices),
        }

    # Compute correlations for all pairs
    syms = sorted(prices.keys())
    pairs = []
    flagged = []
    messages = []

    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            s1, s2 = syms[i], syms[j]
            # Use the shorter of the two series
            p1 = prices[s1]
            p2 = prices[s2]
            # Ensure same length (truncate to shorter)
            min_len = min(len(p1), len(p2))
            r1 = price_returns(p1[:min_len])
            r2 = price_returns(p2[:min_len])

            if len(r1) < 2:
                continue

            corr = pearson_r(r1, r2)
            # Clamp to [-1, 1] to avoid floating-point noise
            corr = max(-1.0, min(1.0, corr))

            pairs.append((s1, s2, round(corr, 4)))

            if abs(corr) > CORRELATION_THRESHOLD:
                flagged.append((s1, s2, round(corr, 4)))
                direction = '📈📈' if corr > 0 else '📈📉'

                # Build actionable alert with position context
                pos1 = positions.get(s1, {})
                pos2 = positions.get(s2, {})
                side1 = '🔴 SHORT' if pos1.get('side') == 'Sell' else '🟢 LONG'
                side2 = '🔴 SHORT' if pos2.get('side') == 'Sell' else '🟢 LONG'
                size1 = pos1.get('size', '?')
                size2 = pos2.get('size', '?')

                # Determine risk severity and action
                both_same_side = pos1.get('side') == pos2.get('side')
                if corr > 0:
                    if both_same_side:
                        risk_msg = 'Двигаются синхронно в одну сторону — при развороте двойной убыток'
                        action = 'Закрой одну или ужесточи SL на обеих'
                    else:
                        risk_msg = 'Двигаются синхронно но в разных позах — частичный хедж'
                        action = 'Проверь что размер хеджа сбалансирован'
                else:
                    risk_msg = 'Обратная корреляция — естественный хедж'
                    action = 'Ок, но проверь что нет перекоса размеров'

                messages.append(
                    f'⚠️ Корреляция {direction} {s1}↔{s2}: r={corr:+.3f} (>±{CORRELATION_THRESHOLD})\n'
                    f'   {s1} {side1} ×{size1} | {s2} {side2} ×{size2}\n'
                    f'   {risk_msg}\n'
                    f'   → {action}'
                )

    # ── Batching: группировка по общему символу ──
    # Если один символ коррелирует с 3+ другими → один алерт вместо N
    if len(messages) >= 3:
        from collections import defaultdict
        sym_pairs = defaultdict(list)
        for msg in messages:
            m = re.search(r'(\w+USDT)↔(\w+USDT)', msg)
            if m:
                sym_pairs[m.group(1)].append((m.group(2), msg))
                sym_pairs[m.group(2)].append((m.group(1), msg))

        batched = {}
        for sym, pairs_list in sym_pairs.items():
            if len(pairs_list) >= 3:
                # Собираем все корреляции с этим символом
                others = [p[0] for p in pairs_list]
                # Берём позицию общего символа
                pos = positions.get(sym, {})
                side = '🔴 SHORT' if pos.get('side') == 'Sell' else '🟢 LONG'
                size = pos.get('size', '?')
                # Диапазон r
                r_vals = [float(re.search(r'r=([+\-]\d+\.\d+)', p[1]).group(1)) for p in pairs_list]
                r_min, r_max = min(r_vals), max(r_vals)
                r_range = f'{r_min:+.2f}..{r_max:+.2f}' if r_min != r_max else f'{r_min:+.2f}'
                batched[sym] = (
                    f'⚠️ Корреляция 📈📈 {sym} ↔ {", ".join(others)}: r={r_range}\n'
                    f'   {sym} {side} ×{size} | {len(others)} коррелирующих позиций\n'
                    f'   Двигаются синхронно — при развороте многократный убыток\n'
                    f'   → Закрой лишние или ужесточи SL на всех'
                )
                # Удаляем индивидуальные сообщения для этих пар
                for _, orig_msg in pairs_list:
                    if orig_msg in messages:
                        messages.remove(orig_msg)

        # Добавляем батчированные сообщения
        for batch_msg in batched.values():
            messages.append(batch_msg)

    # Save snapshot for dashboard
    result = {
        'pairs': pairs,
        'flagged': flagged,
        'messages': messages,
        'timestamp': datetime.now().isoformat(),
        'position_count': len(positions),
        'symbols_fetched': len(prices),
        'threshold': CORRELATION_THRESHOLD,
    }

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CORRELATION_SNAPSHOT + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        os.replace(tmp, CORRELATION_SNAPSHOT)
    except (IOError, OSError):
        pass

    return result


def load_correlation_snapshot():
    """Load the last correlation snapshot from disk. Returns dict or None."""
    if not os.path.exists(CORRELATION_SNAPSHOT):
        return None
    try:
        with open(CORRELATION_SNAPSHOT) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def tighten_correlation_sl(positions, flagged_pairs, dedup_state=None):
    """Ужесточить SL на 1% ближе к марку для позиций с высокой корреляцией.

    MONITOR.md §4: при r > ±0.8 SL поджимается на 1% ближе к текущей цене.
    Для LONG:  new_sl = sl + 0.01 * (mark - sl)
    Для SHORT: new_sl = sl - 0.01 * (sl - mark)

    Args:
        positions: dict {symbol: position_data} из fetch_positions()
        flagged_pairs: list of (sym1, sym2, corr) с |corr| > 0.8
        dedup_state: dict для дедупликации (12ч кулдаун на пару)

    Returns:
        list of alert strings
    """
    alerts = []
    if not flagged_pairs or not positions:
        return alerts

    if dedup_state is None:
        dedup_state = {}

    now = time.time()
    CORR_SL_COOLDOWN = 43200  # 12 часов между ужесточениями на пару
    CORR_SL_STEP = 0.01       # 1% шаг к марку

    for sym1, sym2, corr in flagged_pairs:
        # Дедупликация: не чаще раза в 12ч на пару
        pair_key = f"{min(sym1, sym2)}_{max(sym1, sym2)}"
        last = dedup_state.get(pair_key, 0)
        if now - last < CORR_SL_COOLDOWN:
            continue

        p1 = positions.get(sym1)
        p2 = positions.get(sym2)
        if not p1 or not p2:
            continue

        # Ужесточаем только если оба в одну сторону (синхронный риск)
        if p1['side'] != p2['side']:
            continue

        for sym, p in [(sym1, p1), (sym2, p2)]:
            current_sl = p.get('stopLoss')
            if not current_sl or current_sl <= 0:
                continue

            mark = p['mark']
            side = p['side']

            if side == 'Buy':  # LONG
                if current_sl >= mark:
                    continue  # SL уже выше/равен марку — некуда двигать
                new_sl = current_sl + CORR_SL_STEP * (mark - current_sl)
            else:  # SHORT
                if current_sl <= mark:
                    continue
                new_sl = current_sl - CORR_SL_STEP * (current_sl - mark)

            # Защита: не двигаем SL за марк
            if side == 'Buy' and new_sl > mark:
                new_sl = mark * 0.999
            elif side == 'Sell' and new_sl < mark:
                new_sl = mark * 1.001

            # Округляем до тика
            new_sl = round(new_sl, 4)

            # Отправляем на биржу
            try:
                place_stop_loss(
                    sym, p.get('positionIdx', 0),
                    'Sell' if side == 'Buy' else 'Buy',
                    p['size'], new_sl
                )
                alerts.append(
                    f'🔒 Корреляция SL {sym}: ${current_sl:.4f} → ${new_sl:.4f} '
                    f'(r={corr:+.3f} с {sym1 if sym != sym1 else sym2}, '
                    f'дист. до марка {abs(mark - new_sl) / mark * 100:.2f}%)'
                )
            except Exception as e:
                alerts.append(f'⚠️ Корреляция SL {sym}: ошибка — {e}')

        dedup_state[pair_key] = now

    return alerts
