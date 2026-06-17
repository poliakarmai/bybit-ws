"""
Position Sizing — динамический расчёт маржи от % депозита.

Принцип: депозит = страховка на кросс-марже.
Входим малой долей, остальное страхует от ликвидации.

Параметры читаются из конфига (position_sizing:) с фоллбеком на хардкод.
"""

import time, math, json, os
from pathlib import Path
from .alerts import log_event
from .api import bybit, fetch_atr
from .config import Config

# === Фоллбек-константы (если конфиг не задан) ===
_FB_LONG_RISK = 0.20       # 20% депозита в риске (LONG/SHORT 3x)
_FB_X10_RISK = 0.05        # 5% депозита в риске (x10 стратегии)
_FB_DCA_RISK = 0.10        # 10% для DCA
_FB_PUMP_RISK = 0.06       # 6% для памп-шортов
_FB_MAX_POSITIONS = 5      # макс одновременных позиций
_FB_MIN_MARGIN = 5.0       # пол: Bybit $5 минимум
_FB_MAX_SHARE = 0.40       # потолок: не более 40% бюджета на одну позицию
_FB_MIN_DEPOSIT = 30.0     # мин депозит для торговли

DEPOSIT_CACHE = {"value": None, "ts": 0}
DEPOSIT_CACHE_TTL = 120    # кеш на 2 минуты


def _ps_cfg():
    """Загрузить секцию position_sizing из конфига."""
    try:
        return Config().cfg.get('position_sizing', {}) or {}
    except Exception:
        return {}


def _p(key: str, default):
    """Прочитать параметр из конфига с фоллбеком."""
    return _ps_cfg().get(key, default)


def get_deposit() -> float:
    """Текущий USDT баланс с Bybit (с кешем на 2 мин)."""
    now = time.time()
    if (DEPOSIT_CACHE["value"] is not None and
            now - DEPOSIT_CACHE["ts"] < DEPOSIT_CACHE_TTL):
        return DEPOSIT_CACHE["value"]

    try:
        r = bybit("GET",
            "/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT")
        if r and r.get("retCode") == 0:
            for coin in r["result"]["list"][0].get("coin", []):
                if coin["coin"] == "USDT":
                    balance = float(coin.get("walletBalance", 0))
                    DEPOSIT_CACHE["value"] = balance
                    DEPOSIT_CACHE["ts"] = now
                    return balance
    except Exception as e:
        log_event(f"get_deposit error: {e}")

    if DEPOSIT_CACHE["value"] is not None:
        return DEPOSIT_CACHE["value"]
    return 0


def _score_multiplier(score: float) -> float:
    """Множитель маржи от скора (уверенность → больше маржа)."""
    multipliers = _p('score_multipliers', {
        8.5: 1.4, 7.5: 1.15, 6.5: 1.0, 5.5: 0.75
    })
    # Сортируем пороги по убыванию
    thresholds = sorted((float(k), v) for k, v in multipliers.items()
                        if isinstance(v, (int, float)))
    thresholds.sort(reverse=True)
    for threshold, mult in thresholds:
        if score >= threshold:
            return float(mult)
    return 0  # score ниже минимального порога


def calculate_margin(score: float, risk_pct: float | None = None) -> float:
    """
    Рассчитать маржу на позицию от % депозита.

    score:     5.5–10.0
    risk_pct:  % депозита в риске (None → из конфига или _FB_LONG_RISK)
    """
    if risk_pct is None:
        risk_pct = _p('long_risk_pct', _FB_LONG_RISK)

    mult = _score_multiplier(score)
    if mult <= 0:
        return 0

    deposit = get_deposit()
    min_deposit = _p('min_deposit', _FB_MIN_DEPOSIT)
    if deposit < min_deposit:
        log_event(f"💸 Депозит ${deposit:.1f} < ${min_deposit} — входы заблокированы")
        return 0

    max_pos = _p('max_positions', _FB_MAX_POSITIONS)
    min_margin = _p('min_margin', _FB_MIN_MARGIN)
    max_share = _p('max_position_share', _FB_MAX_SHARE)

    risk_budget = deposit * risk_pct
    base = risk_budget / max_pos
    margin = round(base * mult, 1)

    margin = max(margin, min_margin)
    cap = max(min_margin, risk_budget * max_share)
    margin = min(margin, cap)

    return margin


def margin_for_strategy(strategy: str, score: float = 5.5,
                        symbol: str = None, use_atr: bool = False) -> float:
    """
    Маржа под конкретную стратегию.

    strategy: 'long' | 'short' | 'reentry' | 'scalp' | 'mean_revert' | 'funding' | 'pump' | 'dca'
    symbol:   тикер для ATR-сайзинга (опционально)
    use_atr:  True → ATR-based расчёт вместо процентного (Фаза 4)
    """
    # ATR-режим
    if use_atr and symbol:
        result = atr_margin(symbol, score)
        if result.get('margin', 0) > 0:
            return result['margin']
        # fallback к процентному если ATR не сработал

    strategy_risk = {
        'long':      _p('long_risk_pct', _FB_LONG_RISK),
        'short':     _p('long_risk_pct', _FB_LONG_RISK),
        'reentry':   _p('long_risk_pct', _FB_LONG_RISK),
        'dca':       _p('dca_risk_pct', _FB_DCA_RISK),
        'pump':      _p('pump_risk_pct', _FB_PUMP_RISK),
        'scalp':     _p('x10_risk_pct', _FB_X10_RISK),
        'mean_revert': _p('x10_risk_pct', _FB_X10_RISK),
        'funding':   _p('x10_risk_pct', _FB_X10_RISK),
    }

    risk_pct = strategy_risk.get(strategy, _p('long_risk_pct', _FB_LONG_RISK))
    return calculate_margin(score, risk_pct=risk_pct)


# ── ATR-based sizing (Фаза 4) ──────────────────────────────────

ATR_CACHE_FILE = Path.home() / '.local' / 'share' / 'bybit-ws' / 'atr_cache.json'
ATR_CACHE_TTL = 14400  # 4 часа


def _load_atr_cache() -> dict:
    try:
        if ATR_CACHE_FILE.exists():
            with open(ATR_CACHE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_atr_cache(cache: dict):
    try:
        tmp = str(ATR_CACHE_FILE) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, str(ATR_CACHE_FILE))
    except Exception:
        pass


def get_atr(symbol: str, interval: str = 'D', period: int = 14) -> float | None:
    """Получить ATR с кешированием (4 часа).

    Args:
        symbol: e.g. DOTUSDT
        interval: D, 4h, 1h
        period: период ATR (default 14)

    Returns:
        float: ATR в долларах или None
    """
    cache = _load_atr_cache()
    cache_key = f'{symbol}_{interval}_{period}'
    entry = cache.get(cache_key, {})

    now = time.time()
    if entry.get('value') and now - entry.get('ts', 0) < ATR_CACHE_TTL:
        return entry['value']

    atr = fetch_atr(symbol, interval, period)
    if atr is not None and atr > 0:
        cache[cache_key] = {'value': atr, 'ts': now}
        _save_atr_cache(cache)

    return atr


def atr_margin(symbol: str, score: float = 5.5,
               side: str = 'Buy', interval: str = 'D',
               atr_mult: float = None, risk_per_trade: float = None) -> dict:
    """ATR-based расчёт маржи.

    Формула: margin = risk_budget / (ATR_$ * multiplier)
    - risk_budget: $ на риск в одной сделке (default $5)
    - ATR_$: волатильность в долларах за выбранный интервал
    - multiplier: сколько ATR держим как стоп (default 2.0)

    Returns:
        dict с ключами: margin, usdt_qty, qty, atr, risk_budget, method
        или {'margin': 0, 'error': '...'}
    """
    if atr_mult is None:
        atr_mult = _p('atr_multiplier', 2.0)
    if risk_per_trade is None:
        risk_per_trade = _p('atr_risk_per_trade', 5.0)

    atr = get_atr(symbol, interval)
    if not atr or atr <= 0:
        return {'margin': 0, 'error': f'ATR недоступен для {symbol}'}

    # Риск-бюджет распределяем через ATR
    # Если ATR = $0.01, multiplier = 2.0 → стоп-дистанция = $0.02
    # margin = risk_budget * leverage / (ATR * multiplier)
    # Но проще: margin = risk_budget (маржа и есть наш риск при кросс-марже)

    margin = round(risk_per_trade, 1)
    margin = max(margin, _p('min_margin', _FB_MIN_MARGIN))

    # Рассчитываем qty: usdt_qty = margin * leverage, qty = usdt_qty / price
    # Но цена неизвестна на этом этапе — возвращаем margin и atr,
    # вызывающий код сам посчитает qty при известной цене

    return {
        'margin': margin,
        'atr': atr,
        'atr_multiplier': atr_mult,
        'risk_per_trade': risk_per_trade,
        'stop_distance': round(atr * atr_mult, 8),
        'method': 'atr',
    }


def suggest_qty(margin: float, price: float, leverage: float = 10,
                lot_step: float = 0.1) -> float:
    """По марже и цене посчитать qty для ордера.

    Args:
        margin: маржа в USDT
        price: текущая цена
        leverage: плечо (default 10x)
        lot_step: шаг лота (default 0.1)

    Returns:
        float: количество контрактов
    """
    if price <= 0:
        return 0
    usdt_qty = margin * leverage
    qty = math.floor(usdt_qty / price / lot_step) * lot_step
    return max(qty, lot_step)
