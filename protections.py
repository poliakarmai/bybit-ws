"""Замки на входы: серия стопов и пауза пары после стопа.

Зачем: поймав серию стопов, бот продолжает входить — по той же паре и по всему
рынку. Это не торговля, это оплата комиссии. Замок ставится на время (переживает
рестарт) и всегда пишет причину в лог: молчаливая блокировка входов стоила
1.5 суток тишины 31.10.

Правила (конфиг `protections`, дефолты в скобках):
  • stop_series: ≥`stop_limit` (6) выходов по SL за `stop_window_h` (12ч)
    → глобальный замок на `stop_lock_h` (6ч);
  • pair_after_sl: SL по паре → замок этой пары на `cooldown_sl_h` (4ч).

Замер на истории 756 закрытых сделок (до 31.10): база −$573. Порог 6SL/12ч плюс
пауза пары 4ч → взято 481 сделки, −$8; при 4…8 SL и паузе 2…6ч результат −$5…−$30,
т.е. выигрыш не зависит от точной настройки. Правило «пара с плохим PF» мерил —
пропускает 140 сделок ради −$66, не окупает сложности, в код не пошло.

Точка входа — `margin_for_strategy()`: через неё идут все авто-входы, поэтому
замок не надо расставлять по каждой стратегии.
"""

import os
import time

from .alerts import log_event
from .config import get_config
from .file_utils import safe_json_write

# BYBIT_PROTECTIONS_STATE — подмена файла состояния (тесты, отладка); в бою не задан
STATE_FILE = (os.environ.get('BYBIT_PROTECTIONS_STATE')
              or os.path.expanduser('~/.local/share/bybit-ws/protections.json'))

_DEFAULTS = {
    'enabled': True,
    'stop_limit': 6,
    'stop_window_h': 12,
    'stop_lock_h': 6,
    'cooldown_sl_h': 4,
}


def _cfg() -> dict:
    try:
        raw = get_config().get('protections', {}) or {}
    except Exception:
        raw = {}
    cfg = dict(_DEFAULTS)
    for k, v in raw.items():
        if k in cfg and v is not None:
            cfg[k] = v
    return cfg


def _load_state() -> dict:
    try:
        import json
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log_event(f'⚠️ protections: состояние не прочитано: {e}')
    return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        safe_json_write(STATE_FILE, state)
    except Exception as e:
        log_event(f'⚠️ protections: состояние не сохранено: {e}')


def _trades(since_ts: float) -> list[dict]:
    """Закрытые сделки из SSOT (trade_history), новые первыми."""
    db_path = os.path.expanduser('~/.local/share/bybit-ws/state.db')
    if not os.path.exists(db_path):
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, pnl, closed_at, COALESCE(exit_reason,'') AS exit_reason "
            "FROM trade_history WHERE closed_at >= ? AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC",
            (int(since_ts),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log_event(f'⚠️ protections: сделки не прочитаны: {e}')
        return []


def _stopout(row: dict) -> bool:
    return str(row.get('exit_reason') or '').upper().startswith('SL')


def compute_locks(trades: list[dict], cfg: dict, now: float = None) -> dict:
    """Правила → замки. Чистая функция: сделки и конфиг снаружи (тесты без БД).

    trades — закрытые сделки, новые первыми, с полями symbol/pnl/closed_at/exit_reason.
    Возвращает {'global': {'until','reason'} | None, 'pairs': {sym: {'until','reason'}}}.
    """
    now = time.time() if now is None else now
    hours = 3600.0
    locks = {'global': None, 'pairs': {}}
    if not cfg.get('enabled', True):
        return locks

    # 1) Серия стопов за окно → глобальный замок
    win = now - float(cfg['stop_window_h']) * hours
    recent_stops = [t for t in trades if _stopout(t) and float(t.get('closed_at') or 0) >= win]
    if len(recent_stops) >= int(cfg['stop_limit']):
        locks['global'] = {
            'until': now + float(cfg['stop_lock_h']) * hours,
            'reason': f"{len(recent_stops)} стопов за {cfg['stop_window_h']}ч",
        }

    # 2) Пауза пары после её стопа
    for t in trades:
        if not _stopout(t):
            continue
        sym = t.get('symbol')
        if not sym:
            continue
        until = float(t.get('closed_at') or 0) + float(cfg['cooldown_sl_h']) * hours
        if until > now:
            prev = locks['pairs'].get(sym)
            if not prev or until > float(prev['until']):
                locks['pairs'][sym] = {'until': until, 'reason': 'пауза после стопа'}
    return locks


def evaluate(now: float = None, trades: list[dict] = None) -> dict:
    """Пересчитать замки, слить с ещё живыми, сохранить, залогировать новое."""
    cfg = _cfg()
    now = time.time() if now is None else now
    hours = 3600.0
    if trades is None:
        # окно чтения — максимум из всех окон, +час запаса
        span = max(float(cfg['stop_window_h']), float(cfg['cooldown_sl_h'])) + 1
        trades = _trades(now - span * hours)

    fresh = compute_locks(trades, cfg, now)
    old = _load_state()
    merged = {'global': None, 'pairs': {}}

    g = fresh.get('global')
    old_g = old.get('global') or {}
    if g:
        merged['global'] = g
    elif float(old_g.get('until') or 0) > now:
        merged['global'] = old_g

    pairs = {}
    for sym, lock in (fresh.get('pairs') or {}).items():
        pairs[sym] = lock
    for sym, lock in (old.get('pairs') or {}).items():
        if float(lock.get('until') or 0) > now and sym not in pairs:
            pairs[sym] = lock
    merged['pairs'] = pairs

    # Логируем только новые/продлённые замки, чтобы не спамить каждый цикл
    old_pairs = old.get('pairs') or {}
    if merged['global'] and merged['global'] != old_g:
        log_event(f"🛑 ЗАМОК входов: {merged['global']['reason']} — "
                  f"до {time.strftime('%H:%M', time.localtime(merged['global']['until']))}")
    for sym, lock in pairs.items():
        prev = old_pairs.get(sym) or {}
        if lock.get('until') != prev.get('until'):
            log_event(f"🛑 Замок {sym}: {lock['reason']} — "
                      f"до {time.strftime('%d.%m %H:%M', time.localtime(lock['until']))}")

    _save_state(merged)
    return merged


_last_logged: dict = {}


def announce_lock(symbol: str, why: str) -> None:
    """Лог замка не чаще раза в 30 мин на пару — иначе спам каждые 30 секунд."""
    key = (symbol or '—', why)
    now = time.time()
    if now - _last_logged.get(key, 0) > 1800:
        _last_logged[key] = now
        log_event(f'🛑 вход {symbol or "—"} заблокирован: {why}')


def lock_reason(symbol: str = None, state: dict = None) -> str | None:
    """Причина замка (None = можно входить). Читает состояние, НЕ пересчитывает."""
    st = _load_state() if state is None else state
    now = time.time()
    g = st.get('global') or {}
    if g and float(g.get('until') or 0) > now:
        return f"глобальный замок: {g.get('reason')}"
    if symbol:
        p = (st.get('pairs') or {}).get(symbol) or {}
        if p and float(p.get('until') or 0) > now:
            return f"замок пары: {p.get('reason')}"
    return None
