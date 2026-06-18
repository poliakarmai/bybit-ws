"""Авто-вход по 9-метричному скорингу (v4.0)."""
import json, math, os, time
from . import BYBIT_CLI, DATA_DIR, MAX_POSITION_VALUE
from . import safe_run
from .api import bybit, get_bb_data
from .alerts import log_event, add_alert
from .config import Config
from .position_sizing import margin_for_strategy

AUTO_ENTRY_WATCH = [
    'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
    'HYPEUSDT','NEARUSDT','SUIUSDT','TONUSDT','WLDUSDT','LINKUSDT',
    'AAVEUSDT','AVAXUSDT','DOTUSDT','INJUSDT','ONDOUSDT','ARBUSDT',
    'ENAUSDT','FETUSDT','APTUSDT','ATOMUSDT','RUNUSDT',
]

COOLDOWN_FILE = os.path.join(DATA_DIR, 'cooldown.json')
MIN_SCORE = 25  # порог для авто-входа (из 50)
ML_ENABLED = os.getenv('BYBIT_ML_ENABLED', '1') == '1'  # фича-флаг: отключить весь ML

# ── Фаза 5.4: LSTM-режим → адаптивные параметры ──
REGIME_PARAMS = {
    'TRENDING_UP':   {'min_score': 15, 'entry_discount': 0.97, 'sl_tightness': 0.91, 'aggression': 1.2},
    'TRENDING_DOWN': {'min_score': 25, 'entry_discount': 0.93, 'sl_tightness': 0.95, 'aggression': 0.6},
    'RANGING':       {'min_score': 20, 'entry_discount': 0.95, 'sl_tightness': 0.93, 'aggression': 1.0},
    'HIGH_VOL':      {'min_score': 30, 'entry_discount': 0.90, 'sl_tightness': 0.96, 'aggression': 0.4},
    'LOW_VOL':       {'min_score': 20, 'entry_discount': 0.96, 'sl_tightness': 0.92, 'aggression': 1.0},
    'CHOPPY':        {'min_score': 30, 'entry_discount': 0.92, 'sl_tightness': 0.94, 'aggression': 0.5},
    'NEUTRAL':       {'min_score': 20, 'entry_discount': 0.95, 'sl_tightness': 0.93, 'aggression': 1.0},
}

_regime_cache = {'params': REGIME_PARAMS['NEUTRAL'], 'regime': 'NEUTRAL', 'conf': 50, 'ts': 0}
REGIME_CACHE_TTL = 3600  # 1 час


def _get_regime_params():
    """
    Адаптивные параметры для текущего рыночного режима.
    LSTM (5.4) → fallback regime.py.
    """
    global _regime_cache
    now = time.time()
    if now - _regime_cache['ts'] < REGIME_CACHE_TTL:
        return _regime_cache['params'], _regime_cache['regime'], _regime_cache['conf']

    regime = 'NEUTRAL'
    confidence = 50

    try:
        from .lstm_regime import predict_regime, get_cached_prediction
        data = predict_regime()
        if not data:
            data = get_cached_prediction()
        if data:
            regime = data.get('regime', 'NEUTRAL')
            confidence = data.get('confidence', 50)
    except Exception:
        pass

    if regime == 'NEUTRAL':
        try:
            from .regime import check_regime
            data = check_regime()
            regime = data.get('regime', 'NEUTRAL')
            confidence = data.get('confidence', 50)
        except Exception:
            pass

    params = REGIME_PARAMS.get(regime, REGIME_PARAMS['NEUTRAL'])
    _regime_cache = {'params': params, 'regime': regime, 'conf': confidence, 'ts': now}
    return params, regime, confidence

# ── Фаза 5.2: Per-symbol оптимальные параметры ──
PER_SYMBOL_CONFIG = {}
PER_SYMBOL_CONFIG_FILE = os.path.join(DATA_DIR, 'per_symbol_optimal.json')


def _load_per_symbol_config():
    global PER_SYMBOL_CONFIG
    try:
        if os.path.exists(PER_SYMBOL_CONFIG_FILE):
            with open(PER_SYMBOL_CONFIG_FILE) as f:
                PER_SYMBOL_CONFIG = json.load(f)
    except Exception:
        pass


def _get_symbol_param(sym: str, key: str, default):
    """Получить параметр для символа: per-symbol → global default."""
    sym_config = PER_SYMBOL_CONFIG.get(sym, {})
    return sym_config.get(key, default)


_load_per_symbol_config()  # загружаем при импорте


def _filter_by_mtf_confluence(scored: list, direction: str) -> list:
    """Фаза 4.3.1: фильтровать сигналы без D/W/M конфлюенса (≥2/3)."""
    from .mtf_confirmation import check_confluence

    filtered = []
    for s in scored:
        sym = s['symbol']
        conf = check_confluence(sym, direction)
        if conf is None:
            filtered.append(s)
            continue
        if conf['approved']:
            s['mtf'] = conf
            filtered.append(s)

            # ── Фаза 4.3.5: paper-трекинг конфлюенса ──
            try:
                from .confluence_paper import track_signal
                cur = s.get('cur', 0)
                track_signal(sym, direction, cur, cur,
                           conf['confluence'], s.get('score'))
            except Exception:
                pass

            # ── Фаза 4.3.4: алерт при конфлюенсе 3/3 (ДО входа) ──
            if conf['confluence'] == 3:
                from .alerts import add_alert as _add_alert
                _add_alert('ENTRY',
                    f'🔥 STRONG CONFLUENCE: {sym} {direction} D+W+M | '
                    f'score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}% — '
                    f'ручной вход или увеличенная позиция!'
                )
        else:
            from .alerts import log_event
            log_event(
                f'🚫 MTF filter: {sym} {direction} score={s["score"]} '
                f'confluence={conf["confluence"]}/3 ({conf["filter_reason"]})'
            )
    return filtered


def _load_cooldown():
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE) as f:
                return json.load(f)
        except Exception as e:
            log_event(f'⚠️ auto_entry: {e}')
    return {}


def record_sl_hit(sym: str):
    cd = _load_cooldown()
    cd[sym] = time.time()
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(cd, f)
    except Exception as e:
        log_event(f'⚠️ auto_entry: {e}')


def _parse_ticker_line(line: str) -> dict:
    """Парсит строку тикера из bybit-cli tickers в словарь."""
    parts = line.strip().split()
    if len(parts) < 7:
        return {}
    try:
        return {
            'symbol': parts[0],
            'last': float(parts[1].replace(',','')),
            'turnover24h': float(parts[-1].replace(',','')) if parts[-1].replace(',','').replace('.','').isdigit() else 0,
            'fundingRate': 0.0,  # bybit-cli tickers не показывает funding, берём из REST
        }
    except Exception:
        return {}


def _count_down_days(sym: str) -> int:
    """Считает количество последовательных дней падения."""
    try:
        r = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=8')
        if r and r.get('retCode') == 0:
            candles = r['result'].get('list', [])
            closes = [float(c[4]) for c in reversed(candles)]
            down = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] < closes[i-1]:
                    down += 1
                else:
                    break
            return down
    except Exception:
        pass  # ticker parse error — return 0 (no down days detected)
    return 0


def full_score_coin(sym: str, bb_data: dict, ticker_line: str) -> dict:
    """9-метричный LONG-скоринг (v4.0). Возвращает {score, breakdown, ...} или None если не подходит."""
    if not bb_data:
        return None

    bb_pos = bb_data['bb_pos']
    bb_width = bb_data.get('bb_width', 0)
    cur = bb_data['cur']

    # 1. BB score (0-15)
    if bb_pos <= 10:    bb_score = 15
    elif bb_pos <= 25:  bb_score = 12
    elif bb_pos <= 40:  bb_score = 8
    elif bb_pos <= 60:  bb_score = 5
    elif bb_pos <= 75:  bb_score = 3
    else:               bb_score = 1

    # Auto-skip: BB > 80% — неинтересно
    if bb_pos > 80:
        return None

    # 2. Volume score (0-10)
    t = _parse_ticker_line(ticker_line)
    turnover = t.get('turnover24h', 0)
    if turnover < 1_000_000:
        return None  # слишком мелкий
    if turnover > 500_000_000:    vol_score = 10
    elif turnover > 100_000_000:  vol_score = 8
    elif turnover > 50_000_000:   vol_score = 7
    elif turnover > 20_000_000:   vol_score = 6
    elif turnover > 10_000_000:   vol_score = 5
    elif turnover > 5_000_000:    vol_score = 4
    else:                         vol_score = 2

    # 3. Down days (0-10)
    down = _count_down_days(sym)
    if down >= 5:     down_score = 10
    elif down >= 3:   down_score = 8
    elif down >= 2:   down_score = 5
    elif down >= 1:   down_score = 3
    else:             down_score = 1

    # 4. Funding (0-5) — проверяем через REST
    fund_score = 3  # default если не получили
    try:
        fr = bybit('GET', f'/v5/market/tickers?category=linear&symbol={sym}')
        if fr and fr.get('retCode') == 0:
            tickers = fr['result'].get('list', [])
            if tickers:
                funding = float(tickers[0].get('fundingRate', 0))
                abs_f = abs(funding)
                if abs_f < 0.00005:        fund_score = 5
                elif abs_f < 0.0001:       fund_score = 4
                elif abs_f < 0.0002:       fund_score = 3
                elif abs_f < 0.0004:       fund_score = 2
                else:                      fund_score = 0
    except Exception:
        pass  # funding rate parse error — default score 0

    # 5. BB Width / Volatility (0-5)
    if 3 <= bb_width <= 8:    vola_score = 5
    elif 1 <= bb_width < 3:   vola_score = 3
    elif 8 < bb_width <= 15:  vola_score = 3
    else:                     vola_score = 1

    # 6. Quality score (0-5) — позиция на BB × ширина
    quality = (bb_pos / 100) * bb_width
    if quality <= 0.5:       qscore = 5
    elif quality <= 1.5:     qscore = 4
    elif quality <= 3.0:     qscore = 3
    elif quality <= 5.0:     qscore = 2
    else:                    qscore = 1

    total = bb_score + vol_score + down_score + fund_score + vola_score + qscore

    # ── Фаза 5.1: ML Gate — фильтр вместо бленда ──
    # ── Фаза 5.3: A/B-тестирование — группа B пропускает ML Gate ──
    import time as _time
    signal_id = f"{sym}:{int(_time.time())}:{total}"
    ml_passed = True
    ml_prob = None
    ab_group = None
    try:
        from .ab_test import assign_group as _assign_group
        ab_group = _assign_group(signal_id)

        if ab_group == 'A':
            # Группа A: ML Gate работает как обычно
            from .ml_scorer import ml_gate_pass
            signal_data = {
                'score': total / 5,
                'price': cur,
                'lower_bb': bb_data['lower'],
                'upper_bb': bb_data['upper'],
                'middle_bb': bb_data['middle'],
                'entry': bb_data['lower'] * 0.98,
                'timeframe': 'D',
                'mode': 'long',
            }
            ml_passed, ml_prob = ml_gate_pass(signal_data)
        # else: группа B — пропускаем без ML Gate (ml_passed уже True)
    except Exception:
        pass  # модель недоступна → полагаемся на эвристику

    if not ml_passed:
        return None  # ML gate: не входить

    ml_info = f' ML={ml_prob:.2f}' if ml_prob is not None else ''
    ab_info = f' AB={ab_group}' if ab_group else ''

    return {
        'symbol': sym,
        'score': total,
        'max_score': 50,
        'bb_pos': bb_pos,
        'bb_width': bb_width,
        'cur': cur,
        'signal_id': signal_id,
        'ab_group': ab_group,
        'ml_prob': ml_prob,
        'breakdown': f'BB={bb_score} Vol={vol_score} Down={down_score} Fund={fund_score} Vola={vola_score} Q={qscore}{ml_info}{ab_info}',
    }


def auto_entry_scan(positions):
    """Скрининг и авто-вход по 9-метричному скорингу (v4.0 + LSTM-режим 5.4)."""
    entries = []
    active = set(positions.keys())

    # ── Фаза 5.4: адаптивные параметры от режима ──
    regime_params, regime_name, regime_conf = _get_regime_params()
    min_score = regime_params['min_score']
    entry_discount_mult = regime_params['entry_discount']
    aggression = regime_params['aggression']
    all_watch = list(AUTO_ENTRY_WATCH)
    wl_file = os.path.join(DATA_DIR, 'watchlist_custom.txt')
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            custom = [l.strip() for l in f if l.strip() and l.strip().endswith('USDT')]
            all_watch.extend([s for s in custom if s not in all_watch])

    try:
        cfg = Config()
        banned = set(cfg.risk.get('banned_symbols', []))
        all_watch = [s for s in all_watch if s not in banned]
    except Exception as e:
        log_event(f'⚠️ auto_entry: {e}')

    try:
        r = safe_run([BYBIT_CLI, 'tickers'], timeout=15)
        if r.returncode != 0:
            return entries
        ticker_lines = r.stdout.split('\n')
    except Exception:
        return entries

    candidates = []
    for sym in all_watch:
        if sym in active:
            continue
        for line in ticker_lines:
            if sym in line:
                candidates.append((sym, line))
                break

    scored = []
    for sym, ticker_line in candidates:
        bb = get_bb_data(sym, 'D')
        if not bb:
            continue
        result = full_score_coin(sym, bb, ticker_line)
        if result and result['score'] >= min_score:
            scored.append(result)

    # ── Фаза 4.3.1: Multi-TF конфлюенс-фильтр ──
    scored = _filter_by_mtf_confluence(scored, 'LONG')

    scored.sort(key=lambda x: x['score'], reverse=True)

    for s in scored:
        try:
            sym = s['symbol']
            cooldown = _load_cooldown()
            cfg = Config()
            cooldown_sl = cfg.strategy.long.get('cooldown_after_sl', 14400)
            if sym in cooldown:
                elapsed = time.time() - cooldown[sym]
                if elapsed < cooldown_sl:
                    continue

            # Маржа от score с учётом агрессии режима (Фаза 5.4)
            normalized_score = min(10, s['score'] / 5)  # 25→5, 50→10
            margin = margin_for_strategy('long', score=normalized_score) * aggression
            if margin <= 0:
                continue

            # ── Фаза 4.3.6: MTF-конфлюенс → бонус к позиции ──
            confluence_mult = 1.0
            if 'mtf' in s:
                mtf_conf = s['mtf'].get('confluence', 0)
                if mtf_conf == 3:
                    confluence_mult = 1.5   # +50% при 3/3
                elif mtf_conf == 2:
                    confluence_mult = 1.15  # +15% при 2/3
            margin *= confluence_mult

            # Получаем BB для уровней входа
            bb2 = get_bb_data(sym, 'D')
            if not bb2:
                continue
            # Per-symbol оптимальный дисконт × режимный множитель (Фаза 5.2 + 5.4)
            sym_discount = _get_symbol_param(sym, 'entry_discount', 1.0)
            price = round(bb2['lower'] * sym_discount * entry_discount_mult, 4)
            qty = math.ceil(margin * 3 / price)
            if qty < 1:
                continue

            if sym in positions:
                existing_val = positions[sym]['mark'] * positions[sym]['size']
                if existing_val + price * qty > MAX_POSITION_VALUE:
                    continue

            pos_data = bybit('GET', f'/v5/position/list?category=linear&settleCoin=USDT&symbol={sym}')
            idx = 0
            if pos_data and pos_data.get('retCode') == 0:
                for p in pos_data['result'].get('list', []):
                    if float(p.get('size', 0)) > 0:
                        idx = int(p.get('positionIdx', 0))
                        break

            # ── Фаза 5.6: Ансамбль RF+LSTM+RL — стоит ли входить? ──
            ensemble_decision = None
            if ML_ENABLED:
                try:
                    from .ensemble import ensemble_should_enter as _ensemble_check
                    market_state = {
                        'regime': regime_name, 'regime_conf': regime_conf,
                        'mtf_confluence': s.get('mtf', {}).get('confluence', 2) if 'mtf' in s else 2,
                        'days_since_entry': 0, 'daily_return': 0.0,
                    }
                    signal_data = {
                        'score': s.get('score', 25), 'bb_pos': s.get('bb_pos', 50),
                        'bb_width': s.get('bb_width', 10), 'price': s.get('cur', 0),
                        'lower_bb': bb2.get('lower', 0), 'upper_bb': bb2.get('upper', 0),
                        'middle_bb': bb2.get('middle', 0),
                        'entry': s.get('cur', 0), 'timeframe': 'D', 'mode': 'long',
                        'funding': s.get('funding', 0.0),
                    }
                    ens_enter, ens_conf, ens_details = _ensemble_check(signal_data, market_state)
                    if not ens_enter:
                        ws = ens_details['weighted_score']
                        th = ens_details['threshold']
                        log_event(f'🤖 Ensemble skip {sym}: score={ws:.2f}<{th}')
                        continue
                    ensemble_decision = f'Ensemble({ens_conf:.0%})'
                except Exception:
                    pass  # ансамбль недоступен → входим по эвристике
                    log_event(f'⚠️ Ensemble error for {sym} — fallback to heuristic')

            # ── Повторная проверка: позиция могла появиться между снапшотом и ордером ──
            skip_symbol = False
            recheck = bybit('GET', f'/v5/position/list?category=linear&settleCoin=USDT&symbol={sym}')
            if recheck and recheck.get('retCode') == 0:
                for rp in recheck['result'].get('list', []):
                    if float(rp.get('size', 0)) > 0:
                        log_event(f'⏭️ Дубль {sym}: позиция уже есть — пропускаем')
                        skip_symbol = True
                        break
            if skip_symbol:
                continue

            body = {'category': 'linear', 'symbol': sym, 'side': 'Buy',
                    'orderType': 'Limit', 'qty': str(qty), 'price': str(price),
                    'positionIdx': idx, 'timeInForce': 'GTC'}
            result = bybit('POST', '/v5/order/create', body)
            if result and result.get('retCode') == 0:
                # ── Фаза 4.3.2: Telegram-алерт при входе ──
                mtf_info = ''
                if 'mtf' in s:
                    mtf = s['mtf']
                    bonus = ''
                    if mtf['confluence'] == 3:
                        bonus = ' +50%'
                    elif mtf['confluence'] == 2:
                        bonus = ' +15%'
                    mtf_info = f' | MTF:{mtf["confluence"]}/3({mtf["strength"]}{bonus})'
                regime_info = f' | 📊{regime_name}' if regime_name != 'NEUTRAL' else ''
                rl_info = f' | 🧠{ensemble_decision}' if ensemble_decision else ''

                entries.append(
                    f'🤖 Авто-вход {sym} @ ${price:.4f} x{qty} '
                    f'(score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}% {s["breakdown"]}{mtf_info}{regime_info}{rl_info})'
                )
                add_alert('ENTRY',
                    f'🚀 LONG {sym}: вход ${price:.4f} ×{qty} ({3}x) | '
                    f'score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}%{mtf_info}{regime_info}{rl_info}'
                )
                log_event(f'Авто-вход {sym} @ ${price:.4f} score={s["score"]} BB={s["bb_pos"]:.0f}%')
            elif result is None:
                # API не ответил — проверяем, не ушёл ли ордер на биржу
                orders_check = bybit('GET', f'/v5/order/realtime?category=linear&symbol={sym}&limit=1')
                if orders_check and orders_check.get('retCode') == 0:
                    open_orders = orders_check['result'].get('list', [])
                    if open_orders:
                        log_event(f'⚠️ Авто-вход {sym}: API таймаут но ордер создан — проверь вручную')
                        add_alert('ENTRY',
                            f'⚠️ LONG {sym}: ордер ушёл без подтверждения (таймаут API) — проверь!'
                        )
                # -- Фаза 5.3: запись в A/B тест --
                if s.get('signal_id'):
                    try:
                        from .ab_test import record_entry as _record_entry
                        _record_entry(s['signal_id'], sym, 'Buy', qty, price, s['score'], s.get('ml_prob'))
                    except Exception:
                        pass
        except Exception as e:
            log_event(f'⚠️ auto_entry {sym}: {e}')
            continue

    return entries
