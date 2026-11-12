"""Авто-вход по 9-метричному скорингу (v4.0)."""
import json
import os
import time
from decimal import ROUND_FLOOR, Decimal

from . import (
    BYBIT_CLI,
    DATA_DIR,
    MAX_POSITION_VALUE,
    learn_position_idx,
    position_idx,
    safe_run,
)
from .alerts import add_alert, log_event
from .api import account_wallet, bybit, get_bb_data, set_leverage, verify_leverage
from .config import Config
from .file_utils import safe_json_write
from .position_sizing import margin_for_strategy
from .universe import (  # noqa: F401 — AUTO_ENTRY_WATCH реэкспорт
    AUTO_ENTRY_WATCH,
    scan_universe,
)
from .ws_client import get_kline as _ws_kline

# WebSocket BB-кеш (Фаза 6) — feature flag BYBIT_WS_BB_ENABLED
_WS_BB_ENABLED = os.environ.get('BYBIT_WS_BB_ENABLED', '1') == '1'

# Рука bandit, выбранная на текущем скане: (sym, side) -> (regime, arm_idx).
# Заполняется при скоринге, забирается при успешном входе для record_arm_assignment.
_SCAN_ARMS = {}


def _get_bb_ws(symbol, interval='D'):
    """Получить BB: сначала WS-кеш, fallback на REST."""
    if _WS_BB_ENABLED:
        try:
            from .ws_client import get_bb as ws_get_bb
            from .ws_client import is_connected as ws_alive
            from .ws_client import is_stale as ws_stale
            if ws_alive() and not ws_stale(300):
                bb = ws_get_bb(symbol, interval)
                if bb and bb.get('lower', 0) > 0:
                    return bb
        except Exception:
            pass
    return get_bb_data(symbol, interval)

COOLDOWN_FILE = os.path.join(DATA_DIR, 'cooldown.json')
MIN_SCORE = 25  # порог для авто-входа (макс. score = 55: bb15+vol10+down10+fund5+vola5+q5+wm5)

# ── Anti-overtrading / anti-chasing guard'ы ──
MAX_DAILY_TRADES = 6      # дневной лимит LONG-входов (overtrading-защита)
CHASE_PCT = 3.0           # блок входа, если монета выросла >3%
CHASE_LOOKBACK = 5        # ...за последние 5 дневных свечей
_daily_entries = {'date': '', 'count': 0}
_FUNDING_CACHE: dict = {}  # bulk-снимок fundingRate, TTL 120с


def _count_today_entries() -> int:
    """Счётчик LONG-входов за сегодня (in-memory, сброс при смене даты)."""
    global _daily_entries
    today = time.strftime('%Y-%m-%d')
    if _daily_entries['date'] != today:
        _daily_entries = {'date': today, 'count': 0}
    return _daily_entries['count']
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
        from .lstm_regime import get_cached_prediction, predict_regime
        data = predict_regime()
        if not data:
            data = get_cached_prediction()
        if data:
            regime = data.get('regime', 'NEUTRAL')
            confidence = data.get('confidence', 50)
    except Exception as e:
        log_event(f'⚠️ check_regime LSTM: {e}')

    if regime == 'NEUTRAL':
        try:
            from .regime import check_regime
            data = check_regime()
            regime = data.get('regime', 'NEUTRAL')
            confidence = data.get('confidence', 50)
        except Exception as e:
            log_event(f'⚠️ check_regime fallback: {e}')

    params = REGIME_PARAMS.get(regime, REGIME_PARAMS['NEUTRAL'])
    _regime_cache = {'params': params, 'regime': regime, 'conf': confidence, 'ts': now}
    return params, regime, confidence

# ── Фаза 5.2: Per-symbol оптимальные параметры ──
PER_SYMBOL_CONFIG = {}
PER_SYMBOL_CONFIG_FILE = os.path.join(DATA_DIR, 'per_symbol_optimal.json')


def _load_per_symbol_config():
    global PER_SYMBOL_CONFIG
    # 1. Загружаем per_symbol_optimal.json (от optimize_params.py)
    try:
        if os.path.exists(PER_SYMBOL_CONFIG_FILE):
            with open(PER_SYMBOL_CONFIG_FILE) as f:
                PER_SYMBOL_CONFIG = json.load(f)
    except Exception as e:
        log_event(f'⚠️ per-symbol config load: {e}')

    # 2. Загружаем Optuna-параметры (Фаза 5.2 — optuna_tuner.py)
    _load_optuna_config()


def _load_optuna_config():
    """Загрузить Optuna-параметры и смержить в PER_SYMBOL_CONFIG."""
    try:
        from .optuna_tuner import is_optuna_enabled, load_optuna_params
        if is_optuna_enabled():
            optuna_params = load_optuna_params()
            if optuna_params:
                for sym, params in optuna_params.items():
                    if sym not in PER_SYMBOL_CONFIG:
                        PER_SYMBOL_CONFIG[sym] = {}
                    # Optuna-параметры переопределяют существующие
                    PER_SYMBOL_CONFIG[sym].update({
                        'optuna_bb_period': params.get('bb_period'),
                        'optuna_bb_std_mult': params.get('bb_std_mult'),
                        'optuna_sl_pct': params.get('sl_pct'),
                        'optuna_tp_pct': params.get('tp_pct'),
                        'optuna_min_score': params.get('min_score'),
                    })
                log_event(f'📊 Optuna: загружено {len(optuna_params)} символов из optuna_params.json')
    except Exception as e:
        log_event(f'⚠️ Optuna config load: {e}')


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
            # Нет данных D-TF → блокируем (не пускаем слепые входы)
            from .alerts import log_event
            log_event(f'🚫 MTF nodata: {sym} {direction} — нет данных D-TF, вход заблокирован')
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
            except Exception as e:
                log_event(f'⚠️ confluence_paper {sym} {direction}: {e}')

            # ── Фаза 4.3.4: алерт при конфлюенсе 3/3 (ДО входа, с дедупликацией 30 мин) ──
            if conf['confluence'] == 3:
                from .alerts import add_alert as _add_alert
                _add_alert('CONFLUENCE',
                    f'🔥 STRONG CONFLUENCE: {sym} {direction} D+W+M (score={s["score"]}/{s["max_score"]})'
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
        safe_json_write(str(COOLDOWN_FILE), cd)
    except Exception as e:
        log_event(f'⚠️ auto_entry: {e}')


def _parse_ticker_line(line: str) -> dict:
    """Парсит строку `bybit tickers`: `  1 BTCUSDT  $73656.100000  51,607  $3,819,640,447`.

    Разбор по смыслу, не по числу колонок: ранг может отсутствовать, `$` и
    запятые в числах, между ценой и объёмом бывает колонка изменения за 24ч.
    Символ — первая нечисловая колонка, turnover24h — последняя.
    ponytail: формат CLI живёт вне репо — если снова поменяется, парсер вернёт {}
    и скан молча умрёт; живой формат держит tests/test_auto_entry_parse.py.
    """
    parts = line.strip().split()
    if len(parts) < 4:
        return {}
    if parts[0].startswith(('#', '-')):  # шапка/разделитель вывода CLI — не данные
        return {}
    try:
        off = 1 if parts[0].isdigit() else 0

        def num(s: str) -> float:
            return float(s.replace('$', '').replace(',', ''))

        return {
            'symbol': parts[off],
            'last': num(parts[off + 1]),
            'turnover24h': num(parts[-1]),
            'fundingRate': 0.0,  # bybit-cli tickers не показывает funding, берём из REST
        }
    except Exception as e:
        log_event(f'⚠️ ticker_parse({line[:60]}): {e}')  # line, не sym: в сигнатуре только line
        return {}


def _open_entry_symbols() -> set:
    """Символы с уже висящей лимиткой на вход (снапшот orders.json)."""
    try:
        from . import ORDERS_SNAPSHOT
        from .snapshot import load_json
        snap = load_json(ORDERS_SNAPSHOT) or {}
        return {o['symbol'] for o in snap.values()
                if o.get('symbol')
                and o.get('status') in ('New', 'PartiallyFilled')
                and o.get('kind') in ('LIMIT_ENTRY', 'OTHER')}
    except Exception:
        return set()


def _funding_rate(sym: str):
    """Funding rate из bulk-снимка всех тикеров (TTL 120с).

    Раньше был отдельный REST на каждый символ: 46 запросов за скан, ~20-30с
    впустую — данные и так приходят одним ответом /v5/market/tickers.
    """
    global _FUNDING_CACHE
    now = time.time()
    if not _FUNDING_CACHE or now - _FUNDING_CACHE.get('ts', 0) > 120:
        r = bybit('GET', '/v5/market/tickers?category=linear')
        if r and r.get('retCode') == 0:
            _FUNDING_CACHE = {
                'ts': now,
                'map': {t['symbol']: float(t.get('fundingRate') or 0)
                        for t in r['result'].get('list', [])},
            }
    return _FUNDING_CACHE.get('map', {}).get(sym)


def _count_down_days(sym: str) -> int:
    """Считает количество последовательных дней падения.

    Сначала WS-кеш (прогревается при коннекте), REST — только если кеша нет.
    46 символов × REST-запрос на скан давали ~30с чистой латентности.
    """
    klines = None
    try:
        klines = _ws_kline(sym, 'D')
    except Exception as e:
        log_event(f'⚠️ count_down_days WS ({sym}): {e}')
    if not klines or len(klines) < 2:
        try:
            r = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit=8')
            if r and r.get('retCode') == 0:
                klines = [[0, 0, 0, 0, float(c[4]), 0] for c in reversed(r['result'].get('list', []))]
        except Exception as e:
            log_event(f'⚠️ count_down_days({sym}): {e}')  # kline parse error — return 0
    if not klines or len(klines) < 2:
        return 0

    closes = [k[4] for k in klines][-8:]
    down = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i-1]:
            down += 1
        else:
            break
    return down

def _calc_rsi(sym: str, period: int = 14) -> float:
    """RSI(14) по дневным свечам (телеметрия). 50 при неудаче.

    Свечи берём из WS-кеша — 46 символов × REST давали ~9с на цикл,
    а кеш уже содержит 100 дневных свечей с прогрева.
    """
    try:
        klines = _ws_kline(sym, 'D')
        if klines and len(klines) >= period + 1:
            closes = [k[4] for k in klines][-(period + 1):]
            gains = losses = 0
            for i in range(1, len(closes)):
                diff = closes[i] - closes[i - 1]
                if diff > 0:
                    gains += diff
                else:
                    losses += abs(diff)
            if losses == 0:
                return 100.0
            rs = gains / losses
            return round(100 - (100 / (1 + rs)), 1)
    except Exception as e:
        log_event(f'⚠️ rsi WS ({sym}): {e}')
    try:
        data = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit={period + 5}')
        if not data or data.get('retCode') != 0:
            return 50.0
        candles = data['result'].get('list', [])
        if len(candles) < period + 1:
            return 50.0
        # candles are newest-first from API; reverse to oldest-first
        closes = [float(c[4]) for c in reversed(candles[:period + 1])]
        gains = 0
        losses = 0
        for i in range(1, period + 1):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses += abs(diff)
        if losses == 0:
            return 100.0
        rs = (gains / period) / (losses / period)
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return 50.0



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

    # 4. Funding (0-5) — один bulk-запрос на весь скан вместо 46 поштучных
    fund_score = 3  # default если не получили
    try:
        funding = _funding_rate(sym)
        if funding is not None:
            abs_f = abs(funding)
            if abs_f < 0.00005:        fund_score = 5
            elif abs_f < 0.0001:       fund_score = 4
            elif abs_f < 0.0002:       fund_score = 3
            elif abs_f < 0.0004:       fund_score = 2
            else:                      fund_score = 0
    except Exception as e:
        log_event(f'⚠️ funding_parse({sym}): {e}')  # funding rate parse error — default score 3

    # 5. BB Width / Volatility (0-5)
    if 3 <= bb_width <= 8:    vola_score = 5
    elif 1 <= bb_width < 3 or 8 < bb_width <= 15:   vola_score = 3
    else:                     vola_score = 1

    # 6. Quality score (0-5) — позиция на BB × ширина
    quality = (bb_pos / 100) * bb_width
    if quality <= 0.5:       qscore = 5
    elif quality <= 1.5:     qscore = 4
    elif quality <= 3.0:     qscore = 3
    elif quality <= 5.0:     qscore = 2
    else:                    qscore = 1

    # 7. World Model score (0-5) — предсказание close на t+1 (кешированное)
    world_model_score = 0
    world_predicted_return = None
    if os.environ.get('BYBIT_WORLD_MODEL', '0') == '1':
        try:
            from .lstm_world_model import get_cached_world_prediction as _get_wm
            wm_cache = _get_wm(sym)
            if wm_cache:
                world_predicted_return = wm_cache.get('predicted_return_pct')
                wm_cache.get('regime')
                if world_predicted_return is not None:
                    if world_predicted_return > 0.5:
                        world_model_score = 5
                    elif world_predicted_return >= -0.5:
                        world_model_score = 3
                    else:
                        world_model_score = 1
        except Exception as e:
            log_event(f'⚠️ world_model({sym}): {e}')

    total = bb_score + vol_score + down_score + fund_score + vola_score + qscore + world_model_score

    # ── Фаза 5.1: ML Gate — фильтр вместо бленда ──
    # ── Фаза 5.3: A/B-тестирование — группа B пропускает ML Gate ──
    import time as _time
    signal_id = f"{sym}:{int(_time.time())}:{total}"
    ml_passed = True
    ml_prob = None
    ml_info_extra = ''
    ab_group = None
    gate_details = None
    try:
        from .ab_test import assign_variant as _assign_variant
        # Группа назначается здесь и переиспользуется при записи сделки: раньше
        # звали несуществующий assign_group → ImportError глотался except'ом ниже,
        # и ML-гейт не работал вообще ни на одном сигнале.
        ab_group = _assign_variant(sym, 'Buy', total)

        if ab_group == 'A' and ML_ENABLED:
            # Группа A + ML_ENABLED: ML Gate (RF + опционально DSPy).
            # ML_ENABLED=0 отключает ВЕСЬ ML, включая ML Gate — иначе ab_test
            # fallback 'A' вызывал ml_gate_pass в обход флага и шумел 'joblib не установлен'.
            from .ml_scorer import DSPY_ENABLED as _dspy_en
            from .ml_scorer import combined_gate, ml_gate_pass
            signal_data = {
                'score': total / 5,
                'price': cur,
                'lower_bb': bb_data['lower'],
                'upper_bb': bb_data['upper'],
                'middle_bb': bb_data['middle'],
                'entry': bb_data['lower'] * 0.98,
                'timeframe': 'D',
                'mode': 'long',
                # DSPy-признаки (из сигнала)
                'pnl_pct': 0.0,  # неизвестно до закрытия
                'price_change_pct': 0.0,
                'is_long': 1,
                'side_num': 1,
                'strategy_type': 3,  # long
                'abs_pnl': 0.0,
                'size': 1.0,
            }
            if _dspy_en:
                # DSPy + RF комбинированный гейт
                ml_passed, gate_details = combined_gate(signal_data)
                ml_prob = gate_details.get('rf_prob')
                if gate_details.get('dspy_score') is not None:
                    ml_info_extra = f' DSPy={gate_details["dspy_score"]:.1f}'
                else:
                    ml_info_extra = ''
            else:
                ml_passed, ml_prob = ml_gate_pass(signal_data)
                ml_info_extra = ''
        # else: группа B — пропускаем без ML Gate (ml_passed уже True)
    except Exception as e:
        # Тип исключения в логе обязателен: ImportError тут глотал весь ML-гейт
        # молча — в логе было «cannot import name», но никто не читал.
        log_event(f'⚠️ ml_gate({sym}): {type(e).__name__}: {e}')

    if not ml_passed:
        # Логируем причину блокировки — какой гейт не прошёл
        reason_parts = []
        if gate_details:
            rf_ok = gate_details.get('rf_passed', True)
            dspy_ok = gate_details.get('dspy_passed', True)
            if not rf_ok:
                rf_val = gate_details.get('rf_prob', '?')
                reason_parts.append(f'RF={rf_val}')
            if not dspy_ok:
                dspy_val = gate_details.get('dspy_score', '?')
                reason_parts.append(f'DSPy={dspy_val}')
        else:
            reason_parts.append(f'RF={ml_prob:.2f}' if ml_prob is not None else 'RF=?')
        log_event(f'⛔ ML Gate BLOCKED {sym}: {", ".join(reason_parts)}')
        return None  # ML gate: не входить

    ml_info = f' ML={ml_prob:.2f}' if ml_prob is not None else ''
    ml_info += ml_info_extra
    ab_info = f' AB={ab_group}' if ab_group else ''

    return {
        'symbol': sym,
        'score': total,
        'max_score': 55,
        'bb_pos': bb_pos,
        'bb_width': bb_width,
        'cur': cur,
        'signal_id': signal_id,
        'ab_group': ab_group,
        'ml_prob': ml_prob,
        'world_model_score': world_model_score,
        'world_predicted_return': world_predicted_return,
        'breakdown': f'BB={bb_score} Vol={vol_score} Down={down_score} Fund={fund_score} Vola={vola_score} Q={qscore} WM={world_model_score}{ml_info}{ab_info}',
    }


def _apply_corr_sizing(margin: float, qty: float, corr_sizer: float, price: float, lev: int, sym: str):
    """Корреляционный сайзинг: при r>0.70 corr_sizer=0.5 режет и маржу, и количество.

    Раньше corr_sizer считался и логировался («size ×0.5»), но к размеру не
    применялся — позиция открывалась полной. Возвращает (margin, qty); qty=None —
    если после урезания размер меньше minOrderQty.
    """
    if corr_sizer >= 1.0 or price <= 0:
        return margin, qty
    margin *= corr_sizer
    return margin, _qty_by_step(sym, margin * lev / price)


def _strategy_leverage(side='Buy'):
    """Плечо стороны из конфига стратегии (long/short). Дефолт 3 — как в config.py."""
    strat = Config().strategy.long if side == 'Buy' else Config().strategy.short
    return int(strat.get('leverage', 3))


def _qty_by_step(sym, qty: float):
    """qty, усечённый до qtyStep символа. None — если меньше minOrderQty.

    Раньше qty = ceil(...) давал целые монеты: BTCUSDT в вотчлисте → «1 BTC»
    на $5 маржи (пруф 10.11: 0.0002 → 1.0). Лимиты берём у того же хелпера,
    что auto_tp (fail-closed, без молчаливых дефолтов).
    """
    try:
        from .auto_tp import _get_qty_limits
        step, min_qty = _get_qty_limits(sym)
    except Exception:
        return None
    if not step or not min_qty:
        return None
    # Decimal: str(1e-05) не содержит точки, а float-деление теряет шаг
    # (0.0003 / 0.0001 = 2.9999... → floor дал бы 0.0002).
    step_d, min_d = Decimal(str(step)), Decimal(str(min_qty))
    steps = (Decimal(str(qty)) / step_d).to_integral_value(rounding=ROUND_FLOOR)
    q = steps * step_d
    return float(q) if q >= min_d else None


def apply_entry_leverage(sym, side='Buy'):
    """Единая точка применения плеча перед авто-входом: конфиг → set-leverage.

    Возвращает запрошенное плечо (его же надо сверять после входа).
    """
    lev = _strategy_leverage(side)
    set_leverage(sym, lev)
    return lev


def _resolve_long_idx(pos_data, sym: str) -> int:
    """positionIdx для LONG-входа по ответу position/list (уже запрошенному).

    hedge-режим определяется по НАЛИЧИЮ записи positionIdx 1/2 в ответе — они есть
    даже при size=0. Символ без позиции в hedge-режиме иначе получает idx=0 и
    Bybit отвечает 10001 «position idx not match position mode» (LINK/ARB/ENA/…).
    """
    if not (pos_data and pos_data.get('retCode') == 0):
        return position_idx(sym, 'Buy')
    plist = pos_data['result'].get('list', [])
    if {int(p.get('positionIdx', 0)) for p in plist} & {1, 2}:
        learn_position_idx(sym, 1)
        return 1  # LONG (Buy) в hedge-режиме = positionIdx 1
    for p in plist:
        if float(p.get('size', 0)) > 0:
            return int(p.get('positionIdx', 0))
    return position_idx(sym, 'Buy')


def auto_entry_scan(positions):
    """Скрининг и авто-вход по 9-метричному скорингу (v4.0 + LSTM-режим 5.4)."""
    entries = []

    # ── Фаза 5.4: проверка режимного флага LONG ──
    from . import REGIME_LONG_ENABLED
    if not REGIME_LONG_ENABLED:
        log_event('🚫 REGIME_AUTO: LONG отключён по режиму рынка')
        return entries

    active = set(positions.keys())

    # ── Фаза 5.4: адаптивные параметры от режима ──
    regime_params, regime_name, regime_conf = _get_regime_params()
    min_score = regime_params['min_score']
    entry_discount_mult = regime_params['entry_discount']
    aggression = regime_params['aggression']

    # ── Фаза 5.2: Optuna per-symbol min_score (приоритет над режимом) ──
    _optuna_min_scores = {}
    if os.environ.get('BYBIT_OPTUNA_ENABLED', '0') == '1':
        try:
            from .optuna_tuner import load_optuna_params
            optuna_p = load_optuna_params()
            for sym, params in optuna_p.items():
                if 'min_score' in params:
                    _optuna_min_scores[sym] = params['min_score']
        except Exception:
            pass
    all_watch = scan_universe()

    try:
        r = safe_run([BYBIT_CLI, 'tickers'], timeout=15)
        if r.returncode != 0:
            return entries
        ticker_lines = r.stdout.split('\n')
    except Exception as e:
        log_event(f'⚠️ auto_entry tickers: {e}')
        return entries

    candidates = []
    for sym in all_watch:
        if sym in active:
            continue
        for line in ticker_lines:
            if sym in line:
                candidates.append((sym, line))
                break

    _t0 = time.time()
    scored = []
    for sym, ticker_line in candidates:
        bb = _get_bb_ws(sym, 'D')
        if not bb:
            continue
        result = full_score_coin(sym, bb, ticker_line)
        # v4: Per-symbol min_score: canary → symbol_profile → session → optuna → global
        sym_min_score = _optuna_min_scores.get(sym, min_score)
        try:
            from bybit_ws.journal.self_learn import (
                get_canary_param,
                get_session_modifier,
                get_symbol_params,
                pick_bandit_params,
            )
            sym_min_score = get_canary_param('min_score', sym_min_score, symbol=sym, side='buy')
            # Bandit: Thompson-сэмпл руки на этот скан. Выбранную руку запоминаем —
            # при входе она уйдёт в record_arm_assignment, иначе кредит уедет не туда.
            _bp, _arm_idx = pick_bandit_params(regime_name)
            if _bp and 'min_score' in _bp:
                sym_min_score = _bp['min_score']
                _SCAN_ARMS[(sym, 'buy')] = (regime_name, _arm_idx)
            # Per-symbol profile override (специфичнее bandit)
            sym_min_score = get_symbol_params(sym, 'min_score', sym_min_score)
            # Session modifier
            sym_min_score = int(sym_min_score * get_session_modifier('min_score'))
        except Exception:
            pass
        if result and result['score'] >= sym_min_score:
            scored.append(result)

    # ── Фаза 4.3.1: Multi-TF конфлюенс-фильтр ──
    _t1 = time.time()
    scored = _filter_by_mtf_confluence(scored, 'LONG')
    log_event(f'⏱ scan timings: score={_t1 - _t0:.1f}s confluence={time.time() - _t1:.1f}s'
              f' ({len(candidates)} симв)')

    scored.sort(key=lambda x: x['score'], reverse=True)

    for s in scored:
        try:
            sym = s['symbol']

            # ── v4: Streak check — блокируем вход при серии лосей ──
            try:
                from bybit_ws.journal.self_learn import get_streak_status
                streak = get_streak_status()
                if streak.get("blocked"):
                    log_event(f'🛑 STREAK BLOCK: {streak["consecutive_losses"]} losses — входы заблокированы на {streak["remaining_hours"]:.1f}ч')
                    continue
            except Exception:
                pass

            cooldown = _load_cooldown()
            cfg = Config()
            cooldown_sl = cfg.strategy.long.get('cooldown_after_sl', 14400)
            if sym in cooldown:
                elapsed = time.time() - cooldown[sym]
                if elapsed < cooldown_sl:
                    continue

            # Перманентный blacklist: символы, в которые НЕ входить вообще
            from .symbol_blacklist import is_blacklisted as _is_blacklisted
            if _is_blacklisted(sym):
                log_event(f'🚫 auto_entry: {sym} в blacklist — пропускаю')
                continue

            # Уже висит лимитка на вход по этому символу — вторую не ставим.
            # Прецедент 12.11: XRPUSDT получил два ордера (0.9069 и 0.9078) в
            # разных циклах, позиции-то ещё нет, и «позиция уже есть» не сработало —
            # лимитки удваивали экспозицию по символу.
            if sym in _open_entry_symbols():
                log_event(f'⏭️ auto_entry: {sym} — уже висит лимитка на вход, пропускаю')
                continue

            # Маржа от score с учётом агрессии режима (Фаза 5.4)
            # Volatility filter: block high-vol symbols, scale margin (fail-open)
            from .volatility_filter import is_high_volatility, volatility_scale
            blocked, _ratio, reason = is_high_volatility(sym)
            if blocked:
                log_event(f'🚫 VOL-FILTER {sym}: {reason}')
                continue
            normalized_score = min(10, s['score'] / 5)  # 25→5, 50→10
            margin = margin_for_strategy('long', score=normalized_score, symbol=sym) * aggression * volatility_scale(sym)
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
            # Compute RSI for entry diagnostics (if not already in scored result)
            if s.get('rsi') is None:
                s['rsi'] = _calc_rsi(sym)
            # Per-symbol оптимальный дисконт × режимный множитель (Фаза 5.2 + 5.4)
            sym_discount = _get_symbol_param(sym, 'entry_discount', 1.0)
            price = round(bb2['lower'] * sym_discount * entry_discount_mult, 4)
            # плечо берём из того же источника, что apply_entry_leverage ниже:
            # литерал 3 расходился с конфигом при смене leverage
            notional = margin * _strategy_leverage('Buy')
            qty = _qty_by_step(sym, notional / price)
            if qty is None:
                log_event(f'⚠️ {sym}: ${notional:.2f} меньше minOrderQty — вход пропущен')
                continue
            # ── v4: Streak size multiplier — половинный размер при 3+ лосях ──
            skipped_by_streak = False
            try:
                from bybit_ws.journal.self_learn import get_streak_status
                streak = get_streak_status()
                mult = streak.get("size_multiplier", 1.0)
                if mult < 1.0:
                    q2 = _qty_by_step(sym, notional * mult / price)
                    if q2 is None:
                        log_event(f'⚠️ STREAK SIZE {sym}: урезанный размер < minOrderQty — вход пропущен')
                        skipped_by_streak = True
                    else:
                        qty = q2
                        log_event(f'⚠️ STREAK SIZE: {streak["consecutive_losses"]} losses → size ×{mult} → {qty}')
            except Exception:
                pass
            if skipped_by_streak:
                continue

            if sym in positions:
                existing_val = positions[sym]['mark'] * positions[sym]['size']
                if existing_val + price * qty > MAX_POSITION_VALUE:
                    continue

            pos_data = bybit('GET', f'/v5/position/list?category=linear&settleCoin=USDT&symbol={sym}')
            idx = _resolve_long_idx(pos_data, sym)

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
                except Exception as e:
                    log_event(f'⚠️ Ensemble error for {sym} — fallback to heuristic: {e}')

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

            # ── Проверка свободных средств перед отправкой ордера ──
            try:
                w = account_wallet()
                available_usdt = w['available'] if w else 0.0
                required = margin * 1.05  # +5% запас на комиссию
                if available_usdt < required:
                    _cd_h = Config().strategy.long.get('cooldown_after_sl', 14400) / 3600
                    log_event(f'💰 LOW FUNDS {sym}: need ${required:.1f}, have ${available_usdt:.1f} — кулдаун {_cd_h:.0f}ч')
                    record_sl_hit(sym)  # кулдаун через cooldown.json
                    continue
            except Exception as e:
                log_event(f'⚠️ balance check failed for {sym}: {e}')  # API может отвалиться — лучше войти без проверки чем пропустить сигнал

            # ── Orderbook imbalance filter (27.06) ──
            try:
                from .orderbook_filter import (
                    get_orderbook_imbalance,
                    should_enter_by_imbalance,
                )
                ob_ok, ob_reason = should_enter_by_imbalance(sym, 'Buy', regime_name)
                if not ob_ok:
                    log_event(f'📊 OB BLOCK {sym}: {ob_reason}')
                    # Контрфакт: фильтр требует перевеса покупателей, а на падающем
                    # рынке стакан 0.48-0.50 режет ВСЕ лонги. Без этих записей не
                    # ответить, спасает фильтр или душит — данные копятся в
                    # counterfactual_entries, порог не трогаем до цифр.
                    try:
                        from .state_db import db as _sdb
                        _sdb.log_counterfactual(sym, 'Buy', s['score'], s['bb_pos'],
                                                s['cur'], get_orderbook_imbalance(sym),
                                                f'ob:{ob_reason}')
                    except Exception:
                        pass
                    continue
            except Exception:
                pass  # фильтр недоступен → не блокируем

            # ── Volume confirmation filter (28.06) ──
            try:
                from .volume_filter import volume_ok
                vol_ok, vol_reason = volume_ok(sym)
                if not vol_ok:
                    log_event(f'📊 VOL BLOCK {sym}: {vol_reason}')
                    continue
            except Exception:
                pass  # фильтр недоступен → не блокируем

            # ── Фаза 6.8: Cross-model entry judge (Nemotron) ──
            try:
                from .entry_judge import should_enter as judge_should_enter
                # Ориентир SL для судьи: auto_sl ставит LONG-SL от mark*0.95, а не
                # от BB lower. Лимитка стоит на 10-12% ниже BB lower (дисконт входа),
                # поэтому прежний _est_sl = lower*0.95 оказывался ВЫШЕ цены входа —
                # судья справедливо блокировал 100% лонгов «SL не с той стороны».
                _est_sl = round(price * 0.95, 8)
                can_enter, judge_reason = judge_should_enter(
                    symbol=sym, side='Buy', score=s['score'],
                    bb_pos=s['bb_pos'], entry_price=price,
                    sl_price=_est_sl,
                    funding_rate=s.get('funding', 0.0),
                    bb_lower=bb2.get('lower', 0), bb_upper=bb2.get('upper', 0),
                    mtf_confluence=s.get('mtf', {}).get('confluence', 0) if 'mtf' in s else 0,
                    regime=regime_name,
                )
                if not can_enter:
                    log_event(f'🧑‍⚖️ ENTRY JUDGE BLOCK {sym}: {judge_reason}')
                    continue
            except Exception as e:
                log_event(f'⚠️ entry_judge {sym}: {e}')
                continue  # fail-closed: при ошибке судьи — не входим

            # ── Correlation-adjusted sizing (27.06) ──
            corr_sizer = 1.0
            if positions:
                try:
                    from .correlation import max_corr_with_open
                    max_c, corr_sym = max_corr_with_open(sym, positions)
                    if max_c > 0.85:
                        log_event(f'🔗 CORR BLOCK {sym}: r={max_c:+.2f} с {corr_sym} > 0.85 (концентрация)')
                        continue
                    elif max_c > 0.70:
                        corr_sizer = 0.5
                        log_event(f'🔗 CORR {sym}: r={max_c:+.2f} с {corr_sym} → size ×0.5')
                    elif max_c < -0.70:
                        log_event(f'🔗 HEDGE {sym}: r={max_c:+.2f} с {corr_sym} (отрицательная — диверсификация)')
                except Exception as e:
                    # fail-closed: без корреляционной проверки не входим
                    log_event(f'⚠️ CORR CHECK FAILED {sym}: {e} — вход заблокирован')
                    continue
            # применяем решение корреляционного сайзинга (см. _apply_corr_sizing)
            margin, qty = _apply_corr_sizing(margin, qty, corr_sizer, price, _strategy_leverage('Buy'), sym)
            if qty is None:
                log_event(f'⚠️ {sym}: после corr-сайзинга размер < minOrderQty — вход пропущен')
                continue
            qty = float(qty)

            # ── Symbol concentration check (28.06.2026) ──
            # Не более MAX_RISK_PER_SYMBOL_PCT от депозита на один тикер
            try:
                from .risk_manager import check_symbol_concentration
                conc_ok, conc_reason, _conc_pct = check_symbol_concentration(sym, margin, positions)
                if not conc_ok:
                    log_event(f'⚠️ CONC BLOCK {sym}: {conc_reason}')
                    continue
            except Exception as e:
                log_event(f'⚠️ concentration check failed for {sym}: {e} — вход заблокирован')
                continue

            # ── Anti-chasing: блок LONG, если монета уже разогналась >CHASE_PCT% ──
            try:
                from bybit_ws.chase_guard import is_chasing
                _k = bybit('GET', f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit={CHASE_LOOKBACK + 3}')
                if _k and _k.get('retCode') == 0:
                    _closes = [float(c[4]) for c in reversed(_k['result'].get('list', []))]
                    if is_chasing(_closes, CHASE_PCT, CHASE_LOOKBACK):
                        log_event(f'⛔ CHASE BLOCK {sym}: рост >{CHASE_PCT}% за {CHASE_LOOKBACK} свечей')
                        continue
            except Exception as e:
                log_event(f'⚠️ chase guard failed for {sym}: {e} — вход заблокирован')
                continue

            # ── Anti-overtrading: дневной лимит LONG-входов ──
            try:
                from bybit_ws.trade_limits import allow_by_daily_count
                _today = _count_today_entries()
                _ok, _reason = allow_by_daily_count(_today, MAX_DAILY_TRADES)
                if not _ok:
                    log_event(f'⛔ DAILY LIMIT {sym}: {_reason} ({_today}/{MAX_DAILY_TRADES})')
                    continue
            except Exception as e:
                log_event(f'⚠️ daily-limit check failed for {sym}: {e} — вход заблокирован')
                continue

            # ── Плечо стратегии на биржу ДО ордера (этап 2 аудита) ──
            lev = apply_entry_leverage(sym, 'Buy')

            body = {'category': 'linear', 'symbol': sym, 'side': 'Buy',
                    'orderType': 'Limit', 'qty': str(qty), 'price': str(price),
                    'positionIdx': idx, 'timeInForce': 'PostOnly',
                    'orderLinkId': f'ae_{sym.lower()}_{int(time.time())}'}
            # PostOnly: вход только maker-ом (0.02% вместо 0.055%). Если ордер
            # пересекает спред, биржа его отклоняет — вход теряется, а не
            # оплачивается taker-комиссией. Замер: fee_stats.py
            result = bybit('POST', '/v5/order/create', body)
            if result and result.get('retCode') != 0:
                log_event(f'⛔ POST-ONLY LONG {sym}: {result.get("retMsg", "?")} — вход не открыт')
            if result and result.get('retCode') == 0:
                # режим символа запоминаем по факту принятого ордера
                learn_position_idx(sym, idx)
                # ── Сверка фактического плеча (mismatch → алерт) ──
                verify_leverage(sym, lev)
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
                    f'🚀 LONG {sym}: вход ${price:.4f} ×{qty} ({lev}x) | '
                    f'score={s["score"]}/{s["max_score"]} BB={s["bb_pos"]:.0f}%{mtf_info}{regime_info}{rl_info}'
                )
                log_event(f'Авто-вход {sym} @ ${price:.4f} score={s["score"]} BB={s["bb_pos"]:.0f}%')
                # ── v8.1: запись контекста входа в trade_history ──
                try:
                    from bybit_ws.state_db import db as sync_db
                    entry_reason = f'score={s.get("score")}/{s.get("max_score")} BB={s.get("bb_pos"):.0f}%'
                    if 'mtf' in s:
                        entry_reason += f' MTF={s["mtf"]["confluence"]}/3'
                    if regime_name != 'NEUTRAL':
                        entry_reason += f' regime={regime_name}'
                    if ensemble_decision:
                        entry_reason += f' RL={ensemble_decision}'
                    sync_db.record_entry(
                        symbol=sym, side='Buy', strategy='bollinger_grid',
                        entry_price=price, size=qty,
                        bb_pct=s.get('bb_pos'), rsi=s.get('rsi'),
                        entry_reason=entry_reason,
                    )
                    # Persist diagnostics for retrieval when trade closes
                    sync_db.save_entry_diagnostic(
                        sym, 'Buy',
                        bb_pct=s.get('bb_pos'),
                        rsi=s.get('rsi'),
                        entry_reason=entry_reason,
                        score=s.get('score'),
                        min_score=s.get('max_score'),
                        regime=regime_name,
                        funding=s.get('funding'),
                        confluence=(s.get('mtf') or {}).get('confluence'),
                        leverage=lev,
                        margin=margin,
                    )
                except Exception as e:
                    log_event(f'⚠️ entry telemetry failed for {sym}: {e}')
                # Инкремент дневного счётчика входов (anti-overtrading)
                _count_today_entries()
                _daily_entries['count'] += 1
                # ── Canary: маркируем вход для последующего матчинга при закрытии ──
                try:
                    from bybit_ws.journal.self_learn import (
                        mark_canary_entry,
                        should_use_canary,
                    )
                    if should_use_canary():
                        mark_canary_entry(sym, 'buy', time.time())
                except Exception:
                    pass
                # ── Bandit: фиксируем руку, которая решала по этому входу ──
                try:
                    from bybit_ws.journal.self_learn import record_arm_assignment
                    _arm = _SCAN_ARMS.pop((sym, 'buy'), None)
                    if _arm:
                        record_arm_assignment(sym, 'buy', time.time(), _arm[0], _arm[1])
                except Exception:
                    pass
                # -- Фаза 5.3: A/B-тест стратегий (успешный вход) --
                try:
                    from .ab_test import (
                        _generate_signal_id,
                        assign_variant,
                        is_ab_enabled,
                        record_paper_entry,
                    )
                    if is_ab_enabled():
                        # id и группа — из full_score_coin: assign_variant() кидал
                        # случайный вариант, не тот, что применил ML-гейт
                        signal_id = s.get('signal_id') or _generate_signal_id(sym, 'Buy')
                        variant = s.get('ab_group') or assign_variant(sym, 'Buy', s['score'])
                        record_paper_entry(signal_id, variant, sym, 'Buy', 'long',
                                          price, qty, s['score'])
                        log_event(f'🧪 A/B: {sym} → вариант {variant} (score={s["score"]})')
                except Exception as e:
                    log_event(f'⚠️ ab_test {sym}: {e}')
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
                # -- Фаза 5.3: A/B-тест стратегий --
                try:
                    from .ab_test import (
                        _generate_signal_id,
                        assign_variant,
                        is_ab_enabled,
                        record_paper_entry,
                    )
                    if is_ab_enabled():
                        # id и группа — из full_score_coin: assign_variant() кидал
                        # случайный вариант, не тот, что применил ML-гейт
                        signal_id = s.get('signal_id') or _generate_signal_id(sym, 'Buy')
                        variant = s.get('ab_group') or assign_variant(sym, 'Buy', s['score'])
                        record_paper_entry(signal_id, variant, sym, 'Buy', 'long',
                                          price, qty, s['score'])
                        log_event(f'🧪 A/B: {sym} → вариант {variant} (score={s["score"]})')
                except Exception as e:
                    log_event(f'⚠️ ab_test {sym}: {e}')
        except Exception as e:
            log_event(f'⚠️ auto_entry {sym}: {e}')
            continue

    return entries
