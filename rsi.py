"""RSI-дивергенция: медвежья дивергенция на Daily = сигнал фиксации/SHORT.

Логика: цена делает более высокий максимум, а RSI(14) — более низкий.
Это классический разворотный сигнал.
"""

import os, time, json
from . import DATA_DIR
from .api import bybit, get_bb_data

RSI_PERIOD = 14
DIVERGENCE_STATE_FILE = os.path.join(DATA_DIR, 'rsi_divergence.json')
ALERT_COOLDOWN = 86400  # не чаще раза в сутки на монету


def _load_state():
    if os.path.exists(DIVERGENCE_STATE_FILE):
        try:
            with open(DIVERGENCE_STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_state(state):
    with open(DIVERGENCE_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _calc_rsi(closes):
    """RSI(14) по списку закрытий (от старых к новым)."""
    if len(closes) < RSI_PERIOD + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def check_rsi_divergence():
    """Проверить топ-30 монет на медвежью RSI-дивергенцию на Daily."""
    alerts = []
    state = _load_state()
    now = time.time()

    # Берём топ-30 по обороту
    data = bybit('GET', '/v5/market/tickers?category=linear')
    if not data or data.get('retCode') != 0:
        return alerts

    tickers = data['result'].get('list', [])
    tickers.sort(key=lambda t: float(t.get('turnover24h', 0) or 0), reverse=True)
    top = [t['symbol'] for t in tickers[:30] if t['symbol'].endswith('USDT')]

    for sym in top:
        # Пропускаем стейблкоины
        if 'USD' not in sym or not sym.endswith('USDT'):
            continue

        # Загружаем 30 дневных свечей
        kline = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=30')
        if not kline or kline.get('retCode') != 0:
            continue
        candles = kline['result'].get('list', [])
        if len(candles) < 20:
            continue

        # Свечи от старых к новым (API возвращает от новых)
        candles = candles[::-1]
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]

        # Ищем два последних пика (5-свечной максимум, разделённые минимум 3 свечами)
        # peak = локальный максимум среди ±2 свечей
        peaks = []
        for i in range(2, len(closes) - 2):
            if highs[i] > max(highs[i-2], highs[i-1], highs[i+1], highs[i+2]):
                peaks.append((i, highs[i], closes[i]))

        if len(peaks) < 2:
            continue

        # Последние два пика
        p2_idx, p2_high, p2_close = peaks[-1]
        p1_idx, p1_high, p1_close = peaks[-2]

        # Проверка: цена выше, а RSI ниже
        if p2_high <= p1_high:
            continue
        if p2_idx - p1_idx < 3:
            continue

        # RSI на каждом пике (берём окно до пика)
        rsi1 = _calc_rsi(closes[max(0, p1_idx-RSI_PERIOD-1):p1_idx+1])
        rsi2 = _calc_rsi(closes[max(0, p2_idx-RSI_PERIOD-1):p2_idx+1])
        if rsi1 is None or rsi2 is None:
            continue

        if rsi2 < rsi1:  # медвежья дивергенция!
            st = state.get(sym, {})
            last_alert = st.get('last_alert', 0)
            if now - last_alert < ALERT_COOLDOWN:
                continue

            # Дополнительно: проверяем BB для контекста
            bb = get_bb_data(sym, 'D')
            bb_pos = bb['bb_pos'] if bb else 50

            state[sym] = {'last_alert': now, 'rsi': rsi2, 'bb_pos': bb_pos}
            alerts.append(
                f'🐻 RSI дивергенция {sym}: цена выше (${p2_high:.4f} > ${p1_high:.4f}), '
                f'RSI ниже ({rsi2:.1f} < {rsi1:.1f}) — МЕДВЕЖИЙ сигнал! '
                f'BB: {bb_pos:.0f}%'
            )

    _save_state(state)
    return alerts
