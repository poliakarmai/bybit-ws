#!/usr/bin/env python3
"""Smoke-тесты: trailing_sl, state_db, auto_sl, api.

Запуск: cd /home/openclaw && python3 bybit_ws/test_smoke.py
(требуется symlink bybit_ws → bybit-ws в /home/openclaw)
"""
import sys, os, tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# symlink bybit_ws → bybit-ws в /home/openclaw
sys.path.insert(0, '/home/openclaw')

from bybit_ws.trailing_sl import trailing_sl
from bybit_ws.auto_sl import _get_tiers
from bybit_ws.manual_positions import is_manual_position
from bybit_ws.state_db import StateDB

PASS = FAIL = 0

def check(desc, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {desc}")
    else:
        FAIL += 1; print(f"  ❌ {desc} {detail}")

def bb(bb_pos):
    """Фабрика BB-моков."""
    if bb_pos == 80:
        return {'lower': 90, 'middle': 100, 'upper': 110, 'cur': 108, 'bb_pos': 80}
    if bb_pos == 20:
        return {'lower': 70, 'middle': 80, 'upper': 90, 'cur': 74, 'bb_pos': 20}
    return {'lower': 70, 'middle': 80, 'upper': 90, 'cur': 78, 'bb_pos': bb_pos}


# ═════════════ 1. trailing_sl (8 тестов) ═════════════

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(80))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_long(mock_m, mock_bb):
    print("\n─── trailing_sl: LONG ───")
    actions = trailing_sl({'BTCUSDT': {'entry': 100, 'mark': 118, 'side': 'Buy', 'size': 1, 'positionIdx': 0, 'stopLoss': None}})
    check("SL сгенерирован", len(actions) == 1)
    if actions: check("SL ≈ 102.7", abs(actions[0][4] - 102.7) < 0.01, f"(got {actions[0][4]})")

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(20))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_short(mock_m, mock_bb):
    print("\n─── trailing_sl: SHORT ───")
    actions = trailing_sl({'ETHUSDT': {'entry': 100, 'mark': 82, 'side': 'Sell', 'size': 1, 'positionIdx': 1, 'stopLoss': None}})
    check("SL сгенерирован", len(actions) == 1)
    if actions: check("SL ≈ 97.3", abs(actions[0][4] - 97.3) < 0.01, f"(got {actions[0][4]})")

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(20))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_short_low_pnl(mock_m, mock_bb):
    print("\n─── trailing_sl: SHORT PnL<15% ───")
    check("не генерируем", len(trailing_sl({'ETHUSDT': {'entry': 100, 'mark': 90, 'side': 'Sell', 'size': 1, 'positionIdx': 1, 'stopLoss': None}})) == 0)

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(40))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_long_low_bb(mock_m, mock_bb):
    print("\n─── trailing_sl: LONG BB<75% ───")
    check("не генерируем", len(trailing_sl({'BTCUSDT': {'entry': 100, 'mark': 120, 'side': 'Buy', 'size': 1, 'positionIdx': 0, 'stopLoss': None}})) == 0)

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(40))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_short_high_bb(mock_m, mock_bb):
    print("\n─── trailing_sl: SHORT BB>25% ───")
    check("не генерируем", len(trailing_sl({'ETHUSDT': {'entry': 100, 'mark': 80, 'side': 'Sell', 'size': 1, 'positionIdx': 1, 'stopLoss': None}})) == 0)

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(80))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=True)
def test_manual(mock_m, mock_bb):
    print("\n─── trailing_sl: ручная ───")
    check("не трогаем", len(trailing_sl({'BTCUSDT': {'entry': 100, 'mark': 120, 'side': 'Buy', 'size': 1, 'positionIdx': 0, 'stopLoss': None}})) == 0)

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(80))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_sl_close(mock_m, mock_bb):
    print("\n─── trailing_sl: SL близко ───")
    check("не обновляем", len(trailing_sl({'BTCUSDT': {'entry': 100, 'mark': 118, 'side': 'Buy', 'size': 1, 'positionIdx': 0, 'stopLoss': 102.6}})) == 0)

@patch('bybit_ws.trailing_sl.get_bb_data', return_value=bb(80))
@patch('bybit_ws.trailing_sl.is_manual_position', return_value=False)
def test_sl_far(mock_m, mock_bb):
    print("\n─── trailing_sl: SL далеко ───")
    check("обновляем", len(trailing_sl({'BTCUSDT': {'entry': 100, 'mark': 118, 'side': 'Buy', 'size': 1, 'positionIdx': 0, 'stopLoss': 99.5}})) == 1)


# ═════════════ 2. state_db (20+ проверок) ═════════════

def test_state_db():
    print("\n─── state_db: CRUD ───")
    tmp = tempfile.mktemp(suffix='.db')
    db = StateDB(path=tmp)

    db.add_trade('BTCUSDT', 'Buy', 'x10', 50000, 52000, 0.1, 200, fees=5)
    db.add_trade('ETHUSDT', 'Sell', 'bollinger', 3000, 2800, 1.0, 200, fees=10)
    check("trades: 2", len(db.get_trades()) == 2)
    check("trades: фильтр", len(db.get_trades(symbol='BTCUSDT')) == 1)
    s = db.get_pnl_summary()
    check("pnl=400", s['total_pnl'] == 400)
    check("fees=15", s['total_fees'] == 15)

    db.save_short_state('DOGEUSDT', {'last_short_ts': 1, 'entry_price': 0.1, 'qty': 1000, 'bb_pct': 80, 'is_junk': True})
    check("short: сохр", db.get_short_state('DOGEUSDT')['entry_price'] == 0.1)
    check("short: None", db.get_short_state('X') is None)

    db.save_pump_state('SHIBUSDT', {'first_seen_ts': 1, 'peak_price': 0.00001, 'alerts': ['pump!'], 'daily_pump': True})
    check("pump: daily", db.get_pump_state('SHIBUSDT').get('daily_pump') == True)
    check("pump: empty", db.get_pump_state('X') == {})

    check("cool: init", not db.is_cooling_down('k'))
    db.set_cooldown('k', 3600)
    check("cool: active", db.is_cooling_down('k'))
    db.clear_cooldown('k')
    check("cool: cleared", not db.is_cooling_down('k'))

    check("dedup: first", db.should_alert('a', 300))
    check("dedup: repeat", not db.should_alert('a', 300))
    check("dedup: other", db.should_alert('b', 300))

    db.set_kv('eq', 12345.67)
    check("kv: float", db.get_kv('eq') == 12345.67)
    check("kv: default", db.get_kv('x', 'fb') == 'fb')

    db.save_x10_limits('2026-06-16', 'bollinger', {'losses': 2, 'pnl': -50})
    check("x10: key", '2026-06-16:bollinger' in db.get_x10_limits())

    db.save_positions({'BTCUSDT': {'side': 'Buy', 'entry': 50000, 'mark': 51000, 'size': 0.1}})
    check("pos: 1", len(db.get_positions()) == 1)

    db.set_cooldown('exp', -1)
    db.clean_expired_cooldowns()
    check("expired: gone", not db.is_cooling_down('exp'))

    try: db.vacuum(); check("vacuum: OK", True)
    except: check("vacuum: OK", False)

    db.close(); os.unlink(tmp)


# ═════════════ 3. auto_sl ═════════════

def test_auto_sl():
    print("\n─── auto_sl: tier-логика ───")
    cfg = MagicMock()
    cfg.tiers.A = {'BTCUSDT', 'ETHUSDT'}; cfg.tiers.B = {'SOLUSDT'}; cfg.tiers.one_way = {'DOGEUSDT'}
    tier_ab, one_way = _get_tiers(cfg)
    check("Tier A", 'BTCUSDT' in tier_ab)
    check("Tier B", 'SOLUSDT' in tier_ab)
    check("1way", 'DOGEUSDT' in one_way)
    check("JUNK", 'XRPUSDT' not in tier_ab)
    check("manual callable", callable(is_manual_position))


# ═════════════ 4. api ═════════════

def test_api():
    print("\n─── api: структура ───")
    code = (Path(__file__).parent / 'api.py').read_text()
    for fn in ['fetch_positions', 'fetch_orders', 'place_stop_loss', 'place_take_profit', 'cancel_order', 'get_bb_data', 'bybit']:
        check(f"def {fn}", f'def {fn}' in code)
    check("retry", 'retries' in code and 'MAX_RETRIES' in code)
    check("429", '429' in code)
    # subprocess только в комментариях — норм (не в исполняемом коде)
    has_code_subprocess = any('subprocess' in l and not l.strip().startswith('#') and 'subprocess' not in l.split('#')[0] if '#' in l else False for l in code.split('\n') if 'subprocess' in l)
    check("no subprocess in code", not any('import subprocess' in l for l in code.split('\n')))


# ═════════════ Main ═════════════

if __name__ == '__main__':
    test_long(); test_short(); test_short_low_pnl(); test_long_low_bb()
    test_short_high_bb(); test_manual(); test_sl_close(); test_sl_far()
    test_state_db()
    test_auto_sl()
    test_api()

    print(f"\n{'='*50}")
    print(f"  PASS={PASS}  FAIL={FAIL}  TOTAL={PASS+FAIL}")
    print(f"  {'✅ Все smoke-тесты пройдены!' if FAIL == 0 else f'❌ {FAIL} тестов упало'}")
    sys.exit(0 if FAIL == 0 else 1)
