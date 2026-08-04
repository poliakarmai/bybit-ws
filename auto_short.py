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

from .api import bybit, get_bb_data, fetch_orders
from .alerts import log_event, add_alert
from .config import Config

# WebSocket BB-кеш (Фаза 6) — feature flag BYBIT_WS_BB_ENABLED
_WS_BB_ENABLED = os.environ.get('BYBIT_WS_BB_ENABLED', '1') == '1'

def _get_bb_ws(symbol, interval='D'):
    """Получить BB: сначала WS-кеш, fallback на REST."""
    if _WS_BB_ENABLED:
        try:
            from .ws_client import get_bb as ws_get_bb, is_connected as ws_alive, is_stale as ws_stale
            if ws_alive() and not ws_stale(300):
                bb = ws_get_bb(symbol, interval)
                if bb and bb.get('upper', 0) > 0:
                    return bb
        except Exception:
            pass
    return get_bb_data(symbol, interval)
from .position_sizing import margin_for_strategy
from .file_utils import safe_json_write
from .state_db import db  # SQLite dual-write

SHORT_STATE_FILE = os.path.expanduser('~/.local/share/bybit-ws/short_positions.json')

# Фаза 6.8: Throttle «сухих» символов — если 3+ цикла подряд без входа → пропускать 30 мин
DRY_SPELL_THRESHOLD = 3       # после 3 холостых проверок — throttle
DRY_SPELL_COOLDOWN = 1800     # 30 минут пропуска

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
    safe_json_write(SHORT_STATE_FILE, state)
    # Dual-write в SQLite
    for sym, data in state.items():
        try:
            db.save_short_state(sym, data)
        except Exception as e:
            log_event(f'⚠️ save_short_state({sym}): {e}')


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


def _check_short_mtf(sym: str):
    """Фаза 4.3.1: проверить D/W/M конфлюенс для SHORT-сигнала.
    
    Возвращает dict с confluence info или None если нет данных (не фильтруем).
    Возвращает False если конфлюенс < 2/3 (фильтруем).
    """
    from .mtf_confirmation import check_confluence
    
    try:
        conf = check_confluence(sym, 'SHORT')
        if conf is None:
            return None  # нет данных — не фильтруем
        if not conf['approved']:
            log_event(
                f'🚫 MTF filter SHORT: {sym} confluence={conf["confluence"]}/3 '
                f'({conf["filter_reason"]})'
            )
            return False
        
        # ── Фаза 4.3.5: paper-трекинг конфлюенса ──
        try:
            from .confluence_paper import track_signal
            track_signal(sym, 'SHORT', 0, 0, conf['confluence'])
        except Exception as e:
            log_event(f'⚠️ confluence_paper SHORT {sym}: {e}')

        # ── Фаза 4.3.4: алерт при конфлюенсе 3/3 (ДО входа, с дедупликацией 30 мин) ──
        if conf['confluence'] == 3:
            from .alerts import add_alert as _add_alert
            _add_alert('CONFLUENCE',
                f'🔥 STRONG CONFLUENCE: {sym} SHORT D+W+M — '
                f'ручной вход или увеличенная позиция!'
            )
        
        return conf  # возвращаем полный dict для sizing
    except Exception as e:
        log_event(f'⚠️ MTF filter SHORT {sym}: {e}')
        return None  # ошибка — не блокируем вход
    
    
def _count_up_days(sym: str) -> int:
    """Считает количество последовательных дней РОСТА (для SHORT-скоринга)."""
    try:
        from .api import bybit as _bybit
        r = _bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=8')
        if r and r.get('retCode') == 0:
            candles = r['result'].get('list', [])
            closes = [float(c[4]) for c in reversed(candles)]
            up = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] > closes[i-1]:
                    up += 1
                else:
                    break
            return up
    except Exception as e:
        log_event(f'⚠️ count_up_days({sym}): {e}')
    return 0


def short_score_coin(sym: str, bb_data: dict, ticker: dict, is_junk: bool) -> dict:
    """9-метричный SHORT-скоринг (v1.0). Возвращает {score, breakdown, ...} или None.
    
    Метрики (0-50):
    1. BB score (0-15): высокая позиция на BB = хорошо для SHORT
    2. Volume score (0-10): оборот — ликвидность
    3. Up days (0-10): последовательные дни роста = перегретость
    4. Funding (0-5): нейтральный/отрицательный фандинг = хорошо для SHORT
    5. BB Width (0-5): умеренная волатильность
    6. Quality (0-5): позиция × ширина BB
    """
    if not bb_data:
        return None

    bb_pos = bb_data.get('bb_pos', 
        (float(ticker.get('lastPrice', 0)) - float(bb_data.get('lower', 0))) / 
        (float(bb_data.get('upper', 1)) - float(bb_data.get('lower', 0))) * 100 
        if float(bb_data.get('upper', 1)) != float(bb_data.get('lower', 0)) else 50)
    bb_width = bb_data.get('bb_width', 0)
    cur = float(ticker.get('lastPrice', bb_data.get('current', 0)))

    # 1. BB score (0-15) — зеркально LONG
    if bb_pos >= 90:      bb_score = 15
    elif bb_pos >= 75:    bb_score = 12
    elif bb_pos >= 60:    bb_score = 8
    elif bb_pos >= 40:    bb_score = 5
    elif bb_pos >= 25:    bb_score = 3
    else:                 bb_score = 1

    # Auto-skip: BB < 20% — неинтересно для SHORT
    if bb_pos < 20:
        return None

    # 2. Volume score (0-10) — как у LONG
    turnover = float(ticker.get('turnover24h', 0) or 0)
    if turnover < 1_000_000:
        return None
    if turnover > 500_000_000:     vol_score = 10
    elif turnover > 100_000_000:   vol_score = 8
    elif turnover > 50_000_000:    vol_score = 7
    elif turnover > 20_000_000:    vol_score = 6
    elif turnover > 10_000_000:    vol_score = 5
    elif turnover > 5_000_000:     vol_score = 4
    else:                          vol_score = 2

    # 3. Up days (0-10) — зеркально down_days
    up = _count_up_days(sym)
    if up >= 5:      up_score = 10
    elif up >= 3:    up_score = 8
    elif up >= 2:    up_score = 5
    elif up >= 1:    up_score = 3
    else:            up_score = 1

    # 4. Funding (0-5) — отрицательный фандинг = шортерам платят
    funding = float(ticker.get('fundingRate', 0) or 0)
    # Для SHORT: negative funding = отлично, neutral = хорошо, positive = penalty
    if funding < -0.0002:      fund_score = 5   # шортерам платят
    elif funding < -0.0001:    fund_score = 4
    elif abs(funding) < 0.00005: fund_score = 3  # нейтрально
    elif funding < 0.0001:     fund_score = 2
    elif funding < 0.0002:     fund_score = 1
    else:                      fund_score = 0   # дорогой фандинг для шорта

    # 5. BB Width / Volatility (0-5)
    if 3 <= bb_width <= 8:     vola_score = 5
    elif 1 <= bb_width < 3:    vola_score = 3
    elif 8 < bb_width <= 15:   vola_score = 3
    else:                      vola_score = 1

    # 6. Quality score (0-5) — позиция × ширина, инвертировано
    quality = ((100 - bb_pos) / 100) * bb_width if bb_width > 0 else 3
    if quality <= 0.5:        qscore = 5
    elif quality <= 1.5:      qscore = 4
    elif quality <= 3.0:      qscore = 3
    elif quality <= 5.0:      qscore = 2
    else:                     qscore = 1

    # ── Бонус для JUNK: дневной памп добавляет очков ──
    bonus = 0
    bonus_label = ''
    if is_junk:
        chg_pct = float(ticker.get('price24hPcnt', 0) or 0)
        if chg_pct >= 1.5:     bonus = 8; bonus_label = f' Pump+{int(chg_pct*100)}%'
        elif chg_pct >= 1.0:   bonus = 5; bonus_label = f' Pump+{int(chg_pct*100)}%'
        elif chg_pct >= 0.8:   bonus = 3; bonus_label = f' Pump+{int(chg_pct*100)}%'

    # ── World Model (0-5): инвертировано для SHORT (отрицательный return = хорошо) ──
    wm_score = 0
    wm_label = ''
    if os.environ.get('BYBIT_WORLD_MODEL', '0') == '1':
        try:
            from .lstm_world_model import get_cached_world_prediction as _get_wm
            wm = _get_wm(sym)
            if wm:
                predicted_return = wm.get('predicted_return_pct')
                if predicted_return is not None:
                    if predicted_return < -0.5:
                        wm_score = 5; wm_label = f' WM+{wm_score}'
                    elif predicted_return <= 0.5:
                        wm_score = 3; wm_label = f' WM+{wm_score}'
                    else:
                        wm_score = 1; wm_label = f' WM+{wm_score}'
        except Exception as e:
            log_event(f'⚠️ world_model SHORT({sym}): {e}')

    total = bb_score + vol_score + up_score + fund_score + vola_score + qscore + bonus + wm_score

    return {
        'symbol': sym,
        'score': total,
        'max_score': (58 if is_junk else 50) + 5,
        'bb_pos': bb_pos,
        'bb_width': bb_width,
        'cur': cur,
        'breakdown': f'BB={bb_score} Vol={vol_score} Up={up_score} Fund={fund_score} Vola={vola_score} Q={qscore}{bonus_label}{wm_label}',
    }


def check_auto_short(positions):
    """Сканировать перегретые монеты и ставить SHORT.
    Вызывается каждые 10 циклов (5 мин)."""
    cfg = Config()

    # ── Фаза 5.4: проверка режимного флага SHORT ──
    from . import REGIME_SHORT_ENABLED
    if not REGIME_SHORT_ENABLED:
        log_event('🚫 REGIME_AUTO: SHORT отключён по режиму рынка')
        return []

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
    processed_syms = set()  # Фаза 6.8: трекинг для dry spell

    # Получаем топ-80 тикеров
    try:
        data = bybit('GET', '/v5/market/tickers?category=linear')
        if not data or data.get('retCode') != 0:
            return actions
        tickers = data['result'].get('list', [])
    except Exception as e:
        log_event(f'⚠️ auto_short: tickers API error: {e}')
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

        # Фаза 6.8: Throttle dry spells — пропускаем «сухие» символы
        if sym in state:
            dry_count = state[sym].get('dry_spell_count', 0)
            dry_since = state[sym].get('dry_spell_since', 0)
            if dry_count >= DRY_SPELL_THRESHOLD and now - dry_since < DRY_SPELL_COOLDOWN:
                continue
            if dry_since and now - dry_since >= DRY_SPELL_COOLDOWN:
                state[sym]['dry_spell_count'] = 0
                state[sym]['dry_spell_since'] = 0

        is_junk = sym not in TIER_AB  # Tier C/D = шлак

        # Проверка BB

        # Проверка BB
        try:
            bb = _get_bb_ws(sym, 'D')
            if not bb:
                continue
            upper = float(bb.get('upper', 0))
            middle = float(bb.get('middle', 0))
            lower = float(bb.get('lower', 0))
            if upper <= 0 or upper == lower:
                continue
            bb_pct = (last_price - lower) / (upper - lower) * 100 if upper != lower else 0
        except Exception as e:
            log_event(f'⚠️ auto_short BB {sym}: {e}')
            continue

        processed_syms.add(sym)  # Фаза 6.8: символ прошёл BB — считаем проверенным

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

        # ── Фаза 4.3.1: Multi-TF конфлюенс-фильтр для SHORT ──
        mtf_conf = _check_short_mtf(sym)
        if mtf_conf is False:
            continue

        # Проверка time budget — останавливаемся если подходим к таймауту
        if time.time() > deadline:
            log_event(f'⏱️ check_auto_short: budget исчерпан (обработано {len(actions)} входов)')
            break

        # Шорт! Рассчитываем параметры
        # ── Фаза 5.7: 9-метричный SHORT-скоринг ──
        short_sc = short_score_coin(sym, bb, t, is_junk)
        short_score = short_sc['score'] if short_sc else 35  # fallback: средний скор
        normalized_short = min(10, max(5, short_score / 5))  # 25→5, 50→10
        short_margin = margin_for_strategy('short', score=normalized_short)
        # ── Фаза 4.3.6: MTF-конфлюенс → бонус к позиции ──
        if isinstance(mtf_conf, dict):
            mtf_c = mtf_conf.get('confluence', 0)
            if mtf_c == 3:
                short_margin *= 1.5   # +50% при 3/3
            elif mtf_c == 2:
                short_margin *= 1.15  # +15% при 2/3
        if short_margin <= 0:
            continue
        usdt_qty = short_margin * SHORT_LEVERAGE
        qty_step = _get_lot_step(sym)
        qty = math.ceil(usdt_qty / last_price / qty_step) * qty_step
        # Round to qty_step decimals (fixes floating-point: 4.6000000000000005 → 4.6)
        qty_decimals = len(str(qty_step).split('.')[1]) if '.' in str(qty_step) else 0
        qty = round(qty, qty_decimals)
        if qty <= 0:
            continue

        price = _round_to_tick(last_price, sym)
        tp_price = _round_to_tick(middle, sym)

        try:
            # Проверка: нет ли уже pending лимитного Sell на этот символ (дедупликация)
            try:
                all_orders = fetch_orders()
                if not isinstance(all_orders, dict):
                    log_event(f'⚠️ Auto-SHORT {sym}: fetch_orders вернул не dict ({type(all_orders).__name__}), пропуск')
                    continue
                pending_sells = [o for o in all_orders.values() if o.get('symbol') == sym
                                 and o.get('side') == 'Sell' 
                                 and o.get('orderStatus') == 'New'
                                 and o.get('orderType') == 'Limit']
                if pending_sells:
                    log_event(f'⏭️ Auto-SHORT {sym}: уже есть pending лимитка Sell, пропуск')
                    continue
            except Exception as e:
                log_event(f'⚠️ Auto-SHORT {sym}: fetch_orders error, пропуск ({e})')
                continue

            # ── Проверка свободных средств перед отправкой ордера ──
            try:
                wallet = bybit('GET', '/v5/account/wallet-balance?accountType=UNIFIED')
                available_usdt = 0.0
                if wallet and wallet.get('retCode') == 0:
                    for acc in wallet['result'].get('list', []):
                        for coin in acc.get('coin', []):
                            if coin.get('coin') == 'USDT':
                                balance = float(coin.get('walletBalance', 0))
                                margin_used = float(coin.get('totalPositionIM', 0))
                                available_usdt = balance - margin_used
                                break
                required = short_margin * 1.05
                if available_usdt < required:
                    log_event(f'💰 LOW FUNDS SHORT {sym}: need ${required:.1f}, have ${available_usdt:.1f} — skipping')
                    continue
            except Exception:
                pass

            # ── Orderbook imbalance filter (27.06) ──
            try:
                from .orderbook_filter import should_enter_by_imbalance
                ob_ok, ob_reason = should_enter_by_imbalance(sym, 'Sell')
                if not ob_ok:
                    log_event(f'📊 OB BLOCK {sym}: {ob_reason}')
                    continue
            except Exception:
                pass

            # ── Volume confirmation filter (28.06) ──
            try:
                from .volume_filter import volume_ok
                vol_ok, vol_reason = volume_ok(sym)
                if not vol_ok:
                    log_event(f'📊 VOL BLOCK {sym}: {vol_reason}')
                    continue
            except Exception:
                pass

            # ── Фаза 6.8: Cross-model entry judge (Nemotron) ──
            try:
                from .entry_judge import should_enter as judge_should_enter
                _est_sl = price * 1.03 if price > 0 else None  # SL на 3% выше для SHORT
                _limit_price = _round_to_tick(price * (1 + ENTRY_OFFSET), sym)
                can_enter, judge_reason = judge_should_enter(
                    symbol=sym, side='Sell', score=short_score,
                    bb_pos=bb_pct, entry_price=_limit_price,
                    sl_price=_est_sl,
                    funding_rate=short_sc.get('funding', 0.0) if short_sc else 0.0,
                    bb_lower=bb.get('lower', 0), bb_upper=bb.get('upper', 0),
                    mtf_confluence=mtf_conf.get('confluence', 0) if isinstance(mtf_conf, dict) else 0,
                )
                if not can_enter:
                    log_event(f'🧑‍⚖️ ENTRY JUDGE BLOCK SHORT {sym}: {judge_reason}')
                    continue
            except Exception as e:
                log_event(f'⚠️ entry_judge short {sym}: {e}')
                continue  # fail-closed: при ошибке судьи — не входим

            # ── Symbol concentration check (28.06.2026) ──
            try:
                from .risk_manager import check_symbol_concentration
                conc_ok, conc_reason, conc_pct = check_symbol_concentration(sym, short_margin, positions)
                if not conc_ok:
                    log_event(f'⚠️ CONC BLOCK SHORT {sym}: {conc_reason}')
                    continue
            except Exception:
                pass

            # Лимитный SHORT: Sell выше рынка на +entry_offset% — ждём отскока для входа
            limit_price = _round_to_tick(price * (1 + ENTRY_OFFSET), sym)
            order = None
            for pos_idx in (0, 1, 2):
                order = bybit('POST', '/v5/order/create', {
                    'category': 'linear',
                    'symbol': sym,
                    'side': 'Sell',
                    'orderType': 'Limit',
                    'qty': str(qty),
                    'price': str(limit_price),
                    'positionIdx': pos_idx,
                    'timeInForce': 'GTC',
                })
                if order and order.get('retCode') == 0:
                    break
            if not order or order.get('retCode') != 0:
                log_event(f'⚠️ Auto-SHORT {sym}: ошибка — {order.get("retMsg","?") if order else "no response"}')
                continue

            state_entry = {
                'last_short_ts': now,
                'entry_price': price,
                'qty': qty,
                'bb_pct': round(bb_pct, 1),
                'is_junk': is_junk,
            }

            # ── MTF-бонус (один раз, для обеих веток) ──
            mtf_bonus = ''
            if isinstance(mtf_conf, dict):
                c = mtf_conf.get('confluence', 0)
                mtf_bonus = f' | MTF:{c}/3'
                if c == 3: mtf_bonus += ' +50%'
                elif c == 2: mtf_bonus += ' +15%'

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
                # Хард-SL на +25% — защита если DCA не сработает
                state_entry['hard_sl'] = round(price * 1.25, 6)

                state[sym] = state_entry
                _save_state(state)

                dca_str = ', '.join(f'+{d["mult"]*100:.0f}% @ ${d["price"]:.4f}' for d in dca_placed)
                msg = (f'🔴 SHORT JUNK {sym}: вход ${price:.6f} лимит ${limit_price:.6f} ×{qty} ({SHORT_LEVERAGE}x) | '
                       f'score={short_score} памп +{chg_pct*100:.0f}% | TP ${tp_price:.6f} | DCA: {dca_str}{mtf_bonus}')
                add_alert('ENTRY', msg)
                actions.append(sym)
                log_event(msg)

            else:
                # ── Tier A/B: SL отложен на 20 мин (23.06.2026), TP сразу ──
                sl_pct = SL_PCT_JUNK if sym not in TIER_AB else SL_PCT
                sl_price = _round_to_tick(price * (1 + sl_pct), sym)

                # TP ставим сразу, SL — через trading-stop без stopLoss
                ts_body = {
                    'category': 'linear',
                    'symbol': sym,
                    'positionIdx': 0,
                }
                if tp_price < price:  # для шорта TP должен быть НИЖЕ входа
                    ts_body['takeProfit'] = str(tp_price)
                    ts_body['tpTriggerBy'] = 'MarkPrice'
                
                # SL НЕ ставим при входе — auto_sl поставит через 20 мин
                # Но если шлак (is_junk) — ставим SL сразу
                if sym not in TIER_AB:
                    ts_body['stopLoss'] = str(sl_price)
                    ts_body['slTriggerBy'] = 'MarkPrice'
                    state_entry['no_sl'] = False
                else:
                    state_entry['no_sl'] = True
                    state_entry['sl_pending'] = sl_price  # будет установлен auto_sl через 20 мин
                
                bybit('POST', '/v5/position/trading-stop', ts_body)

                state_entry['sl'] = sl_price
                state_entry['tp'] = tp_price

                state[sym] = state_entry
                _save_state(state)

                sl_note = '⏳SL-20min' if state_entry.get('no_sl') else f'SL ${sl_price:.4f} (+{sl_pct*100:.0f}%)'
                msg = (f'🔴 SHORT {sym}: вход ${price:.6f} лимит ${limit_price:.6f} ×{qty} ({SHORT_LEVERAGE}x) | '
                       f'score={short_score} BB={bb_pct:.0f}% | {sl_note} | '
                       f'TP ${tp_price:.4f}{mtf_bonus}')
                add_alert('ENTRY', msg)
                actions.append(sym)
                log_event(msg)

        except Exception as e:
            log_event(f'⚠️ Auto-SHORT {sym}: исключение — {e}')

    # Фаза 6.8: Dry spell tracking — символы без входов получают штраф
    action_syms = set(actions)
    for sym in processed_syms:
        if sym not in state:
            state[sym] = {}
        if sym in action_syms:
            # Был вход → сброс dry spell
            state[sym]['dry_spell_count'] = 0
            state[sym]['dry_spell_since'] = 0
        else:
            # Нет входа → инкремент dry spell
            state[sym]['dry_spell_count'] = state[sym].get('dry_spell_count', 0) + 1
            if not state[sym].get('dry_spell_since'):
                state[sym]['dry_spell_since'] = now
    if processed_syms:
        _save_state(state)

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
    MAX_LOSS_PCT = getattr(junk_cfg, 'max_loss_pct', 15) / 100 if junk_cfg is not None else 0.15
    MAX_HOLD_HOURS = getattr(junk_cfg, 'max_hold_hours', 48) if junk_cfg is not None else 48

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

        # ── Hard SL check (абсолютный уровень, +25% от входа) ──
        hard_sl = entry.get('hard_sl', 0)
        if hard_sl > 0 and mark_price >= hard_sl:
            try:
                _close_junk_position(sym, pos)
                pnl_pct = (mark_price - entry_price) / entry_price * 100
                msg = (f'🛑 HARD-SL JUNK {sym}: +{pnl_pct:.1f}% > hard SL ${hard_sl:.6f} | '
                       f'вход ${entry_price:.6f} → выход ${mark_price:.6f}')
                add_alert('STOP', msg)
                log_event(msg)
                actions.append(sym)
                del state[sym]
                _save_state(state)
            except Exception as e:
                log_event(f'⚠️ Junk-HardSL {sym}: ошибка — {e}')
            continue

        # ── Max loss check (hard stop) ──
        margin_used = float(pos.get('positionIM', pos.get('margin', 0)) or 0)
        unrealised_pnl = float(pos.get('unrealisedPnl', pos.get('upnl', 0)) or 0)

        if margin_used > 0 and unrealised_pnl < 0:
            loss_pct = abs(unrealised_pnl) / margin_used
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
    idx = int(pos.get('positionIdx', 0))
    try:
        order = bybit('POST', '/v5/order/create', {
            'category': 'linear', 'symbol': sym, 'side': 'Buy',
            'orderType': 'Market', 'qty': str(size),
            'positionIdx': idx, 'timeInForce': 'IOC',
            'reduceOnly': True,
        })
        if order.get('retCode') == 0:
            return
    except Exception as e:
        log_event(f'⚠️ auto_short close_short({sym}): {e}')
