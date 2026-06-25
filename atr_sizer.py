"""ATR-Based Risk Sizing — расчёт размера позиции под заданный риск.

Про-подход: SL = 1.5 × ATR(14), размер позиции = (1% от баланса) / (SL% × плечо).

Используется:
1. Как рекомендательный слой для всех x10 стратегий
2. Для валидации новых входов — не даёт войти слишком крупно
3. Для мониторинга текущих позиций — алерт если риск > 2%

Формула:
  risk_usdt = balance * risk_pct / 100
  sl_distance_pct = (1.5 * ATR) / price
  max_margin = risk_usdt / (sl_distance_pct * leverage)
"""
import json
import math
import os
import time

from .api import bybit
from .alerts import log_event, _is_duplicate

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
ATR_CACHE_FILE = os.path.join(DATA_DIR, 'atr_cache.json')

ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5         # SL на расстоянии 1.5 × ATR
RISK_PCT = 1.0               # 1% баланса на сделку
MAX_RISK_PCT = 2.0           # алерт если риск > 2%

# Только для активных позиций + кандидатов
ATR_INTERVAL = '15'           # 15-минутные свечи


def _load_cache():
    try:
        if os.path.exists(ATR_CACHE_FILE):
            with open(ATR_CACHE_FILE) as f:
                return json.load(f)
    except Exception as e:
        log_event(f'⚠️ atr_sizer: {e}')
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(ATR_CACHE_FILE), exist_ok=True)
    with open(ATR_CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)


def _get_atr(sym, interval=ATR_INTERVAL, period=ATR_PERIOD):
    """Рассчитать ATR за период."""
    try:
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval={interval}&limit={period + 1}')
        candles = data.get('result', {}).get('list', [])
        if len(candles) < period + 1:
            return None

        tr_values = []
        for i in range(1, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)

        atr = sum(tr_values[-period:]) / period
        return round(atr, 8)
    except Exception:
        return None


def size_position(sym, price, leverage, balance, risk_pct=RISK_PCT):
    """Рассчитать безопасный размер позиции.

    Returns dict с margin, qty, sl_price, sl_distance_pct, risk_usdt.
    """
    atr = _get_atr(sym)
    if not atr or price <= 0:
        return None

    sl_distance = ATR_MULTIPLIER * atr
    sl_distance_pct = sl_distance / price
    risk_usdt = balance * risk_pct / 100

    # max_margin = risk_usdt / (sl_distance_pct * leverage)
    if sl_distance_pct <= 0:
        return None

    max_margin = risk_usdt / (sl_distance_pct * leverage)
    qty = math.ceil(max_margin * leverage / price * 100) / 100

    return {
        'symbol': sym,
        'atr': atr,
        'sl_distance': round(sl_distance, 8),
        'sl_distance_pct': round(sl_distance_pct * 100, 2),
        'risk_usdt': round(risk_usdt, 4),
        'max_margin': round(max_margin, 4),
        'qty': qty,
        'leverage': leverage,
        'sl_price': round(price - sl_distance, 8),
    }


def check_position_risk(positions, balance_usdt):
    """Проверить текущие позиции на превышение риска."""
    alerts = []
    cache = _load_cache()
    now = time.time()

    for sym, p in positions.items():
        if float(p.get('size', 0)) <= 0:
            continue

        # Кешируем ATR на 30 минут
        cached = cache.get(sym, {})
        if now - cached.get('ts', 0) < 1800 and cached.get('atr'):
            atr = cached['atr']
        else:
            atr = _get_atr(sym)
            cache[sym] = {'atr': atr, 'ts': now}

        if not atr:
            continue

        mark = float(p.get('mark', 0))
        if mark <= 0:
            continue

        margin = float(p.get('margin', 0))
        leverage = float(p.get('leverage', 1))
        sl_distance = ATR_MULTIPLIER * atr
        sl_distance_pct = sl_distance / mark
        position_risk_pct = (sl_distance_pct * leverage * margin / balance_usdt) * 100

        if position_risk_pct > MAX_RISK_PCT:
            msg = (f'⚠️ РИСК {sym}: {position_risk_pct:.1f}% от баланса '
                   f'(margin=${margin:.1f} ATR={atr:.4f} SL-dist={sl_distance_pct*100:.1f}%)')
            if not _is_duplicate(msg, 'STOP'):
                alerts.append(msg)
                log_event(msg)

    _save_cache(cache)
    return alerts


def validate_entry(entry_info, balance_usdt):
    """Проверить что вход не нарушает риск-лимиты.

    entry_info: dict с symbol, entry, leverage, margin
    Возвращает (passed: bool, reason: str)
    """
    sym = entry_info['symbol']
    price = entry_info['entry']
    leverage = entry_info.get('leverage', 10)
    margin = entry_info.get('margin', 10)

    sizing = size_position(sym, price, leverage, balance_usdt)
    if not sizing:
        return True, 'ATR not available — skipping risk check'

    if margin > sizing['max_margin'] * 1.5:
        return False, (f'Риск превышен: маржа ${margin:.1f} > макс ${sizing["max_margin"]:.1f} '
                       f'(SL-dist {sizing["sl_distance_pct"]}%, риск ${sizing["risk_usdt"]})')

    return True, 'OK'
