"""BB-сжатие + объёмный всплеск: предвестник прорыва.

Когда полосы Боллинджера сужаются до <5% ширины и объём внезапно ×5 от среднего —
это верный признак, что через 5-15 минут будет сильное движение.
"""

import os, time, json
from . import DATA_DIR
from .api import bybit

SQUEEZE_STATE_FILE = os.path.join(DATA_DIR, 'squeeze.json')
BB_WIDTH_THRESHOLD = 5.0    # % ширины BB
VOL_SPIKE_MULT = 5.0        # во сколько раз объём выше среднего
ALERT_COOLDOWN = 3600       # не чаще раза в час на монету


def _load_state():
    if os.path.exists(SQUEEZE_STATE_FILE):
        try:
            with open(SQUEEZE_STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_state(state):
    with open(SQUEEZE_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def check_squeeze():
    """Проверить топ-40 монет на BB-сжатие + объёмный всплеск на 15-минутках."""
    alerts = []
    state = _load_state()
    now = time.time()

    # Берём топ-40 по обороту
    data = bybit('GET', '/v5/market/tickers?category=linear')
    if not data or data.get('retCode') != 0:
        return alerts

    tickers = data['result'].get('list', [])
    tickers.sort(key=lambda t: float(t.get('turnover24h', 0) or 0), reverse=True)
    top = [t['symbol'] for t in tickers[:40] if t['symbol'].endswith('USDT')]

    for sym in top:
        # Загружаем 30 свечей 15-минуток
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=15&limit=30')
        if not kline or kline.get('retCode') != 0:
            continue
        candles = kline['result'].get('list', [])
        if len(candles) < 20:
            continue

        # Свечи от старых к новым
        candles = candles[::-1]
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        # BB(20,2) на 15-минутках
        n = 20
        closes_bb = closes[-n:]
        if len(closes_bb) < n:
            continue
        import math
        sma = sum(closes_bb) / n
        variance = sum((x - sma) ** 2 for x in closes_bb) / n
        std = math.sqrt(variance)
        upper = sma + 2 * std
        lower = sma - 2 * std
        bb_width = (upper - lower) / sma * 100 if sma > 0 else 100

        if bb_width > BB_WIDTH_THRESHOLD:
            continue

        # Средний объём за последние 15 свечей (без текущей)
        avg_vol = sum(volumes[-16:-1]) / 15 if len(volumes) >= 16 else sum(volumes[:-1]) / max(1, len(volumes)-1)
        cur_vol = volumes[-1]

        if avg_vol <= 0 or cur_vol <= 0:
            continue

        vol_ratio = cur_vol / avg_vol
        if vol_ratio < VOL_SPIKE_MULT:
            continue

        # Дедупликация
        st = state.get(sym, {})
        last_alert = st.get('last_alert', 0)
        if now - last_alert < ALERT_COOLDOWN:
            continue

        # Направление: куда идёт объём — вверх или вниз?
        price_chg = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        direction = '🟢 ВВЕРХ' if price_chg > 0 else '🔴 ВНИЗ'

        state[sym] = {'last_alert': now, 'bb_width': bb_width, 'vol_ratio': vol_ratio}

        alerts.append(
            f'💥 СЖАТИЕ {sym}: BB ширина {bb_width:.1f}%, '
            f'объём ×{vol_ratio:.1f} от среднего — ПРОРЫВ {direction}! '
            f'Цена ${closes[-1]:.4f}'
        )

    _save_state(state)
    return alerts
