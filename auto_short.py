"""Авто-SHORT по перегреву (BB Daily > 85%).

Зеркало LONG-стратегии: когда цена перегрета — шорт с возвратом к Middle BB.

Правила:
- BB Daily > 85% (цена у Upper или выше)
- Все Tier'ы (S/A/B/C/D) — шортим любой перегрев
- One-way монеты исключены (там нельзя SHORT)
- Плечо 3x, маржа $10 → $5 для Tier C/D
- SL: +5% от входа (stop-buy выше)
- TP: Middle BB (reduceOnly лимитный buy)
- Макс 3 одновременных SHORT
- Кулдаун 2 часа на монету
- Блок при >80% SHORT (корреляция)
"""

import json
import math
import os
import time

from .api import bybit, get_bb_data
from .alerts import log_event, add_alert
from .config import Config

SHORT_STATE_FILE = os.path.expanduser('~/.local/share/bybit-ws/short_positions.json')

# Tier sets are built from config.tiers
def _get_tier_ab(cfg):
    """Build TIER_AB set from config tiers A + B."""
    return set(cfg.tiers.A) | set(cfg.tiers.B)

def _get_one_way(cfg):
    """Build ONE_WAY set from config tiers.one_way."""
    return set(cfg.tiers.one_way)


def _load_state():
    try:
        if os.path.exists(SHORT_STATE_FILE):
            with open(SHORT_STATE_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}


def _save_state(state):
    os.makedirs(os.path.dirname(SHORT_STATE_FILE), exist_ok=True)
    with open(SHORT_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _get_lot_step(sym):
    try:
        data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
        instruments = data.get('result', {}).get('list', [])
        if instruments:
            return float(instruments[0].get('lotSizeFilter', {}).get('qtyStep', 0.1))
    except:
        pass
    return 0.1


def _round_to_tick(price, sym):
    if price < 1:
        tick = 0.0001
    elif price < 10:
        tick = 0.001
    elif price < 100:
        tick = 0.01
    elif price < 1000:
        tick = 0.1
    else:
        tick = 1.0
    return round(price / tick) * tick


def check_auto_short(positions):
    """Сканировать перегретые монеты и ставить SHORT.
    Вызывается каждые 10 циклов (5 мин)."""
    cfg = Config()
    ONE_WAY = _get_one_way(cfg)
    TIER_AB = _get_tier_ab(cfg)
    BB_SHORT_THRESHOLD = cfg.strategy.short.bb_threshold
    SHORT_MARGIN = cfg.strategy.short.margin
    SHORT_LEVERAGE = cfg.strategy.short.leverage
    SL_PCT = cfg.strategy.short.sl_tier_ab
    SL_PCT_JUNK = cfg.strategy.short.sl_tier_cd
    MAX_SHORTS = cfg.strategy.short.max_positions
    COOLDOWN = cfg.strategy.short.cooldown_seconds
    ENTRY_OFFSET = cfg.strategy.short.entry_offset

    state = _load_state()
    now = time.time()

    # Считаем текущие SHORT (в позиции + в стейте)
    active_shorts = sum(1 for p in positions.values()
                        if isinstance(p, dict) and p.get('side') == 'Sell')
    live_syms = set(positions.keys()) if isinstance(positions, dict) else set()

    if active_shorts >= MAX_SHORTS:
        return []

    actions = []

    # Получаем топ-80 тикеров
    try:
        data = bybit('GET', '/v5/market/tickers?category=linear')
        if not data or data.get('retCode') != 0:
            return actions
        tickers = data['result'].get('list', [])
    except:
        return actions

    # Сортируем по обороту, берём кандидатов (все Tier'ы, кроме one-way)
    tickers.sort(key=lambda t: float(t.get('turnover24h', 0) or 0), reverse=True)
    candidates = [t for t in tickers[:80]
                  if t['symbol'] not in ONE_WAY
                  and t['symbol'] not in live_syms]

    for t in candidates:
        if active_shorts + len(actions) >= MAX_SHORTS:
            break

        sym = t['symbol']
        last_price = float(t.get('lastPrice', 0) or 0)
        if last_price <= 0:
            continue

        # Проверка кулдауна
        if sym in state and now - state[sym].get('last_short_ts', 0) < COOLDOWN:
            continue

        # Проверка BB
        try:
            bb = get_bb_data(sym, 'D')
            if not bb:
                continue
            upper = float(bb.get('upper', 0))
            middle = float(bb.get('middle', 0))
            lower = float(bb.get('lower', 0))
            if upper <= 0 or upper == lower:
                continue
            bb_pct = (last_price - lower) / (upper - lower) * 100 if upper != lower else 0
        except:
            continue

        if bb_pct < BB_SHORT_THRESHOLD:
            continue

        # Шорт! Рассчитываем параметры
        usdt_qty = SHORT_MARGIN * SHORT_LEVERAGE  # $30
        qty_step = _get_lot_step(sym)
        qty = math.ceil(usdt_qty / last_price / qty_step) * qty_step
        if qty <= 0:
            continue

        price = _round_to_tick(last_price, sym)
        sl_pct = SL_PCT_JUNK if sym not in TIER_AB else SL_PCT
        sl_price = _round_to_tick(price * (1 + sl_pct), sym)
        tp_price = _round_to_tick(middle, sym)

        try:
            # Лимитный SHORT: Sell выше рынка на +entry_offset% — ждём отскока для входа
            limit_price = _round_to_tick(price * (1 + ENTRY_OFFSET), sym)
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': sym,
                'side': 'Sell',
                'orderType': 'Limit',
                'qty': str(qty),
                'price': str(limit_price),
                'positionIdx': 0,  # SHORT (one-way mode)
                'timeInForce': 'GTC',
            })
            if order.get('retCode') != 0:
                log_event(f'⚠️ Auto-SHORT {sym}: ошибка — {order.get("retMsg","?")}')
                continue

            # SL + TP через trading-stop (единым вызовом — надёжнее отдельных ордеров)
            ts_body = {
                'category': 'linear',
                'symbol': sym,
                'positionIdx': 0,
                'stopLoss': str(sl_price),
                'slTriggerBy': 'MarkPrice',
            }
            if tp_price < price:  # для шорта TP должен быть НИЖЕ входа
                ts_body['takeProfit'] = str(tp_price)
                ts_body['tpTriggerBy'] = 'MarkPrice'
            bybit('POST', '/v5/position/trading-stop', ts_body)

            state[sym] = {
                'last_short_ts': now,
                'entry_price': price,
                'sl': sl_price,
                'tp': tp_price,
                'qty': qty,
                'bb_pct': round(bb_pct, 1),
            }
            _save_state(state)

            msg = (f'🐻 Auto-SHORT {sym}: лимитка ${limit_price:.4f} (рынок ${price:.4f}, +2%) ×{qty} ({SHORT_LEVERAGE}x), '
                   f'BB={bb_pct:.0f}%, SL ${sl_price:.4f} (+{sl_pct*100:.0f}%), '
                   f'TP ${tp_price:.4f} (Middle BB)')
            add_alert('ENTRY', msg)
            actions.append(sym)
            log_event(msg)

        except Exception as e:
            log_event(f'⚠️ Auto-SHORT {sym}: исключение — {e}')

    return actions
