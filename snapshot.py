"""Снепшоты и сравнение состояний."""
import json
from filelock import FileLock


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    lock = FileLock(path + '.lock', timeout=5)
    with lock:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

def check_position_changes(old, new):
    changes = []
    for sym in old:
        if sym not in new:
            p = old[sym]
            changes.append(('CLOSED', sym, f'{sym} закрыта (было {p["size"]} @ ${p["entry"]:.4f})'))
    for sym in new:
        if sym not in old:
            p = new[sym]
            changes.append(('NEW', sym, f'{sym} открыта ({p["size"]} @ ${p["entry"]:.4f})'))
        else:
            p_old = old[sym]
            p_new = new[sym]
            diff = abs(p_new['size'] - p_old['size'])
            if diff > 0.001:
                if p_new['size'] > p_old['size']:
                    changes.append(('ADD', sym, f'{sym}: +{diff:.2f} (докупка)'))
                else:
                    changes.append(('REDUCE', sym, f'{sym}: -{diff:.2f} (частичное закрытие)'))
    return changes

def check_order_changes(old_orders, new_orders):
    """Сравнивает снапшоты ордеров. Фильтрует перевыставления (отмена + новый)."""
    changes = []
    for key in old_orders:
        if key not in new_orders:
            o = old_orders[key]
            if o['status'] not in ('New', 'Untriggered', 'PartiallyFilled'):
                continue
            sym = o['symbol']
            # Защита от перевыставления: если есть новый ордер того же kind для того же символа — пропускаем
            same_kind_exists = any(
                no.get('kind') == o['kind'] and no.get('symbol') == sym
                for no in new_orders.values()
            )
            if same_kind_exists:
                continue
            if o['kind'] == 'SL':
                changes.append(('SL_HIT', sym,
                    f'🛑 **Stop Loss сработал!** {sym} триггер ${o["trigger"]:.4f}'))
            elif o['kind'] == 'TP':
                changes.append(('TP_HIT', sym,
                    f'🎯 **Take Profit сработал!** {sym} @ ${o["price"]:.4f}. Прибыль зафиксирована ✅'))
            elif o['kind'] == 'LIMIT_ENTRY':
                cum = o.get('cumExecQty', 0)
                if cum > 0:
                    changes.append(('ENTRY_HIT', sym,
                        f'📌 **Лимитка сработала!** {sym} @ ${o["price"]:.4f} (исполнено {cum:.0f}/{o.get("qty",0):.0f})'))
                else:
                    changes.append(('CANCELLED', sym,
                        f'🗑️ **Лимитка отменена** {sym} @ ${o["price"]:.4f} (без исполнения)'))
    return changes
