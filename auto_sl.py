"""
Авто-SL v2 — ATR-adaptive (27.06.2026).

SL = entry ± k × ATR(14), где k зависит от волатильности:
  - trending:  k = 2.0 (даём дышать)
  - normal:    k = 1.5 (баланс)
  - choppy:    k = 1.2 (плотный SL)
  - high_vol:  k = 2.5 (широкий, чтобы не выбило шумом)

Fallback на BB-based если ATR недоступен.
"""
import os, json, time
from .api import bybit, fetch_positions, get_bb_data
from .config import Config
from .alerts import log_event
from .manual_positions import is_manual_position

_WS_BB_ENABLED = os.environ.get('BYBIT_WS_BB_ENABLED', '1') == '1'

# ATR SL multipliers by regime
ATR_SL_MULTIPLIERS = {
    'trending': 2.0,
    'normal': 1.5,
    'choppy': 1.2,
    'high_vol': 2.5,
    'low_vol': 1.3,
}
ATR_SL_DEFAULT = 1.5

# ATR cache TTL (секунд)
_ATR_CACHE = {}
_ATR_CACHE_TTL = 60


def _get_atr(symbol: str, period: int = 14, interval: str = '60') -> float | None:
    """Получить ATR(14) для символа с кешированием."""
    now = time.time()
    cache_key = f"{symbol}:{interval}"
    if cache_key in _ATR_CACHE and now - _ATR_CACHE[cache_key]['ts'] < _ATR_CACHE_TTL:
        return _ATR_CACHE[cache_key]['value']

    try:
        kline = bybit('GET',
            f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={period + 1}')
        if not kline or kline.get('retCode') != 0:
            return None

        candles = kline['result']['list']
        if len(candles) < period:
            return None

        ranges = []
        for c in candles[:period]:
            high = float(c[2])
            low = float(c[3])
            ranges.append(high - low)

        atr = sum(ranges) / len(ranges)
        _ATR_CACHE[cache_key] = {'ts': now, 'value': atr}
        return atr
    except Exception:
        return None


def _get_sl_multiplier(symbol: str) -> float:
    """Определить множитель SL по ATR + LSTM-режим."""
    atr = _get_atr(symbol)
    if atr is None:
        return ATR_SL_DEFAULT

    base_mult = ATR_SL_DEFAULT
    try:
        ticker = bybit('GET', f'/v5/market/tickers?category=linear&symbol={symbol}')
        if ticker and ticker.get('retCode') == 0:
            price = float(ticker['result']['list'][0]['lastPrice'])
            atr_pct = atr / price if price > 0 else 0.03

            if atr_pct > 0.05:
                base_mult = ATR_SL_MULTIPLIERS['high_vol']
            elif atr_pct > 0.03:
                base_mult = ATR_SL_MULTIPLIERS['trending']
            elif atr_pct < 0.01:
                base_mult = ATR_SL_MULTIPLIERS['low_vol']
    except Exception:
        pass

    # ── v9: LSTM-regime adjustment ──
    try:
        from .lstm_regime import predict_regime
        regime_data = predict_regime()
        regime = regime_data.get('regime', 'NEUTRAL')
        regime_adj = {
            'RANGING': 0.7,        # SL ближе — быстрее режем в боковике
            'CHOPPY': 0.7,
            'TRENDING_UP': 1.2,    # SL дальше — даём тренду пространство
            'TRENDING_DOWN': 1.2,
            'HIGH_VOL': 1.0,       # без изменений
            'LOW_VOL': 0.85,        # чуть ближе
            'NEUTRAL': 1.0,
        }.get(regime, 1.0)
        base_mult *= regime_adj
    except Exception:
        pass

    return base_mult


def _get_bb_ws(symbol, interval='D'):
    """BB: сначала WS-кеш, fallback на REST."""
    if _WS_BB_ENABLED:
        try:
            from .ws_client import get_bb as ws_get_bb, is_connected as ws_alive, is_stale as ws_stale
            if ws_alive() and not ws_stale(300):
                bb = ws_get_bb(symbol, interval)
                if bb and bb.get('lower', 0) > 0:
                    return bb
        except Exception:
            pass
    return get_bb_data(symbol, interval)


def _get_tiers(cfg):
    tier_ab = set()
    one_way = set()
    try:
        tier_ab = set(cfg.tiers.A) | set(cfg.tiers.B)
    except Exception:
        pass
    try:
        one_way = set(cfg.tiers.one_way)
    except Exception:
        pass
    return tier_ab, one_way


def check_and_fix_sl(positions=None):
    """Проверить все позиции, поставить ATR-adaptive SL тем у кого нет."""
    alerts = []
    if positions is None:
        positions = fetch_positions()
    if not positions:
        return alerts

    cfg = Config()
    tier_ab, one_way = _get_tiers(cfg)

    for sym, p in positions.items():
        if is_manual_position(sym):
            continue

        sl = p.get('stopLoss')
        if sl is not None and sl != '' and sl != '0' and float(sl or 0) > 0:
            sl_val = float(sl)
            entry = p['entry']
            side = p['side']
            mark = p['mark']  # для проверки стороны SL относительно рынка
            # SL на неправильной стороне от рынка → исправляем
            if side == 'Buy' and sl_val > mark:
                # LONG: SL выше рынка → немедленно триггерится → фикс
                sl_price = round(mark * 0.95, 4)
                body = {'category': 'linear', 'symbol': sym, 'positionIdx': p.get('positionIdx', 0),
                        'stopLoss': str(sl_price), 'slTriggerBy': 'MarkPrice'}
                data = bybit('POST', '/v5/position/trading-stop', body)
                if data and data.get('retCode') == 0:
                    log_event(f'🔧 {sym}: LONG SL исправлен ${sl_val:.4f}→${sl_price:.4f} (был выше рынка ${mark:.4f})')
                else:
                    err_msg = data.get('retMsg', '?') if data else 'no response'
                    log_event(f'⚠️ {sym}: LONG SL fix failed: {err_msg}')
                continue
            if side == 'Sell' and sl_val < mark:
                # SHORT: SL ниже рынка → немедленно триггерится → фикс
                sl_price = round(mark * 1.05, 4)
                body = {'category': 'linear', 'symbol': sym, 'positionIdx': p.get('positionIdx', 0),
                        'stopLoss': str(sl_price), 'slTriggerBy': 'MarkPrice'}
                data = bybit('POST', '/v5/position/trading-stop', body)
                if data and data.get('retCode') == 0:
                    log_event(f'🔧 {sym}: SHORT SL исправлен ${sl_val:.4f}→${sl_price:.4f} (был ниже рынка ${mark:.4f})')
                else:
                    err_msg = data.get('retMsg', '?') if data else 'no response'
                    log_event(f'⚠️ {sym}: SHORT SL fix failed: {err_msg}')
                continue
            # Проверка: не слишком ли близко SL
            # LONG: <2% от входа → переставим дальше
            # SHORT: <5% от входа → переставим (иначе выбивает шумом)
            sl_dist_pct = abs(sl_val - entry) / entry
            min_dist = 0.05 if side == 'Sell' else 0.02
            if sl_dist_pct < min_dist:
                pass  # продолжаем ниже (не делаем continue)
            else:
                continue  # SL на нормальном расстоянии — не трогаем

        mark = p['mark']
        side = p['side']
        idx = p['positionIdx']
        size = p['size']
        entry = p['entry']

        # BE-SL: в плюсе >3% → безубыток с зазором +1%
        # (не ставим раньше — ATR-adaptive SL даёт позиции дышать)
        if side == 'Buy' and mark > entry * 1.03:
            sl_price = round(entry * 1.01, 4)  # +1% выше входа = безубыток с зазором
            if sl_price < mark:
                body = {'category': 'linear', 'symbol': sym, 'positionIdx': idx,
                        'stopLoss': str(sl_price), 'slTriggerBy': 'MarkPrice'}
                data = bybit('POST', '/v5/position/trading-stop', body)
                if data and data.get('retCode') == 0:
                    alerts.append(f'🛡 BE-SL {sym}: ${sl_price:.4f} (безубыток, +3% от входа)')
                elif data and data.get('retMsg') == 'not modified':
                    pass  # SL already at target — not an error
                else:
                    err = data.get('retMsg', '?') if data else 'no response'
                    alerts.append(f'⚠️ BE-SL {sym} НЕ встал: {err}')
            continue
        if side == 'Sell' and mark < entry * 0.97:
            sl_price = round(entry * 0.99, 4)  # -1% ниже входа = безубыток с зазором
            if sl_price > mark:
                body = {'category': 'linear', 'symbol': sym, 'positionIdx': idx,
                        'stopLoss': str(sl_price), 'slTriggerBy': 'MarkPrice'}
                data = bybit('POST', '/v5/position/trading-stop', body)
                if data and data.get('retCode') == 0:
                    alerts.append(f'🛡 BE-SL {sym}: ${sl_price:.4f} (безубыток, -3% от входа)')
                elif data and data.get('retMsg') == 'not modified':
                    pass  # SL already at target
                else:
                    err = data.get('retMsg', '?') if data else 'no response'
                    alerts.append(f'⚠️ BE-SL {sym} НЕ встал: {err}')
            continue

        # ── ATR-adaptive SL ──
        atr = _get_atr(sym)
        if atr and atr > 0:
            multiplier = _get_sl_multiplier(sym)
            if side == 'Buy':
                sl_price = round(entry - multiplier * atr, 4)
                sl_desc = f'ATR(14)={atr:.4f} ×{multiplier}'
            else:
                sl_price = round(entry + multiplier * atr, 4)
                sl_desc = f'ATR(14)={atr:.4f} ×{multiplier}'

            # ── SL cap: не шире -50% / +50% от входа ──
            if side == 'Buy':
                min_sl = round(entry * 0.5, 4)
                if sl_price < min_sl:
                    sl_price = min_sl
                    sl_desc += f' (capped -50%)'
            else:
                max_sl = round(entry * 1.5, 4)
                if sl_price > max_sl:
                    sl_price = max_sl
                    sl_desc += f' (capped +50%)'

            # ── SL floor: не ближе 5% от входа (v9.1) ──
            if side == 'Buy':
                min_sl_5pct = round(entry * 0.95, 4)
                if sl_price > min_sl_5pct:
                    sl_price = min_sl_5pct
                    sl_desc += ' (min -5%)'
            else:
                max_sl_5pct = round(entry * 1.05, 4)
                if sl_price < max_sl_5pct:
                    sl_price = max_sl_5pct
                    sl_desc += ' (min +5%)'

            # Проверка что SL на правильной стороне
            if side == 'Buy' and sl_price >= mark:
                # ATR/BB SL выше рынка → позиция в минусе → аварийный SL
                sl_price = round(mark * 0.95, 4)
                sl_desc = f'аварийный SL (mark×0.95, ATR SL был бы выше рынка)'
            elif side == 'Sell' and sl_price <= mark:
                # ATR/BB SL ниже рынка для SHORT → позиция в минусе → аварийный SL
                sl_price = round(mark * 1.05, 4)
                sl_desc = f'аварийный SL (mark×1.05, ATR SL был бы ниже рынка)'
        else:
            # ── Fallback: BB-based SL ──
            if side == 'Buy':
                bb = _get_bb_ws(sym, 'D')
                if bb and bb['lower'] > 0:
                    sl_price = bb['lower'] * 0.93
                    sl_desc = f'BB Lower×0.93 (${bb["lower"]:.4f})'
                else:
                    sl_price = mark * 0.93
                    sl_desc = 'Mark×0.93 (нет BB)'
            else:
                is_junk = sym not in tier_ab and sym not in one_way
                if is_junk:
                    sl_price = entry * 1.07
                    sl_desc = '+7% от входа (Tier C/D)'
                else:
                    sl_price = entry * 1.10
                    sl_desc = '+10% от входа (Tier A/B)'

            sl_price = round(sl_price, 4)
            if side == 'Buy' and sl_price >= mark:
                continue
            if side == 'Sell' and sl_price <= mark:
                continue

        # Bybit v5 trading-stop
        body = {
            'category': 'linear',
            'symbol': sym,
            'positionIdx': idx,
            'stopLoss': str(sl_price),
            'slTriggerBy': 'MarkPrice',
        }

        data = bybit('POST', '/v5/position/trading-stop', body)
        if data and data.get('retCode') == 0:
            msg = f'🛡 SL {sym}: ${sl_price:.4f} ({sl_desc}, вход ${entry:.4f})'
            alerts.append(msg)
        elif data and data.get('retMsg') == 'not modified':
            pass  # SL already at target — not an error
        else:
            err = data.get('retMsg', '?') if data else 'no response'
            msg = f'⚠️ SL {sym} НЕ встал: {err}'
            alerts.append(msg)

    return alerts


def check_breakeven_sl(positions=None):
    """Безубыток: при движении +10% → SL = entry + 1%."""
    alerts = []
    if positions is None:
        positions = fetch_positions()
    if not positions:
        return alerts

    try:
        state_file = os.path.join(os.path.expanduser('~/.local/share/bybit-ws'), 'pumps.json')
        with open(state_file) as f:
            pump_state = json.loads(f.read())
    except Exception:
        pump_state = {}

    for sym, p in positions.items():
        if is_manual_position(sym):
            continue
        if sym in pump_state:
            continue

        entry = p['entry']
        mark = p['mark']
        side = p['side']
        idx = p['positionIdx']
        sl = p.get('stopLoss')
        sl_val = float(sl) if sl and sl != '' and sl != '0' else None

        if side == 'Buy':
            if mark < entry * 1.10:
                continue
            if sl_val and sl_val > entry:
                continue
            sl_price = round(entry * 1.01, 4)
            sl_desc = f'безубыток +1% (рост +{(mark/entry-1)*100:.0f}%)'
        else:
            if mark > entry * 0.90:
                continue
            if sl_val and sl_val < entry:
                continue
            sl_price = round(entry * 0.99, 4)
            sl_desc = f'безубыток −1% (падение −{(1-mark/entry)*100:.0f}%)'

        if side == 'Buy' and sl_price >= mark:
            continue
        if side == 'Sell' and sl_price <= mark:
            continue

        body = {
            'category': 'linear',
            'symbol': sym,
            'positionIdx': idx,
            'stopLoss': str(sl_price),
            'slTriggerBy': 'MarkPrice',
        }

        data = bybit('POST', '/v5/position/trading-stop', body)
        if data and data.get('retCode') == 0:
            alerts.append(f'🛡 Б/у-SL {sym}: ${sl_price:.4f} ({sl_desc})')
        elif data and data.get('retMsg') == 'not modified':
            pass  # SL already at target
        else:
            err = data.get('retMsg', '?') if data else 'no response'
            log_event(f'⚠️ Б/у-SL {sym} НЕ встал: {err}')

    return alerts
