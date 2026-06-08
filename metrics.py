"""Метрики успешности."""
import json, os, time
from datetime import datetime
from . import DATA_DIR, METRICS_FILE
from .snapshot import load_json

def _today_key():
    return datetime.now().strftime('%Y-%m-%d')

def record_alert(level, is_false=False):
    """Записать алерт в метрики."""
    metrics = load_json(METRICS_FILE)
    today = _today_key()
    if today not in metrics:
        metrics = {today: {'tp_real': 0, 'tp_false': 0, 'sl_real': 0, 'sl_false': 0,
                           'entry': 0, 'auto_entry_placed': 0, 'auto_entry_filled': 0,
                           'auto_entry_pnl': 0.0}}
    m = metrics[today]
    if level == 'TP':
        if is_false: m['tp_false'] += 1
        else: m['tp_real'] += 1
    elif level == 'SL':
        if is_false: m['sl_false'] += 1
        else: m['sl_real'] += 1
    elif level == 'ENTRY':
        m['entry'] += 1
    with open(METRICS_FILE, 'w') as f:
        json.dump(metrics, f, indent=2)

def record_auto_entry(placed=False, filled=False, pnl=0.0):
    metrics = load_json(METRICS_FILE)
    today = _today_key()
    if today not in metrics:
        metrics[today] = {'tp_real': 0, 'tp_false': 0, 'sl_real': 0, 'sl_false': 0,
                          'entry': 0, 'auto_entry_placed': 0, 'auto_entry_filled': 0,
                          'auto_entry_pnl': 0.0}
    m = metrics[today]
    if placed: m['auto_entry_placed'] += 1
    if filled: m['auto_entry_filled'] += 1
    m['auto_entry_pnl'] += pnl
    with open(METRICS_FILE, 'w') as f:
        json.dump(metrics, f, indent=2)

def get_metrics():
    metrics = load_json(METRICS_FILE)
    today = _today_key()
    return metrics.get(today, {})

def print_metrics():
    m = get_metrics()
    print(f"📊 Метрики за {_today_key()}:")
    print(f"   TP: {m.get('tp_real',0)} реальных / {m.get('tp_false',0)} ложных")
    print(f"   SL: {m.get('sl_real',0)} реальных / {m.get('sl_false',0)} ложных")
    print(f"   ENTRY: {m.get('entry',0)}")
    print(f"   Авто-входы: {m.get('auto_entry_placed',0)} выставлено / {m.get('auto_entry_filled',0)} сработало")
    if m.get('auto_entry_filled', 0) > 0:
        print(f"   Средний PnL авто-входа: ${m['auto_entry_pnl']/m['auto_entry_filled']:+.2f}")
