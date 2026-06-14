"""Авто-DCA (лесенка): докупка при падении цены ниже входа.

Уровни, маржа и лимиты — из конфига strategy.dca (фикс код-ревью Manus AI).
"""

import json, math, os, time
from .api import bybit, fetch_positions, get_bb_data
from .alerts import log_event
from .config import Config

DCA_STATE_FILE = os.path.expanduser('~/.local/share/bybit-ws/dca_state.json')


def _get_dca_config(cfg):
    """Получить параметры DCA из конфига с дефолтами."""
    dca = getattr(cfg.strategy, 'dca', cfg.strategy)  # фоллбек на корень strategy
    return {
        'levels': [
            (float(l.get('drawdown', -0.10)), float(l.get('margin_mult', 1.5)))
            if isinstance(l, dict) else (float(l), 1.5)
            for l in dca.get('levels', [
                {'drawdown': -0.10, 'margin_mult': 1.5},
                {'drawdown': -0.20, 'margin_mult': 2.0},
                {'drawdown': -0.30, 'margin_mult': 2.5},
            ])
        ] if hasattr(dca, 'get') else [
            (-0.10, 1.5), (-0.20, 2.0), (-0.30, 2.5)
        ],
        'base_margin': float(dca.get('base_margin', 10)) if hasattr(dca, 'get') else 10,
        'max_pos_value': float(dca.get('max_pos_value', 40)) if hasattr(dca, 'get') else 40,
        'max_margin_per_symbol': float(dca.get('max_margin_per_symbol', 80)) if hasattr(dca, 'get') else 80,
        'max_dca_count': int(dca.get('max_dca_count', 2)) if hasattr(dca, 'get') else 2,
        'quality_set': set(dca.get('quality_set', [])
            ) if hasattr(dca, 'get') else set(),
    }


def _load_state():
    if os.path.exists(DCA_STATE_FILE):
        try:
            with open(DCA_STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log_event(f'⚠️ dca: {e}')
    return {}


def _save_state(state):
    with open(DCA_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def _get_lot_step(sym):
    """Получить шаг лота."""
    data = bybit('GET', f'/v5/market/instruments-info?category=linear&symbol={sym}')
    if not data or data.get('retCode') != 0:
        return 0.1
    try:
        ls = data['result']['list'][0]['lotSizeFilter']
        return float(ls['qtyStep'])
    except:
        return 0.1


def check_dca():
    """Проверить позиции на DCA-уровни и поставить лимитки при необходимости."""
    alerts = []
    positions = fetch_positions()
    if not positions:
        return alerts

    state = _load_state()
    now = time.time()
    cfg = Config()
    dca_cfg = _get_dca_config(cfg)

    for sym, p in positions.items():
        side = p['side']
        if side != 'Buy':
            continue

        entry = p['entry']
        mark = p['mark']
        size = p['size']
        lev = p.get('leverage', 3)

        pos_value = mark * size
        if pos_value > dca_cfg['max_pos_value']:
            continue

        bb = get_bb_data(sym, 'D')
        if not bb or bb['bb_pos'] > 25:
            continue

        drawdown = (mark - entry) / entry

        sym_state = state.get(sym, {})
        dca_done = sym_state.get('dca_levels', [])

        if len(dca_done) >= dca_cfg['max_dca_count']:
            continue

        current_margin = pos_value / lev
        dca_margin_used = sym_state.get('dca_margin_used', 0)
        total_margin_sym = current_margin + dca_margin_used

        for level_pct, margin_mult in dca_cfg['levels']:
            level_tag = f'{level_pct:.0%}'

            if level_tag in dca_done:
                continue

            if drawdown > level_pct:
                continue

            dca_price = round(entry * (1 + level_pct), 4)
            if dca_price < mark:
                dca_price = round(mark * 0.995, 4)

            margin = dca_cfg['base_margin'] * margin_mult
            new_total = total_margin_sym + margin

            if new_total > dca_cfg['max_margin_per_symbol']:
                continue

            qty_raw = margin * lev / dca_price
            lot_step = _get_lot_step(sym)
            # Используем round вместо ceil для точности (фикс код-ревью)
            qty = round(qty_raw / lot_step) * lot_step
            if lot_step < 1:
                qty = round(qty, len(str(lot_step).split('.')[-1]))
            qty = max(qty, lot_step)

            link_id = f'dca_{sym.lower()}_{int(time.time())}'

            body = {
                'category': 'linear',
                'symbol': sym,
                'side': 'Buy',
                'orderType': 'Limit',
                'qty': str(qty),
                'price': str(dca_price),
                'timeInForce': 'GTC',
                'positionIdx': p['positionIdx'],
                'orderLinkId': link_id,
            }

            data_resp = bybit('POST', '/v5/order/create', body)
            if data_resp and data_resp.get('retCode') == 0:
                dca_done.append(level_tag)
                dca_margin_used += margin
                sym_state['dca_margin_used'] = dca_margin_used
                msg = (
                    f'📉 DCA {sym}: уровень {level_tag} @ ${dca_price:.4f}, '
                    f'{qty} шт, маржа ~${margin:.0f} ({margin_mult:.0f}x базы), '
                    f'просадка {drawdown*100:.1f}%'
                )
                alerts.append(msg)
            else:
                err = data_resp.get('retMsg', '?') if data_resp else 'no response'
                alerts.append(f'⚠️ DCA {sym} {level_tag} не встал: {err}')

            break

        if dca_done:
            sym_state['dca_levels'] = dca_done
            state[sym] = sym_state

    _save_state(state)
    return alerts
