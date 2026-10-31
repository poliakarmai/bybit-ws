"""Защита ручных позиций от автоматического вмешательства монитора.

Когда мы входим в обход авто-входа (ручной market/limit через bybit raw),
монитор не знает что позиция «наша» и может:
- переставить SL (auto_sl)
- воткнуть свой TP (auto_tp)
- закрыть при первом профите (instant_tp)
- орать «не по стратегии» (strategy_compliance)

Механизм: pumps.json с флагом "manual": true.
Все опасные модули проверяют is_manual_position() перед вмешательством.
"""

import os
import json
import time
from .alerts import log_event

PUMP_STATE_FILE = os.path.join(os.path.expanduser('~/.local/share/bybit-ws'), 'pumps.json')


def _load_state():
    """Загрузить pumps.json (безопасно)."""
    if os.path.exists(PUMP_STATE_FILE):
        try:
            with open(PUMP_STATE_FILE) as f:
                return json.loads(f.read())
        except Exception as e:
            log_event(f'⚠️ manual_positions: ошибка чтения pumps.json: {e}')
    return {}


def _save_state(state):
    """Сохранить pumps.json атомарно."""
    tmp = PUMP_STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, PUMP_STATE_FILE)


def is_manual_position(sym, include_closed=False):
    """Помечена ли позиция как ручная.

    include_closed=True — вопрос про ЗАКРЫТУЮ сделку (импорт closed-pnl): метка
    снимается с торговли, но 48ч ещё помнит, что выход был ручным. Живые модули
    (auto_sl/auto_tp/reporting) спрашивают без флага и закрытую метку не видят.
    """
    state = _load_state()
    entry = state.get(sym, {})
    if not entry.get('manual', False):
        return False
    return bool(include_closed) or not entry.get('closed_at')


def mark_manual_position(sym, entry=None, side='Buy', note='', leverage=None, auto_sltp=False,
                         **diag):
    """Пометить позицию как ручную — монитор её не трогает (кроме auto_sltp).

    Args:
        sym: символ (например 'NEARUSDT')
        entry: цена входа (опционально, для истории)
        side: 'Buy' или 'Sell'
        note: заметка (например 'x10 wide-SL')
        leverage: плечо (опционально)
        auto_sltp: True → SL/TP бот ставит сам (по стратегии), но self-learn исключает
        diag: bb_pct/rsi/margin — что известно о входе, уйдёт в entry_diag
    """
    state = _load_state()
    entry_data = state.get(sym, {})
    entry_data.pop('closed_at', None)  # новый ручной вход — прошлое закрытие не в счёт
    entry_data['manual'] = True
    entry_data['manual_ts'] = time.time()
    entry_data['auto_sltp'] = bool(auto_sltp)
    if entry is not None:
        entry_data['manual_entry'] = entry
    if side:
        entry_data['manual_side'] = side
    if note:
        entry_data['manual_note'] = note
    if leverage is not None:
        entry_data['manual_leverage'] = leverage
    state[sym] = entry_data
    _save_state(state)
    # логируем причину ручного входа → trade_history.entry_reason (через entry_diag)
    if note or diag:
        try:
            from .state_db import db
            db.save_entry_diagnostic(sym, side, entry_reason=note, **diag)
        except Exception as e:
            log_event(f'⚠️ mark_manual_position entry_reason: {e}')


# ── Атрибуция по бирже (кто поставил ордер) ─────────────────────────────
# Каждый ордер, который ставит бот, помечен своим orderLinkId-префиксом. Что
# пришло с другим/пустым link — ручной вход: приложение (link пустой), /enter,
# MCP. createType для этого НЕ годится: Bybit отдаёт CreateByUser и для наших
# API-ордеров без явного link (проверено на JCTUSDT 27.10).
AUTO_LINK_PREFIXES = ('ae_', 'as_', 're_', 'dca_', 'bb_', 'mr_', 'fe_', 'pd_')
_ORIGIN_TTL = 600
_origin_cache: dict = {}   # sym -> (ts, origin, link)


def origin_by_link(link):
    """'auto' — ордер бота (наш префикс), иначе 'manual'."""
    return 'auto' if str(link or '').startswith(AUTO_LINK_PREFIXES) else 'manual'


# Плечи, с которыми входит только бот (config: long/short 3x, скальп/фандинг/junk 10x).
AUTO_LEVERAGES = (3.0, 10.0)


def _entry_diag(sym, side):
    try:
        from .state_db import db
        return db.get_kv(f'entry_diag:{sym}:{side}') or {}
    except Exception:
        return {}


_judge_cache: dict = {}   # ts -> (время чтения, [(ts, символ), ...])


def _judge_mentions():
    """Упоминания судьи в events.log: приговоры хранятся только там."""
    from .constants import EVENTS_LOG
    key = 'all'
    now = time.time()
    cached = _judge_cache.get(key)
    if cached and now - cached[0] < 600:
        return cached[1]
    out = []
    try:
        import re
        pat_ts = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
        pat_sym = re.compile(r'\b([A-Z0-9]{2,15}USDT)\b')
        with open(EVENTS_LOG, errors='replace') as f:
            for line in f:
                if 'JUDGE' not in line and 'судья' not in line.lower():
                    continue
                m = pat_ts.match(line)
                s = pat_sym.search(line)
                if m and s:
                    out.append((time.mktime(time.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')),
                                s.group(1)))
    except Exception as e:
        log_event(f'⚠️ manual_positions: events.log для судьи не прочитан: {e}')
    _judge_cache[key] = (now, out)
    return out


def judge_seen(sym, entry_ts, window=900):
    if not entry_ts:
        return False
    t0 = float(entry_ts) - window
    return any(s == sym and t0 <= ts <= float(entry_ts) + window for ts, s in _judge_mentions())


def manual_signals(sym, side, entry_ts=None, leverage=None):
    """Признаки ручного входа, которых у авто-ветки не бывает.

    По одному сигналу — не приговор (бот иногда тоже молчит), но вместе с плечом
    вне авто-набора дают уверенность без истории ордеров (Bybit помнит 7 дней).
    """
    reasons = []
    diag = _entry_diag(sym, side)
    if not any(diag.get(k) is not None for k in ('score', 'bb_pct', 'rsi', 'entry_reason')):
        reasons.append('нет анализа на входе (entry_diag пуст)')
    if not judge_seen(sym, entry_ts):
        reasons.append('нет решения судьи рядом с входом')
    if leverage and float(leverage) not in AUTO_LEVERAGES:
        reasons.append(f'плечо {float(leverage):g}x вне авто-набора {AUTO_LEVERAGES}')
    return reasons


def decide_manual(sym, side, entry_ts=None, leverage=None, link=None):
    """(ручная?, причины). Пустой link — факт приложения. Иначе нужен сигнал
    И плечо вне авто-набора: только признаков «нет анализа» мало, у скальпа
    diag тоже может не быть."""
    reasons = manual_signals(sym, side, entry_ts, leverage)
    if link == '':
        return True, ['ордер из приложения (orderLinkId пуст)'] + reasons
    lev_off = any('плечо' in r for r in reasons)
    return (lev_off and len(reasons) >= 2), reasons


def entry_origin(sym, since_ms=None):
    """(origin, link) ордера, которым ОТКРЫТА позиция; (None, None) — не узнали.

    Кеш 10 мин на символ: вызывается из цикла по каждой позиции.
    """
    now = time.time()
    cached = _origin_cache.get(sym)
    if cached and now - cached[0] < _ORIGIN_TTL:
        return cached[1], cached[2]
    origin, link = None, None
    try:
        from .api import fetch_order_history, fetch_positions
        if not since_ms:
            since_ms = int((fetch_positions().get(sym) or {}).get('openTime') or 0)
        if since_ms:
            orders = fetch_order_history(sym, since_ms - 120_000, end_ms=since_ms + 900_000)
            side = str((fetch_positions().get(sym) or {}).get('side') or '')
            cands = [o for o in orders if o.get('side') == side] or orders
            if cands:
                best = min(cands, key=lambda o: abs(int(o.get('createdTime', 0) or 0) - since_ms))
                link = best.get('orderLinkId') or ''
                origin = origin_by_link(link)
    except Exception as e:
        log_event(f'⚠️ manual_positions: атрибуция {sym} не получена: {e}')
    _origin_cache[sym] = (now, origin, link)
    return origin, link


def is_auto_sltp(sym):
    """True если ручная позиция помечена auto_sltp — бот ставит SL/TP сам."""
    state = _load_state()
    return state.get(sym, {}).get('auto_sltp', False)


def unmark_manual_position(sym):
    """Снять ручную метку (позиция закрылась или передана авто)."""
    state = _load_state()
    if sym in state:
        state[sym]['manual'] = False
        state[sym].pop('manual_ts', None)
        state[sym].pop('manual_entry', None)
        state[sym].pop('manual_side', None)
        state[sym].pop('manual_note', None)
        state[sym].pop('manual_leverage', None)
        # Не удаляем запись целиком — могут быть другие поля (pump state)
        _save_state(state)
