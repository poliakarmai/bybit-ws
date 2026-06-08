"""Сводки, трейд-журнал, аудит стратегии."""
import os
from . import safe_run
from datetime import datetime
from . import DATA_DIR, BYBIT_CLI, HERMES_BIN, COVERAGE_CHECK_INTERVAL
from .snapshot import load_json, save_json
from .alerts import log_event, add_alert, send_telegram_alert

TRADE_LOG = os.path.join(DATA_DIR, 'trades.md')
TRADE_JSONL = os.path.join(DATA_DIR, 'trades.jsonl')
SUMMARY_SENT_FILE = os.path.join(DATA_DIR, 'last_summary.txt')
PROFIT_TRIGGERS_FILE = os.path.join(DATA_DIR, 'profit_triggers.json')
PROFIT_LEVELS = [30, 50, 100]

# ── Сводка портфеля ──

def should_send_summary():
    now = datetime.now()
    hour, minute = now.hour, now.minute
    if hour not in (9, 21):
        return None
    if minute > 5:
        return None
    try:
        with open(SUMMARY_SENT_FILE) as f:
            if f.read().strip() == now.strftime('%Y-%m-%d-%H'):
                return None
    except:
        pass
    return f"{'☀️ Утренняя' if hour == 9 else '🌙 Вечерняя'} сводка"

def send_summary(label):
    try:
        r = safe_run([BYBIT_CLI, 'balance'], timeout=10)
        balance_out = r.stdout.strip()
        r = safe_run([BYBIT_CLI, 'positions'], timeout=10)
        pos_out = r.stdout.strip()
    except:
        return
    lines = [f'**{label}**', '', '💰 *Баланс:*', balance_out, '', '📊 *Позиции:*', pos_out]
    msg = '\n'.join(lines)
    if len(msg) > 3500:
        msg = msg[:3500] + '...'
    send_telegram_alert(msg)
    with open(SUMMARY_SENT_FILE, 'w') as f:
        f.write(datetime.now().strftime('%Y-%m-%d-%H'))

# ── Профит-триггеры ──

def check_profit_triggers(positions):
    alerts = []
    triggered = load_json(PROFIT_TRIGGERS_FILE)
    for sym, p in positions.items():
        entry, mark = p['entry'], p['mark']
        pnl_pct = (mark - entry) / entry * 100
        if pnl_pct <= 0:
            continue
        sym_triggers = triggered.get(sym, [])
        for level in PROFIT_LEVELS:
            if pnl_pct >= level and level not in sym_triggers:
                alerts.append(f'🔔 {sym} +{pnl_pct:.0f}%! Mark=${mark:.4f}, Entry=${entry:.4f}')
                sym_triggers.append(level)
        triggered[sym] = sym_triggers
    save_json(PROFIT_TRIGGERS_FILE, triggered)
    return alerts

# ── Трейд-журнал ──

def log_trade(sym, entry, exit_price, pnl, side, reason, alert_ref=''):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    now_iso = datetime.now().isoformat()
    ref_str = f' [{alert_ref}]' if alert_ref else ''
    line = f'| {now} | {sym} | {side} | ${entry:.4f} | ${exit_price:.4f} | ${pnl:+.2f} | {reason}{ref_str} |'
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, 'w') as f:
            f.write('# Трейд-журнал\n\n')
            f.write('| Дата | Монета | Сторона | Вход | Выход | PnL | Причина |\n')
            f.write('|------|--------|---------|------|-------|-----|--------|\n')
    with open(TRADE_LOG, 'a') as f:
        f.write(line + '\n')

    # JSONL для машиночитаемого доступа (дашборд, RPC)
    import json as _json
    trade_record = {
        'date': now_iso,
        'symbol': sym,
        'side': side,
        'entry': float(entry),
        'exit': float(exit_price),
        'pnl': float(pnl),
        'reason': reason,
        'alert_ref': alert_ref,
    }
    with open(TRADE_JSONL, 'a') as f:
        f.write(_json.dumps(trade_record) + '\n')

    emoji = '✅' if pnl > 0 else '❌'
    log_event(f'{emoji} Трейд {sym}: ${pnl:+.2f} ({reason})')

# ── Аудит стратегии + сводка покрытия ──

def check_strategy_compliance(positions, orders):
    """Аудит TP/SL покрытия для отдельных позиций."""
    alerts = []
    for sym, p in positions.items():
        if p['side'] != 'Buy' or p['size'] <= 0:
            continue
        size = p['size']
        sl = p.get('stopLoss', '')
        tp_qty = sum(float(o.get('qty', 0)) for o in orders.values()
                     if o['kind'] == 'TP' and o['symbol'] == sym
                     and o['status'] in ('New', 'PartiallyFilled', 'Untriggered'))
        tp_pct = tp_qty / size * 100 if size > 0 else 0
        if tp_pct < 90:
            alerts.append(f'⚠️ {sym}: TP покрытие {tp_pct:.0f}% — проверь ордера')
        if not sl:
            alerts.append(f'🚨 {sym}: НЕТ стоп-лосса!')
    return alerts

def check_coverage_summary(positions, orders):
    """Полная сводка TP/SL покрытия — отправляется раз в 4 часа."""
    if not positions:
        return None
    lines = ['🛡 **TP/SL покрытие:**', '']
    protected = 0
    total = len(positions)
    for sym, p in positions.items():
        if p['side'] != 'Buy' or p['size'] <= 0:
            total -= 1
            continue
        size = p['size']
        sl = p.get('stopLoss')
        tp_qty = sum(float(o.get('qty', 0)) for o in orders.values()
                     if o['kind'] == 'TP' and o['symbol'] == sym
                     and o['status'] in ('New', 'PartiallyFilled', 'Untriggered'))
        tp_pct = tp_qty / size * 100 if size > 0 else 0
        issues = []
        if not sl:
            issues.append('нет SL')
        if tp_pct < 50:
            issues.append(f'TP {tp_pct:.0f}%')
        if not issues:
            protected += 1
        else:
            lines.append(f'  ⚠️ {sym}: {", ".join(issues)}')
    lines.insert(1, f'  {protected}/{total} позиций защищены')
    return '\n'.join(lines) if protected < total else None
