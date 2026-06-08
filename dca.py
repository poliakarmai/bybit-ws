"""Авто-DCA (лесенка): докупка при падении цены ниже входа.

Уровни:
  -10% от входа → +50% маржи
  -20% от входа → +100% маржи
  -30% от входа → +150% маржи

Ограничения:
  - Все Tier'ы (S/A/B/C/D)
  - Не более 3 докупок на позицию
  - Daily BB < 25% (зелёная зона)
  - Не докупать если позиция уже >$40
"""

import json, math, os, time, subprocess
from . import DATA_DIR, BYBIT_CLI
from .api import bybit, fetch_positions, get_bb_data
from .alerts import log_event
from .config import Config

DCA_STATE_FILE = os.path.join(DATA_DIR, 'dca_state.json')

# Уровни DCA: (падение от входа в %, множитель маржи)
DCA_LEVELS = [
    (-0.10, 1.5),   # -10% → +50% маржи
    (-0.20, 2.0),   # -20% → +100% маржи
    (-0.30, 2.5),   # -30% → +150% маржи
]
BASE_MARGIN = 10   # базовая маржа $10
MAX_POS_VALUE = 40  # макс $40 на монету

# Tier A/B монеты (высокая ликвидность, проверенные проекты)
QUALITY_SET = {
    'BTCUSDT','ETHUSDT','SOLUSDT','LTCUSDT','XRPUSDT','ADAUSDT','DOGEUSDT',
    'LINKUSDT','AVAXUSDT','DOTUSDT','TONUSDT','SUIUSDT','NEARUSDT',
    'INJUSDT','AAVEUSDT','APTUSDT','ARBUSDT','HYPEUSDT','ONDOUSDT',
    'WLDUSDT','ENAUSDT','HBARUSDT','XLMUSDT','BCHUSDT','BNBUSDT',
}


def _load_state():
    if os.path.exists(DCA_STATE_FILE):
        try:
            with open(DCA_STATE_FILE) as f:
                return json.load(f)
        except:
            pass
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
    max_margin_sym = cfg.strategy.dca.get('max_margin_per_symbol', 80)
    max_dca_count = cfg.strategy.dca.get('max_dca_count', 2)

    for sym, p in positions.items():
        # Все Tier'ы — DCA при падении
        side = p['side']
        if side != 'Buy':
            continue  # DCA только для LONG

        entry = p['entry']
        mark = p['mark']
        size = p['size']
        lev = p.get('leverage', 3)

        # Позиция >$40 — не докупаем
        pos_value = mark * size
        if pos_value > MAX_POS_VALUE:
            continue

        # Daily BB должен быть в зелёной зоне (<25%)
        bb = get_bb_data(sym, 'D')
        if not bb or bb['bb_pos'] > 25:
            continue

        # Текущая просадка от входа
        drawdown = (mark - entry) / entry  # отрицательная = падение

        sym_state = state.get(sym, {})
        dca_done = sym_state.get('dca_levels', [])

        # Ограничение: максимум N DCA-добавок на символ
        if len(dca_done) >= max_dca_count:
            continue

        # Считаем текущую суммарную маржу на этот символ
        current_margin = pos_value / lev
        dca_margin_used = sym_state.get('dca_margin_used', 0)
        total_margin_sym = current_margin + dca_margin_used

        for level_pct, margin_mult in DCA_LEVELS:
            level_tag = f'{level_pct:.0%}'

            # Уже докупались на этом уровне
            if level_tag in dca_done:
                continue

            # Проверяем, пробила ли цена уровень
            if drawdown > level_pct:
                continue  # ещё не дошли

            # Цена ниже уровня — ставим лимитку на этом уровне
            dca_price = round(entry * (1 + level_pct), 4)
            # Но не ниже текущей цены (чтобы не сработала сразу по market)
            if dca_price < mark:
                dca_price = round(mark * 0.995, 4)  # чуть ниже рынка

            margin = BASE_MARGIN * margin_mult
            new_total = total_margin_sym + margin

            # Ограничение: не более max_margin_per_symbol суммарной маржи
            if new_total > max_margin_sym:
                continue

            qty_raw = margin * lev / dca_price
            lot_step = _get_lot_step(sym)
            qty = math.ceil(qty_raw / lot_step) * lot_step
            if lot_step < 1:
                qty = round(qty, len(str(lot_step).split('.')[-1]))
            qty = max(qty, lot_step)

            # orderLinkId с таймстемпом
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

            # Один уровень за цикл (не спамим ордерами)
            break

        if dca_done:
            sym_state['dca_levels'] = dca_done
            state[sym] = sym_state

    _save_state(state)
    return alerts
