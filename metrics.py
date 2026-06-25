"""Метрики успешности."""
import json
from datetime import datetime
from . import METRICS_FILE
from .file_utils import safe_json_write

def _today_key():
    return datetime.now().strftime('%Y-%m-%d')

def record_alert(level, is_false=False, symbol=None):
    """Записать алерт в метрики. symbol — тикер (напр. 'BTCUSDT')."""
    with open(METRICS_FILE) as f:
        metrics = json.load(f)
    today = _today_key()
    if today not in metrics:
        metrics[today] = {'tp_real': 0, 'tp_false': 0, 'sl_real': 0, 'sl_false': 0,
                          'entry': 0, 'auto_entry_placed': 0, 'auto_entry_filled': 0,
                          'auto_entry_pnl': 0.0,
                          'tp_coins': [], 'sl_coins': [], 'entry_coins': []}
    m = metrics[today]
    # Ensure coin lists exist (backward compat)
    for key in ('tp_coins', 'sl_coins', 'entry_coins'):
        if key not in m:
            m[key] = []
    
    coin = symbol.replace('USDT', '') if symbol else None
    if level == 'TP':
        if is_false: m['tp_false'] += 1
        else: m['tp_real'] += 1
        if coin and coin not in m['tp_coins']:
            m['tp_coins'].append(coin)
    elif level == 'SL':
        if is_false: m['sl_false'] += 1
        else: m['sl_real'] += 1
        if coin and coin not in m['sl_coins']:
            m['sl_coins'].append(coin)
    elif level == 'ENTRY':
        m['entry'] += 1
        if coin and coin not in m['entry_coins']:
            m['entry_coins'].append(coin)
    safe_json_write(METRICS_FILE, metrics)

def record_auto_entry(placed=False, filled=False, pnl=0.0, symbol=None):
    with open(METRICS_FILE) as f:
        metrics = json.load(f)
    today = _today_key()
    if today not in metrics:
        metrics[today] = {'tp_real': 0, 'tp_false': 0, 'sl_real': 0, 'sl_false': 0,
                          'entry': 0, 'auto_entry_placed': 0, 'auto_entry_filled': 0,
                          'auto_entry_pnl': 0.0,
                          'tp_coins': [], 'sl_coins': [], 'entry_coins': []}
    m = metrics[today]
    for key in ('tp_coins', 'sl_coins', 'entry_coins'):
        if key not in m:
            m[key] = []
    
    if placed: m['auto_entry_placed'] += 1
    if filled: m['auto_entry_filled'] += 1
    m['auto_entry_pnl'] += pnl
    
    coin = symbol.replace('USDT', '') if symbol else None
    if filled and coin and coin not in m['entry_coins']:
        m['entry_coins'].append(coin)
    
    safe_json_write(METRICS_FILE, metrics)

def get_metrics():
    with open(METRICS_FILE) as f:
        metrics = json.load(f)
    today = _today_key()
    return metrics.get(today, {})

def print_metrics():
    m = get_metrics()
    print(f"📊 Метрики за {_today_key()}:")
    print(f"   TP: {m.get('tp_real',0)} реальных / {m.get('tp_false',0)} ложных")
    print(f"   SL: {m.get('sl_real',0)} реальных / {m.get('sl_false',0)} ложных")
    print(f"   ENTRY: {m.get('entry',0)}")
    
    tp_coins = m.get('tp_coins', [])
    sl_coins = m.get('sl_coins', [])
    entry_coins = m.get('entry_coins', [])
    if tp_coins:
        print(f"   TP монеты: {', '.join(tp_coins)}")
    if sl_coins:
        print(f"   SL монеты: {', '.join(sl_coins)}")
    if entry_coins:
        print(f"   Входы: {', '.join(entry_coins)}")
    
    print(f"   Авто-входы: {m.get('auto_entry_placed',0)} выставлено / {m.get('auto_entry_filled',0)} сработало")
    if m.get('auto_entry_filled', 0) > 0:
        print(f"   Средний PnL авто-входа: ${m['auto_entry_pnl']/m['auto_entry_filled']:+.2f}")
