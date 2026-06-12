"""Авто-SHORT по перегреву (BB Daily > 85%) + шлак-режим (дневной рост ≥80%).

Зеркало LONG-стратегии: когда цена перегрета — шорт с возвратом к Middle BB.

Правила:
- BB Daily > 85% (цена у Upper или выше)
- Все Tier'ы (S/A/B/C/D) — шортим любой перегрев
- One-way монеты исключены (там нельзя SHORT)

Tier A/B (обычный режим):
- Плечо 3x, маржа $10
- SL: +5% от входа (через trading-stop)
- TP: Middle BB (через takeProfit в trading-stop)

Tier C/D — шлак-режим (NEW):
- Дневной рост ≥ 80% — обязательный фильтр
- БЕЗ стоп-лосса (шлак слишком волатильный, SL только жрёт маржу)
- max_loss_pct: 15% — hard market-close при убытке >15% маржи
- max_hold_hours: 48 — авто-закрытие через 48ч
- DCA-лесенка: +100% и +120% от входа (лимитные Sell)
- TP: Middle BB (reduceOnly limit Buy)

Общие:
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
from .position_sizing import margin_for_strategy

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
    except Exception as e:
        log_event(f'⚠️ auto_short: {e}')
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
    except Exception as e:
        log_event(f'⚠️ auto_short: {e}')
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
    BANNED = set(cfg.risk.get('banned_symbols', []))
    BB_SHORT_THRESHOLD = cfg.strategy.short.bb_threshold
    SHORT_MARGIN = cfg.strategy.short.margin
    SHORT_LEVERAGE = cfg.strategy.short.leverage
    SL_PCT = cfg.strategy.short.sl_tier_ab
    SL_PCT_JUNK = cfg.strategy.short.sl_tier_cd
    MAX_SHORTS = cfg.strategy.short.max_positions
    COOLDOWN = cfg.strategy.short.cooldown_seconds
    ENTRY_OFFSET = cfg.strategy.short.entry_offset
    JUNK_PUMP_THRESHOLD = getattr(cfg.strategy, 'junk', None)
    if JUNK_PUMP_THRESHOLD is not None:
        JUNK_PUMP_THRESHOLD = getattr(JUNK_PUMP_THRESHOLD, 'daily_pump_threshold', 0.80)
    else:
        JUNK_PUMP_THRESHOLD = getattr(cfg.strategy.short, 'junk_daily_pump_threshold', 0.80)
    JUNK_DCA_LEVELS = getattr(cfg.strategy, 'junk', None)
    if JUNK_DCA_LEVELS is not None:
        JUNK_DCA_LEVELS = getattr(JUNK_DCA_LEVELS, 'dca_levels', [1.0, 1.2])
    else:
        JUNK_DCA_LEVELS = getattr(cfg.strategy.short, 'junk_dca_levels', [1.0, 1.2])

    state = _load_state()
    now = time.time()
    deadline = now + 20  # time budget — не дольше 20с на все BB-запросы

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
                  and t['symbol'] not in live_syms
                  and t['symbol'] not in BANNED]

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

        is_junk = sym not in TIER_AB  # Tier C/D = шлак

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

        # Early exit: если time budget исчерпан — не тратим время на threshold-проверки
        if time.time() > deadline:
            log_event(f'⏱️ check_auto_short: budget исчерпан на {sym} (кандидатов проверено, actions={len(actions)})')
            break

        # ── Шлак-режим: дневной рост ≥ 80% ──
        if is_junk:
            chg_pct = float(t.get('price24hPcnt', 0) or 0)
            if chg_pct < JUNK_PUMP_THRESHOLD:
                continue
            # BB тоже должен быть перегрет (но порог мягче — 70% вместо 85%)
            if bb_pct < 70:
                continue
        else:
            # Tier A/B — обычный фильтр BB
            if bb_pct < BB_SHORT_THRESHOLD:
                continue

        # Проверка time budget — останавливаемся если подходим к таймауту
        if time.time() > deadline:
            log_event(f'⏱️ check_auto_short: budget исчерпан (обработано {len(actions)} входов)')
            break

        # Шорт! Рассчитываем параметры
        short_margin = margin_for_strategy('short', score=7.0)
        if short_margin <= 0:
            continue
        usdt_qty = short_margin * SHORT_LEVERAGE
        qty_step = _get_lot_step(sym)
        qty = math.ceil(usdt_qty / last_price / qty_step) * qty_step
        if qty <= 0:
            continue

        price = _round_to_tick(last_price, sym)
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

            state_entry = {
                'last_short_ts': now,
                'entry_price': price,
                'qty': qty,
                'bb_pct': round(bb_pct, 1),
                'is_junk': is_junk,
            }

            if is_junk:
                # ── Шлак: без SL, с DCA-лесенкой ──
                chg_pct = float(t.get('price24hPcnt', 0) or 0)

                # TP отдельным reduceOnly лимитным Buy
                if tp_price < price:
                    bybit('POST', '/v5/order/create', {
                        'category': 'linear',
                        'symbol': sym,
                        'side': 'Buy',
                        'orderType': 'Limit',
                        'qty': str(qty),
                        'price': str(tp_price),
                        'positionIdx': 0,
                        'timeInForce': 'GTC',
                        'reduceOnly': True,
                    })

                # DCA-лимитки: Sell на +100% и +120% от входа
                dca_placed = []
                for dca_mult in JUNK_DCA_LEVELS:
                    dca_price = _round_to_tick(price * (1 + dca_mult), sym)
                    dca_qty = qty  # такой же размер
                    dca_order = bybit('POST', '/v5/order/create', {
                        'category': 'linear',
                        'symbol': sym,
                        'side': 'Sell',
                        'orderType': 'Limit',
                        'qty': str(dca_qty),
                        'price': str(dca_price),
                        'positionIdx': 0,
                        'timeInForce': 'GTC',
                    })
                    if dca_order.get('retCode') == 0:
                        dca_placed.append({'mult': dca_mult, 'price': dca_price, 'qty': dca_qty})

                state_entry['no_sl'] = True
                state_entry['tp'] = tp_price
                state_entry['dca_levels'] = JUNK_DCA_LEVELS
                state_entry['dca_placed'] = dca_placed
                state_entry['pump_pct'] = round(chg_pct * 100, 1)

                state[sym] = state_entry
                _save_state(state)

                dca_str = ', '.join(f'+{d["mult"]*100:.0f}% @ ${d["price"]:.4f}' for d in dca_placed)
                msg = (f'🔴 SHORT JUNK {sym}: вход ${price:.6f} лимит ${limit_price:.6f} ×{qty} ({SHORT_LEVERAGE}x) | '
                       f'памп +{chg_pct*100:.0f}% | TP ${tp_price:.6f} | DCA: {dca_str}')
                add_alert('ENTRY', msg)
                actions.append(sym)
                log_event(msg)

            else:
                # ── Tier A/B: обычный режим с SL + TP ──
                sl_pct = SL_PCT_JUNK if sym not in TIER_AB else SL_PCT
                sl_price = _round_to_tick(price * (1 + sl_pct), sym)

                # SL + TP через trading-stop (единым вызовом)
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

                state_entry['sl'] = sl_price
                state_entry['tp'] = tp_price
                state_entry['no_sl'] = False

                state[sym] = state_entry
                _save_state(state)

                msg = (f'🔴 SHORT {sym}: вход ${price:.6f} лимит ${limit_price:.6f} ×{qty} ({SHORT_LEVERAGE}x) | '
                       f'BB={bb_pct:.0f}% | SL ${sl_price:.4f} (+{sl_pct*100:.0f}%) | '
                       f'TP ${tp_price:.4f}')
                add_alert('ENTRY', msg)
                actions.append(sym)
                log_event(msg)

        except Exception as e:
            log_event(f'⚠️ Auto-SHORT {sym}: исключение — {e}')

    return actions


def check_junk_dca(positions):
    """Проверить открытые шлак-шорты: DCA-уровни + max_loss + max_hold.

    Вызывается каждые 10 циклов вместе с check_auto_short."""
    cfg = Config()
    SHORT_LEVERAGE = cfg.strategy.short.leverage
    # Динамическая маржа для DCA (шлак — score 5.5)
    short_margin = margin_for_strategy('short', score=5.5)
    # Читаем junk-параметры из strategy.junk (с фоллбеком на старые ключи strategy.short)
    junk_cfg = getattr(cfg.strategy, 'junk', None)
    if junk_cfg is not None:
        JUNK_DCA_LEVELS = getattr(junk_cfg, 'dca_levels', [1.0, 1.2])
    else:
        JUNK_DCA_LEVELS = getattr(cfg.strategy.short, 'junk_dca_levels', [1.0, 1.2])
    MAX_LOSS_PCT = junk_cfg.get('max_loss_pct', 15) / 100  # 15% → 0.15
    MAX_HOLD_HOURS = junk_cfg.get('max_hold_hours', 48)

    state = _load_state()
    now = time.time()
    actions = []

    for sym, entry in list(state.items()):
        if not entry.get('is_junk'):
            continue

        # Если позиция закрылась — пропускаем
        if sym not in positions:
            # Но может быть лимитка ещё не сработала
            if not entry.get('entered', False):
                continue
            # Позиция закрылась — очищаем стейт
            del state[sym]
            _save_state(state)
            continue

        pos = positions[sym]
        if not isinstance(pos, dict) or pos.get('side') != 'Sell':
            continue

        entry_price = entry.get('entry_price', 0)
        if entry_price <= 0:
            continue

        mark_price = float(pos.get('mark', 0) or 0)
        if mark_price <= 0:
            continue

        # ── Max loss check (hard stop) ──
        margin_used = float(pos.get('positionIM', pos.get('margin', 0)) or 0)
        unrealised_pnl = float(pos.get('unrealisedPnl', pos.get('upnl', 0)) or 0)

        if margin_used > 0 and unrealised_pnl < 0:
            loss_pct = abs(unrealised_pnl) / (margin_used * SHORT_LEVERAGE)
            if loss_pct > MAX_LOSS_PCT:
                # Hard stop — закрываем по рынку
                try:
                    _close_junk_position(sym, pos)
                    msg = (f'🛑 STOP JUNK {sym}: убыток -{loss_pct*100:.1f}% > лимит {MAX_LOSS_PCT*100:.0f}% | '
                           f'вход ${entry_price:.6f} → выход ${mark_price:.6f} | PnL ${unrealised_pnl:+.2f}')
                    add_alert('STOP', msg)
                    log_event(msg)
                    actions.append(sym)
                    del state[sym]
                    _save_state(state)
                except Exception as e:
                    log_event(f'⚠️ Junk-STOP {sym}: ошибка — {e}')
                continue

        # ── Max hold hours check ──
        entry_ts = entry.get('last_short_ts', entry.get('entered_ts', 0))
        if entry_ts > 0:
            held_hours = (now - entry_ts) / 3600
            if held_hours > MAX_HOLD_HOURS and unrealised_pnl <= 0:
                try:
                    _close_junk_position(sym, pos)
                    msg = (f'⏰ TIMEOUT JUNK {sym}: {held_hours:.0f}ч > {MAX_HOLD_HOURS}ч лимит | '
                           f'выход ${mark_price:.6f} | PnL ${unrealised_pnl:+.2f}')
                    add_alert('STOP', msg)
                    log_event(msg)
                    actions.append(sym)
                    del state[sym]
                    _save_state(state)
                except Exception as e:
                    log_event(f'⚠️ Junk-Timeout {sym}: ошибка — {e}')
                continue

        # ── DCA levels ──
        dca_placed = entry.get('dca_placed', [])
        placed_multipliers = {d['mult'] for d in dca_placed}

        for dca_mult in JUNK_DCA_LEVELS:
            if dca_mult in placed_multipliers:
                continue

            dca_trigger = entry_price * (1 + dca_mult)
            if mark_price < dca_trigger:
                continue

            usdt_qty = short_margin * SHORT_LEVERAGE
            qty_step = _get_lot_step(sym)
            dca_qty = math.ceil(usdt_qty / mark_price / qty_step) * qty_step
            if dca_qty <= 0:
                continue

            dca_price = _round_to_tick(mark_price, sym)
            try:
                dca_order = bybit('POST', '/v5/order/create', {
                    'category': 'linear',
                    'symbol': sym,
                    'side': 'Sell',
                    'orderType': 'Limit',
                    'qty': str(dca_qty),
                    'price': str(dca_price),
                    'positionIdx': 0,
                    'timeInForce': 'GTC',
                })
                if dca_order.get('retCode') == 0:
                    dca_placed.append({'mult': dca_mult, 'price': dca_price, 'qty': dca_qty, 'ts': now})
                    entry['dca_placed'] = dca_placed
                    _save_state(state)

                    msg = (f'🔴 DCA JUNK {sym}: +{dca_mult*100:.0f}% @ ${dca_price:.4f} ×{dca_qty} | '
                           f'вход ${entry_price:.6f} → сейчас ${mark_price:.6f}')
                    add_alert('ENTRY', msg)
                    actions.append(sym)
                    log_event(msg)
            except Exception as e:
                log_event(f'⚠️ Junk-DCA {sym}: исключение — {e}')

    return actions


def _close_junk_position(sym, pos):
    """Закрыть шлак-позицию по рынку."""
    size = float(pos.get('size', 0))
    if size <= 0:
        return
    for idx in (0, 1):
        try:
            order = bybit('POST', '/v5/order/create', {
                'category': 'linear', 'symbol': sym, 'side': 'Buy',
                'orderType': 'Market', 'qty': str(size),
                'positionIdx': idx, 'timeInForce': 'IOC',
                'reduceOnly': True,
            })
            if order.get('retCode') == 0:
                return
            if order.get('retCode') == 10001:
                continue
        except Exception:
            continue
