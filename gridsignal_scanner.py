#!/usr/bin/env python3
"""
GridSignal Scanner v4.0 — модуль скоринга для Telegram-бота.
v4.0: SHORT-режим, честный Multi-TF (D/5/3), RSI(14) метрика.
Полный 9-метричный анализ топ-50 монет, возвращает топ-5 сигналов
для стратегии Bollinger Grid (LONG + SHORT).

Использует bybit CLI (ключи из ~/.config/bybit-cli/config).
Основной монитор (bybit-ws-async) использует api.py → env-ключи (EnvironmentFile).
"""

import json
import subprocess
import math
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# TIER MAP
# ═══════════════════════════════════════════════════════════════
TIER_MAP = {
    'BTCUSDT': 'S', 'ETHUSDT': 'S',
    'SOLUSDT': 'A', 'LTCUSDT': 'A', 'XRPUSDT': 'A', 'ADAUSDT': 'A',
    'DOTUSDT': 'A', 'LINKUSDT': 'A', 'UNIUSDT': 'A', 'AVAXUSDT': 'A',
    'SUIUSDT': 'A', 'NEARUSDT': 'A', 'APTUSDT': 'A',
    'ARBUSDT': 'B', 'OPUSDT': 'B', 'AAVEUSDT': 'B', 'INJUSDT': 'B',
    'ONDOUSDT': 'B', 'ENAUSDT': 'B', 'FETUSDT': 'B', 'WLDUSDT': 'B',
    'ATOMUSDT': 'B', 'ALGOUSDT': 'B', 'RUNEUSDT': 'B',
    'ACHUSDT': 'C', 'ASTERUSDT': 'C', 'TONUSDT': 'C', 'HYPEUSDT': 'C',
    'ZECUSDT': 'C', 'BCHUSDT': 'C', 'EGLDUSDT': 'C', 'VETUSDT': 'C',
    'FILUSDT': 'C', 'SANDUSDT': 'C', 'MANAUSDT': 'C', 'GALAUSDT': 'C',
    'DOGEUSDT': 'D', 'SHIBUSDT': 'D', 'PEPEUSDT': 'D',
}
TIER_SCORE = {'S': 10, 'A': 8, 'B': 6, 'C': 4, 'D': 2, 'E': 0}

# Для SHORT: чем ниже тир — тем лучше для шорта (меньше ликвидности = легче падает)
TIER_SCORE_SHORT = {'S': 1, 'A': 3, 'B': 5, 'C': 7, 'D': 9, 'E': 0}

# Чёрный список — Tier E, мусор
BLACKLIST = {'TRUMPUSDT', 'MELANIAUSDT', 'BONKUSDT', 'FLOKIUSDT', 'WIFUSDT'}


def run_bybit(*args: str) -> dict:
    """Выполнить bybit CLI и вернуть JSON."""
    try:
        result = subprocess.run(
            ['bybit', *args],
            capture_output=True, text=True, timeout=25
        )
        if result.returncode != 0:
            return {'retCode': -1, 'retMsg': result.stderr.strip()}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {'retCode': 0, 'stdout': result.stdout}
    except subprocess.TimeoutExpired:
        return {'retCode': -1, 'retMsg': 'timeout'}
    except FileNotFoundError:
        return {'retCode': -1, 'retMsg': 'bybit not found'}


def get_top_tickers(limit: int = 50) -> list:
    """Получить топ-N монет по обороту."""
    result = run_bybit('raw', 'GET', '/v5/market/tickers?category=linear')
    if result.get('retCode') != 0:
        return []
    tickers = result.get('result', {}).get('list', [])
    tickers.sort(key=lambda t: float(t.get('turnover24h', 0)), reverse=True)
    out = []
    for t in tickers[:limit]:
        if not t['symbol'].endswith('USDT'):
            continue
        if t['symbol'] in BLACKLIST:
            continue
        out.append(t)
    return out


def get_candles(symbol: str, interval: str = 'D', limit: int = 30) -> Optional[list]:
    """Получить свечи (от старых к новым)."""
    result = run_bybit('raw', 'GET',
                       f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}')
    if result.get('retCode') != 0:
        return None
    try:
        candles = result.get('result', {}).get('list', [])
        if candles:
            return list(reversed(candles))
    except (KeyError, IndexError, TypeError):
        pass
    return None


def calc_bb(candles: list):
    """Рассчитать Bollinger Bands (20, 2)."""
    closes = [float(c[4]) for c in candles[-20:]]
    if len(closes) < 20:
        return None
    sma = sum(closes) / 20
    variance = sum((x - sma)**2 for x in closes) / 20
    std = math.sqrt(variance)
    return {
        'upper': sma + 2 * std,
        'middle': sma,
        'lower': sma - 2 * std,
        'current': closes[-1],
        'pos': ((closes[-1] - (sma - 2*std)) / (4*std)) * 100 if std > 0 else 50,
        'width': ((4*std) / sma) * 100 if sma > 0 else 10,
    }


def calc_rsi(candles: list, period: int = 14) -> Optional[float]:
    """Рассчитать RSI(period) на закрытиях (Wilder smoothing)."""
    closes = [float(c[4]) for c in candles]
    if len(closes) < period + 1:
        return None
    
    # Initial avg gain/loss on first 'period' price changes (oldest data)
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        return 100.0
    
    # Wilder smoothing for remaining candles (chronological order)
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0)
        loss = max(-delta, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def count_down_days(candles: list) -> int:
    """Посчитать последовательные дни падения (close < open)."""
    count = 0
    for c in reversed(candles[-15:]):
        if float(c[4]) < float(c[1]):
            count += 1
        else:
            break
    return count


def count_up_days(candles: list) -> int:
    """Посчитать последовательные дни роста (close > open)."""
    count = 0
    for c in reversed(candles[-15:]):
        if float(c[4]) > float(c[1]):
            count += 1
        else:
            break
    return count


def score_coin(symbol: str, ticker: dict, interval: str = 'D') -> Optional[dict]:
    """Полный 9-метричный LONG скоринг монеты. v4.0: +RSI, +честный TF."""
    if symbol in BANNED_SYMBOLS:
        return None
    # ── Фундамент (вес ×1, макс 10) ──
    tier = TIER_MAP.get(symbol, 'E')
    if tier == 'E':
        return None
    fundamental = TIER_SCORE[tier]

    # ── BB на заданном TF (вес ×3, макс 15) ──
    candles = get_candles(symbol, interval, 30)
    if not candles or len(candles) < 20:
        return None
    bb = calc_bb(candles)
    if not bb:
        return None

    # Auto-skip: BB > 80%
    if bb['pos'] > 80:
        return None

    # BB score
    p = bb['pos']
    if p < 10:   bb_score = 15
    elif p < 25: bb_score = 12
    elif p < 40: bb_score = 8
    elif p < 60: bb_score = 5
    elif p < 75: bb_score = 3
    else:        bb_score = 1

    # ── Объём (вес ×2, макс 10) ──
    turnover = float((ticker.get('turnover24h') or 0))
    if turnover < 1_000_000:
        return None
    if turnover > 500_000_000:    vol_score = 10
    elif turnover > 100_000_000:  vol_score = 8
    elif turnover > 50_000_000:   vol_score = 7
    elif turnover > 20_000_000:   vol_score = 6
    elif turnover > 10_000_000:   vol_score = 5
    elif turnover > 5_000_000:    vol_score = 4
    else:                         vol_score = 2

    # ── Серия падений (вес ×2, макс 10) ──
    down = count_down_days(candles)
    if down >= 5:      down_score = 10
    elif down >= 3:    down_score = 8
    elif down >= 2:    down_score = 5
    elif down >= 1:    down_score = 3
    else:              down_score = 1

    # ── Weekly + Monthly BB (вес ×1, макс 5) — всегда на Daily ──
    w_candles = get_candles(symbol, 'W', 30)
    m_candles = get_candles(symbol, 'M', 30)
    bb_w = calc_bb(w_candles) if w_candles else None
    bb_m = calc_bb(m_candles) if m_candles else None

    wm_score = 1
    if bb_w and bb_m:
        if bb_w['pos'] < 50 and bb_m['pos'] < 50:
            wm_score = 5
        elif bb_w['pos'] < 50 or bb_m['pos'] < 50:
            wm_score = 3

    # ── Фандинг (вес ×1, макс 5) ──
    funding = float(ticker.get('fundingRate', 0))
    abs_f = abs(funding)
    if abs_f < 0.00005:        fund_score = 5
    elif abs_f < 0.0001:       fund_score = 4
    elif abs_f < 0.0002:       fund_score = 3 if funding > 0 else 2
    elif abs_f < 0.0004:       fund_score = 2
    else:                      fund_score = 0

    # ── Волатильность / ширина BB (вес ×1, макс 5) ──
    w = bb['width']
    if 3 <= w <= 8:    vola_score = 5
    elif 1 <= w < 3:   vola_score = 3
    elif 8 < w <= 15:  vola_score = 3
    else:              vola_score = 1

    # ── Качество отскока (вес ×1, макс 5) ──
    quality = (bb['pos'] / 100) * bb['width']
    if quality <= 0.5:       qscore = 5
    elif quality <= 1.5:     qscore = 4
    elif quality <= 3.0:     qscore = 3
    elif quality <= 5.0:     qscore = 2
    else:                    qscore = 1

    # ── RSI (вес ×1, макс 5) — v4.0 ──
    rsi_val = calc_rsi(candles)
    if rsi_val is not None:
        if rsi_val < 25:       rsi_score = 5   # перепродано
        elif rsi_val < 30:     rsi_score = 4
        elif rsi_val < 40:     rsi_score = 3
        elif rsi_val < 50:     rsi_score = 2
        elif rsi_val >= 50:    rsi_score = 1   # нейтрально/перекуплено — не помогает LONG
    else:
        rsi_score = 1

    # ── Финальный Score ──
    raw = (fundamental * 1) + (bb_score * 3) + (vol_score * 2) + \
          (down_score * 2) + (wm_score * 1) + (fund_score * 1) + \
          (vola_score * 1) + (qscore * 1) + (rsi_score * 1)
    # Веса: 1+3+2+2+1+1+1+1+1 = 13, макс = 10+45+20+20+5+5+5+5+5 = 120
    score = round(raw / 12.0, 1)

    # ── Бонус BB-согласованности D/W/M ──
    if bb_w and bb_m:
        if bb['pos'] < 25 and bb_w['pos'] < 50 and bb_m['pos'] < 50:
            score += 0.4
        elif bb['pos'] < 50 and bb_w['pos'] < 50 and bb_m['pos'] < 50:
            score += 0.3
        elif bb['pos'] < 50 and (bb_w['pos'] > 75 if bb_w else False):
            score -= 0.1

    # ── RSI-бонус: перепродано + низкий BB = конфлюенс ──
    if rsi_val is not None and rsi_val < 30 and bb['pos'] < 25:
        score += 0.3

    # ── ML Score (вес ×1, макс 5) — Phase 3 ──
    ml_score_val = None
    ml_adjusted = None
    try:
        from bybit_ws.ml_scorer import ml_adjusted_score
        # Строим сигнал-дикт для ML
        signal_dict = {
            'lower_bb': bb['lower'], 'upper_bb': bb['upper'],
            'middle_bb': bb['middle'], 'price': float(ticker.get('lastPrice', 0)),
            'entry': bb['lower'] * 0.97, 'score': score,
            'timeframe': interval, 'mode': 'long',
        }
        ml_adjusted = ml_adjusted_score(signal_dict)
        if ml_adjusted and ml_adjusted != score:
            ml_score_val = round(ml_adjusted, 1)
            # ML-бонус: мягкая коррекция
            ml_bonus = ml_adjusted - score
            score = ml_adjusted  # заменяем на ML-скорректированный
    except ImportError:
        pass

    return {
        'symbol': symbol,
        'score': round(score, 1),
        'price': bb['current'],
        'lower_bb': bb['lower'],
        'upper_bb': bb['upper'],
        'middle_bb': bb['middle'],
        'bb_pos': round(bb['pos'], 1),
        'bb_width': round(bb['width'], 1),
        'down_days': down,
        'turnover': turnover,
        'funding': funding,
        'tier': tier,
        'rsi': round(rsi_val, 1) if rsi_val is not None else None,
        'ml_score': ml_score_val,
        'mode': 'LONG',
        'interval': interval,
    }


def score_short(symbol: str, ticker: dict, interval: str = 'D') -> Optional[dict]:
    """9-метричный SHORT скоринг. Инвертированная логика: ищем перегретые монеты."""
    if symbol in BANNED_SYMBOLS:
        return None
    tier = TIER_MAP.get(symbol, 'E')
    if tier == 'E':
        return None
    
    # Для шорта: чем ниже тир — тем лучше (мусор легче шортить)
    fundamental = TIER_SCORE_SHORT.get(tier, 0)

    # ── BB на заданном TF ──
    candles = get_candles(symbol, interval, 30)
    if not candles or len(candles) < 20:
        return None
    bb = calc_bb(candles)
    if not bb:
        return None

    # Auto-skip: BB < 70% (нет перегрева)
    if bb['pos'] < 70:
        return None

    # BB score (инвертирован: чем выше BB — тем лучше для шорта)
    p = bb['pos']
    if p > 95:      bb_score = 15
    elif p > 85:    bb_score = 12
    elif p > 80:    bb_score = 8
    elif p > 75:    bb_score = 5
    else:           bb_score = 2

    # ── Объём (вес ×2, макс 10) ──
    turnover = float((ticker.get('turnover24h') or 0))
    if turnover < 1_000_000:
        return None
    if turnover > 500_000_000:    vol_score = 10
    elif turnover > 100_000_000:  vol_score = 8
    elif turnover > 50_000_000:   vol_score = 7
    elif turnover > 20_000_000:   vol_score = 6
    elif turnover > 10_000_000:   vol_score = 5
    elif turnover > 5_000_000:    vol_score = 4
    else:                         vol_score = 2

    # ── Серия роста (вес ×2, макс 10) — памп перед шортом ──
    up = count_up_days(candles)
    if up >= 5:      up_score = 10
    elif up >= 3:    up_score = 8
    elif up >= 2:    up_score = 5
    elif up >= 1:    up_score = 3
    else:            up_score = 1

    # ── Weekly + Monthly BB (вес ×1, макс 5) ──
    w_candles = get_candles(symbol, 'W', 30)
    m_candles = get_candles(symbol, 'M', 30)
    bb_w = calc_bb(w_candles) if w_candles else None
    bb_m = calc_bb(m_candles) if m_candles else None

    wm_score = 1
    if bb_w and bb_m:
        if bb_w['pos'] > 50 and bb_m['pos'] > 50:
            wm_score = 5   # перегрет на всех TF
        elif bb_w['pos'] > 50 or bb_m['pos'] > 50:
            wm_score = 3

    # ── Фандинг (вес ×1, макс 5) — положительный = хорошо для шорта ──
    funding = float(ticker.get('fundingRate', 0))
    if funding > 0.0003:       fund_score = 5   # сильный перекос лонгистов
    elif funding > 0.0001:     fund_score = 4
    elif funding > 0.00005:    fund_score = 3
    elif funding > 0:          fund_score = 2
    else:                      fund_score = 1   # neutral/negative — less attractive

    # ── Волатильность (вес ×1, макс 5) — широкая BB = хорошо для шорта ──
    w = bb['width']
    if w > 15:         vola_score = 5   # экстремальная вола — хороший разворот
    elif w > 10:       vola_score = 4
    elif w > 6:        vola_score = 3
    elif w > 3:        vola_score = 2
    else:              vola_score = 1

    # ── Качество разворота (вес ×1, макс 5) ──
    quality = ((100 - bb['pos']) / 100) * bb['width']
    if quality <= 0.5:       qscore = 5
    elif quality <= 1.5:     qscore = 4
    elif quality <= 3.0:     qscore = 3
    elif quality <= 5.0:     qscore = 2
    else:                    qscore = 1

    # ── RSI (вес ×1, макс 5) — перекуплено = хорошо для шорта ──
    rsi_val = calc_rsi(candles)
    if rsi_val is not None:
        if rsi_val > 75:       rsi_score = 5
        elif rsi_val > 70:     rsi_score = 4
        elif rsi_val > 60:     rsi_score = 3
        elif rsi_val > 50:     rsi_score = 2
        else:                  rsi_score = 1
    else:
        rsi_score = 1

    # ── Финальный Score ──
    raw = (fundamental * 1) + (bb_score * 3) + (vol_score * 2) + \
          (up_score * 2) + (wm_score * 1) + (fund_score * 1) + \
          (vola_score * 1) + (qscore * 1) + (rsi_score * 1)
    score = round(raw / 12.0, 1)

    # ── Бонус: все TF перегреты ──
    if bb_w and bb_m:
        if bb['pos'] > 85 and bb_w['pos'] > 50 and bb_m['pos'] > 50:
            score += 0.4
        elif bb['pos'] > 75 and bb_w['pos'] > 50 and bb_m['pos'] > 50:
            score += 0.3

    # ── RSI-бонус: перекуплено + высокий BB = конфлюенс ──
    if rsi_val is not None and rsi_val > 70 and bb['pos'] > 85:
        score += 0.3

    return {
        'symbol': symbol,
        'score': round(score, 1),
        'price': bb['current'],
        'lower_bb': bb['lower'],
        'upper_bb': bb['upper'],
        'middle_bb': bb['middle'],
        'bb_pos': round(bb['pos'], 1),
        'bb_width': round(bb['width'], 1),
        'up_days': up,
        'turnover': turnover,
        'funding': funding,
        'tier': tier,
        'rsi': round(rsi_val, 1) if rsi_val is not None else None,
        'mode': 'SHORT',
        'interval': interval,
    }


# ═══════════════════════════════════════════════════════════════
# X10 SCORING FUNCTIONS (v4.1)
# ═══════════════════════════════════════════════════════════════

# Tier A/B только для x10 стратегий
X10_TIERS = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'AVAXUSDT', 'LINKUSDT',
    'ADAUSDT', 'DOTUSDT', 'LTCUSDT', 'XRPUSDT', 'UNIUSDT',
    'NEARUSDT', 'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT',
    'ENAUSDT', 'ATOMUSDT', 'ALGOUSDT', 'FETUSDT', 'RUNEUSDT',
    'WLDUSDT', 'SUIUSDT',
}

ONE_WAY_X10 = {'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT',
               'AVAXUSDT', 'APTUSDT', 'SUIUSDT'}

# Символы в перманентном бане (убыточные, не торгуются НИКОГДА)
BANNED_SYMBOLS = {'BLESSUSDT'}


def score_scalp(symbol: str, ticker: dict, interval: str = '5') -> Optional[dict]:
    """BB Scalping M5 x10: касание полосы + RSI-фильтр."""
    if symbol in BANNED_SYMBOLS or symbol not in X10_TIERS:
        return None

    candles = get_candles(symbol, '5', 30)
    if not candles or len(candles) < 21:
        return None

    bb = calc_bb(candles)
    if not bb:
        return None

    closes = [float(c[4]) for c in candles]
    rsi_val = calc_rsi(candles)
    price = closes[-1]

    direction = None
    if price <= bb['lower'] * 1.005 and rsi_val is not None and rsi_val < 35:
        direction = 'LONG'
        entry = bb['lower']
        tp = bb['middle']
        sl = round(entry * 0.97, 8)
    elif price >= bb['upper'] * 0.995 and rsi_val is not None and rsi_val > 65:
        direction = 'SHORT'
        entry = price
        tp = bb['middle']
        sl = round(entry * 1.03, 8)
    else:
        return None

    # Score: RSI далеко от середины + близко к полосе = выше
    rsi_extreme = abs(rsi_val - 50) / 50 * 5  # 0..5
    bb_width = bb['upper'] - bb['lower']; bb_touch = (1 - min(abs(price - bb['lower']) / (bb_width if bb_width > 0 else 0.0001),
                        abs(price - bb['upper']) / (bb['upper'] - bb['lower']))) * 5
    score = round(rsi_extreme + bb_touch, 1)

    return {
        'symbol': symbol,
        'score': min(score, 10),
        'price': price,
        'lower_bb': bb['lower'],
        'upper_bb': bb['upper'],
        'middle_bb': bb['middle'],
        'bb_pos': round(bb['pos'], 1),
        'bb_width': round(bb['width'], 1),
        'turnover': float((ticker.get('turnover24h') or 0)),
        'tier': TIER_MAP.get(symbol, 'C'),
        'rsi': round(rsi_val, 1) if rsi_val else None,
        'mode': f'SCALP_{direction}',
        'interval': '5',
        'direction': direction,
        'entry': round(entry, 8),
        'tp': round(tp, 8),
        'sl': sl,
    }


def score_mean_revert(symbol: str, ticker: dict, interval: str = 'D') -> Optional[dict]:
    """Mean Reversion Extreme x10: BB% < 5% (LONG) или > 95% (SHORT)."""
    if symbol in BANNED_SYMBOLS or symbol not in X10_TIERS:
        return None

    candles = get_candles(symbol, 'D', 20)
    if not candles or len(candles) < 5:
        return None

    bb = calc_bb(candles)
    if not bb:
        return None

    bb_pos = bb['pos']
    direction = None
    entry = sl = tp = None

    if bb_pos < 5:
        direction = 'LONG'
        entry = bb['lower']
        tp = bb['middle']
        sl = round(entry * 0.95, 8)
    elif bb_pos > 95 and symbol not in ONE_WAY_X10:
        direction = 'SHORT'
        entry = bb['upper']
        tp = bb['middle']
        sl = round(entry * 1.05, 8)
    else:
        return None

    # Score: чем экстремальнее BB%, тем выше
    extreme = max(bb_pos, 100 - bb_pos)  # 95..100
    score = round(extreme / 10, 1)  # 9.5..10

    return {
        'symbol': symbol,
        'score': score,
        'price': bb['current'],
        'lower_bb': bb['lower'],
        'upper_bb': bb['upper'],
        'middle_bb': bb['middle'],
        'bb_pos': round(bb_pos, 1),
        'bb_width': round(bb['width'], 1),
        'turnover': float((ticker.get('turnover24h') or 0)),
        'tier': TIER_MAP.get(symbol, 'C'),
        'mode': f'MEAN_{direction}',
        'interval': 'D',
        'direction': direction,
        'entry': round(entry, 8),
        'tp': round(tp, 8),
        'sl': sl,
    }


def score_funding_momentum(symbol: str, ticker: dict, interval: str = 'D') -> Optional[dict]:
    """Funding Rate Momentum x10: экстремальный фондинг + BB-фильтр + тренд."""
    if symbol in BANNED_SYMBOLS or symbol not in X10_TIERS:
        return None

    candles = get_candles(symbol, 'D', 20)
    if not candles or len(candles) < 5:
        return None

    bb = calc_bb(candles)
    if not bb:
        return None

    funding = float(ticker.get('fundingRate', 0))
    closes = [float(c[4]) for c in candles]
    bb_pos = bb['pos']

    # 3-дневный тренд
    trend_3d = 0.0
    if len(closes) >= 4:
        trend_3d = (closes[-1] - closes[-4]) / closes[-4]

    direction = None
    entry = sl = tp = None

    # LONG: фондинг < -0.1% + BB% < 15%
    if funding < -0.001 and bb_pos < 15:
        direction = 'LONG'
        entry = bb['lower']
        tp = bb['middle']
        sl = round(entry * 0.96, 8)
    # SHORT: фондинг > +0.1% + BB% > 85% + тренд падает
    elif (funding > 0.001 and bb_pos > 85 and
          symbol not in ONE_WAY_X10 and trend_3d < 0):
        direction = 'SHORT'
        entry = bb['upper']
        tp = bb['middle']
        sl = round(entry * 1.04, 8)
    else:
        return None

    # Score: сила фондинга + BB-экстремальность
    funding_str = min(abs(funding) * 1000, 5)  # 0..5
    bb_str = max(bb_pos, 100 - bb_pos) / 20  # 0..5
    score = round(funding_str + bb_str, 1)

    return {
        'symbol': symbol,
        'score': min(score, 10),
        'price': bb['current'],
        'lower_bb': bb['lower'],
        'upper_bb': bb['upper'],
        'middle_bb': bb['middle'],
        'bb_pos': round(bb_pos, 1),
        'bb_width': round(bb['width'], 1),
        'turnover': float((ticker.get('turnover24h') or 0)),
        'funding': funding,
        'tier': TIER_MAP.get(symbol, 'C'),
        'mode': f'FUNDING_{direction}',
        'interval': 'D',
        'direction': direction,
        'entry': round(entry, 8),
        'tp': round(tp, 8),
        'sl': sl,
    }


def scan(limit: int = 5, mode: str = 'long', interval: str = 'D',
         green_only: bool = False) -> list:
    """
    Основная функция: просканировать рынок, вернуть top-N сигналов.
    
    Args:
        limit: сколько сигналов вернуть
        mode: 'long', 'short', 'scalp', 'mean_revert', 'funding'
        interval: 'D', '5', '3', '15', '60', 'W', 'M'
        green_only: только BB < 25% (только для LONG)
    """
    tickers = get_top_tickers(50)
    if not tickers:
        return []

    if mode == 'scalp':
        score_func = score_scalp
    elif mode == 'mean_revert':
        score_func = score_mean_revert
    elif mode == 'funding':
        score_func = score_funding_momentum
    elif mode == 'short':
        score_func = score_short
    else:
        score_func = score_coin
    results = []
    
    for t in tickers:
        symbol = t['symbol']
        s = score_func(symbol, t, interval)
        if not s:
            continue
        
        if mode == 'long':
            if s['score'] < 3.5:
                continue
            if interval in ('5', '3', '15') and s.get('bb_width', 0) > 15:
                continue
            if green_only and s['bb_pos'] > 25:
                continue
        elif mode in ('scalp', 'mean_revert', 'funding'):
            # x10 стратегии: более жёсткий порог
            if s['score'] < 5.0:
                continue
        else:
            if s['score'] < 3.0:
                continue

        results.append(s)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:limit]


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='GridSignal Scanner v4.1')
    p.add_argument('--mode', default='long', choices=['long', 'short', 'scalp', 'mean_revert', 'funding'])
    p.add_argument('--tf', default='D', help='D/5/3/15/60/W/M')
    p.add_argument('--green', action='store_true', help='Green zone only')
    p.add_argument('--limit', type=int, default=5)
    p.add_argument('symbol', nargs='?', help='Specific symbol to scan')
    args = p.parse_args()
    
    if args.symbol:
        # Скан конкретного тикера
        tickers = get_top_tickers(100)
        ticker = next((t for t in tickers if t['symbol'] == args.symbol.upper()), None)
        if not ticker:
            # fallback: прямой запрос
            r = run_bybit('raw', 'GET', f'/v5/market/tickers?category=linear&symbol={args.symbol.upper()}')
            if r.get('retCode') == 0:
                tlist = r.get('result', {}).get('list', [])
                if tlist:
                    ticker = tlist[0]
        if ticker:
            if args.mode == 'scalp':
                score_func = score_scalp
            elif args.mode == 'mean_revert':
                score_func = score_mean_revert
            elif args.mode == 'funding':
                score_func = score_funding_momentum
            elif args.mode == 'short':
                score_func = score_short
            else:
                score_func = score_coin
            s = score_func(args.symbol.upper(), ticker, args.tf)
            if s:
                print(json.dumps(s, indent=2, default=str))
            else:
                print(json.dumps({'error': 'No score', 'symbol': args.symbol.upper()}))
        else:
            print(json.dumps({'error': 'Symbol not found', 'symbol': args.symbol.upper()}))
    else:
        print(json.dumps(scan(args.limit, args.mode, args.tf, args.green), indent=2, default=str))
