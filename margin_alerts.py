"""
Модуль контроля использования маржи — margin_alerts.py.

Предупреждает, когда суммарная маржа приближается к лимитам:
  • >80% ($400 из $500) — ⚠️ предупреждение (STOP)
  • >95% ($475 из $500) — 🚨 критическое (STOP)
  • >100% ($500)          — 🆘 превышение (STOP)

Данные: positionIM из fetch_positions(), лимит: risk.max_total_margin из конфига.
"""

import os
import json
import time
from datetime import datetime
from . import DATA_DIR, EVENTS_LOG
from .file_utils import safe_json_write
from .config import Config


# ── Дедупликация предупреждений (не чаще раза в 15 мин на уровень) ──────────
_MARGIN_ALERT_FILE = os.path.join(DATA_DIR, 'margin_alert_state.json')
_MARGIN_ALERT_COOLDOWN = 900  # 15 минут между одинаковыми уровнями
_GROSS_ALERT_COOLDOWN = 4 * 3600  # экспозиция меняется медленно: алерт раз в 4ч


def _log_event(msg: str):
    """Локальный логгер (не зависим от alerts.log_event)."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}\n'
    os.makedirs(os.path.dirname(EVENTS_LOG), exist_ok=True)
    with open(EVENTS_LOG, 'a') as f:
        f.write(line)


def _load_alert_state() -> dict:
    """Загрузить состояние дедупликации."""
    if os.path.exists(_MARGIN_ALERT_FILE):
        try:
            with open(_MARGIN_ALERT_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def _save_alert_state(state: dict):
    """Сохранить состояние дедупликации."""
    try:
        safe_json_write(_MARGIN_ALERT_FILE, state)
    except IOError:
        pass


def _should_alert(level_key: str, cooldown: float = None) -> bool:
    """Проверить, можно ли отправлять алерт уровня level_key.

    cooldown — свой интервал для «медленных» уровней (экспозиция держится часами,
    алерт раз в 15 минут был бы спамом: 96 сообщений в сутки).
    """
    state = _load_alert_state()
    now = time.time()
    cd = cooldown or _MARGIN_ALERT_COOLDOWN
    # Очистка старых записей
    state = {k: v for k, v in state.items() if now - v < max(cd, _MARGIN_ALERT_COOLDOWN) * 2}
    if level_key in state:
        if now - state[level_key] < cd:
            return False
    state[level_key] = now
    _save_alert_state(state)
    return True


_EQUITY_CACHE = {'ts': 0.0, 'value': 0.0}
_EQUITY_TTL = 120.0


def _account_equity() -> float:
    """Equity счёта ($) с кешем 2 мин — знаменатель gross leverage.

    Берём totalEquity (баланс + нереализованный PnL), не USDT-монету и не
    walletBalance: 31.10 при балансе $400 equity было $453.
    """
    now = time.time()
    if _EQUITY_CACHE['value'] > 0 and now - _EQUITY_CACHE['ts'] < _EQUITY_TTL:
        return _EQUITY_CACHE['value']
    try:
        from .api import account_wallet
        w = account_wallet() or {}
        eq = float(w.get('equity') or w.get('totalEquity') or 0)
        if eq > 0:
            _EQUITY_CACHE.update({'ts': now, 'value': eq})
            return eq
    except Exception:
        pass
    try:
        from .position_sizing import get_deposit
        return float(get_deposit() or 0)
    except Exception:
        return 0.0


def get_gross_stats(positions: dict) -> dict:
    """Экспозиция портфеля: суммарный notional и плечо всего счёта.

    gross_leverage = Σ|size × mark| / equity. Именно он был 8.7x при лимите
    margin $300: маржа мерила только залог, а риск — в notional.
    Лимит `risk.max_gross_leverage` (0 = выключен) — алерт, не блок входа:
    ручные входы Алексея не режем (75x LTC — его сделка), но видеть их обязаны.
    """
    cfg = Config()
    cap = float(cfg.risk.get('max_gross_leverage', 0) or 0)
    equity = _account_equity()

    gross = 0.0
    by_symbol = {}
    for sym, p in (positions or {}).items():
        n = abs(float(p.get('size') or 0)) * float(p.get('mark') or p.get('entry') or 0)
        if n > 0:
            gross += n
            by_symbol[sym] = round(n, 2)

    return {
        'gross': round(gross, 2),
        'equity': round(equity, 2),
        'gross_leverage': round(gross / equity, 2) if equity > 0 else 0.0,
        'max_gross_leverage': cap,
        'by_symbol': by_symbol,
    }


def get_margin_stats(positions: dict) -> dict:
    """Вычислить статистику использования маржи по текущим позициям.

    Args:
        positions: {symbol: {positionIM, ...}} как из fetch_positions()

    Returns:
        {
            'total_margin': float,       # суммарная маржа, $
            'max_margin': float,         # лимит из конфига, $
            'utilization_pct': float,    # % использования
            'position_count': int,       # число позиций
            'by_symbol': {sym: margin},  # раскладка по символам
        }
    """
    cfg = Config()
    max_margin = float(cfg.risk.get('max_total_margin', 500))

    total_margin = 0.0
    by_symbol = {}

    if positions:
        for sym, p in positions.items():
            # positionIM — initial margin из Bybit API
            margin = float(p.get('positionIM', 0))
            # Фолбэк: если positionIM = 0, считаем по size/entry/leverage
            if margin == 0:
                size = float(p.get('size', 0))
                entry = float(p.get('entry', 0))
                leverage = float(p.get('leverage', 1))
                if leverage > 0 and size > 0 and entry > 0:
                    margin = size * entry / leverage
            total_margin += margin
            if margin > 0:
                by_symbol[sym] = margin

    utilization_pct = (total_margin / max_margin * 100) if max_margin > 0 else 0.0

    return {
        'total_margin': total_margin,
        'max_margin': max_margin,
        'utilization_pct': utilization_pct,
        'position_count': len(positions) if positions else 0,
        'by_symbol': by_symbol,
        **get_gross_stats(positions),
    }


def check_margin_utilization(positions: dict) -> list[str]:
    """Проверить использование маржи и вернуть список алерт-сообщений.

    Пороги:
      • 80%–95%  → ⚠️ предупреждение (близко к лимиту)
      • 95%–100% → 🚨 критическое (почти исчерпано)
      • >100%     → 🆘 превышение (уже за лимитом)

    Args:
        positions: {symbol: {positionIM, ...}} как из fetch_positions()

    Returns:
        Список строк-алертов (может быть пустым).
    """
    stats = get_margin_stats(positions)
    util = stats['utilization_pct']
    total = stats['total_margin']
    max_m = stats['max_margin']
    count = stats['position_count']

    alerts = []

    # ── Экспозиция: gross leverage всего счёта (ручные + авто) ──
    cap = stats['max_gross_leverage']
    lev = stats['gross_leverage']
    if cap > 0 and lev > 0:
        top = sorted(stats['by_symbol'].items(), key=lambda kv: -kv[1])[:2]
        where = ', '.join(f'{s} ${n:.0f}' for s, n in top)
        if lev >= cap and _should_alert('gross_over', _GROSS_ALERT_COOLDOWN):
            alerts.append(
                f'🆘 ЭКСПОЗИЦИЯ {lev:.1f}x equity (лимит {cap:.1f}x): '
                f'${stats["gross"]:.0f} / ${stats["equity"]:.0f} — {where}'
            )
        elif lev >= cap * 0.8 and _should_alert('gross_80', _GROSS_ALERT_COOLDOWN):
            alerts.append(
                f'⚠️ Экспозиция {lev:.1f}x equity — {lev / cap * 100:.0f}% лимита {cap:.1f}x '
                f'(${stats["gross"]:.0f} / ${stats["equity"]:.0f}, {count} позиций)'
            )

    if util > 100:
        key = 'over_100'
        if _should_alert(key):
            alerts.append(
                f'🆘 ИСПОЛЬЗОВАНИЕ МАРЖИ {util:.1f}% '
                f'(${total:.0f} / ${max_m:.0f}) — ПРЕВЫШЕН ЛИМИТ! '
                f'({count} позиций)'
            )
        return alerts  # критичнее уже некуда — пропускаем нижние пороги

    if util > 95:
        key = 'over_95'
        if _should_alert(key):
            alerts.append(
                f'🚨 МАРЖА {util:.1f}% '
                f'(${total:.0f} / ${max_m:.0f}) — критический уровень! '
                f'({count} позиций)'
            )

    if util > 80:
        key = 'over_80'
        if _should_alert(key):
            alerts.append(
                f'⚠️ Использование маржи {util:.1f}% '
                f'(${total:.0f} / ${max_m:.0f}) — выше 80%. '
                f'({count} позиций)'
            )

    return alerts


def check_margin_utilization_detailed(positions: dict) -> dict:
    """Полная проверка с детализацией (для дашборда и сводок).

    Returns:
        {
            'stats': {...},        # из get_margin_stats()
            'alerts': [...],       # список строк-алертов
            'level': str,          # 'ok' | 'warn' | 'critical' | 'exceeded'
        }
    """
    stats = get_margin_stats(positions)
    util = stats['utilization_pct']
    alerts = check_margin_utilization(positions)

    if util > 100:
        level = 'exceeded'
    elif util > 95:
        level = 'critical'
    elif util > 80:
        level = 'warn'
    else:
        level = 'ok'

    return {
        'stats': stats,
        'alerts': alerts,
        'level': level,
    }


# ── CLI (для ручного запуска и тестирования) ─────────────────────────────────
if __name__ == '__main__':
    from .api import fetch_positions
    import sys

    print('🔍 Margin Utilization Check')
    print('=' * 50)

    positions = fetch_positions()
    if not positions:
        print('📭 Нет открытых позиций')
        sys.exit(0)

    result = check_margin_utilization_detailed(positions)
    s = result['stats']

    print(f'📊 Использование маржи: {s["utilization_pct"]:.1f}%')
    print(f'   Суммарная маржа:  ${s["total_margin"]:.2f}')
    print(f'   Максимум:         ${s["max_margin"]:.2f}')
    print(f'   Позиций:           {s["position_count"]}')
    print(f'   Уровень:           {result["level"]}')

    if s['by_symbol']:
        print('\n📋 По символам:')
        for sym, margin in sorted(s['by_symbol'].items(), key=lambda x: -x[1]):
            pct = margin / s['max_margin'] * 100 if s['max_margin'] > 0 else 0
            print(f'   {sym:14s}  ${margin:7.2f}  ({pct:5.1f}%)')

    if result['alerts']:
        print(f'\n🚨 Алерты ({len(result["alerts"])}):')
        for a in result['alerts']:
            print(f'   {a}')
    else:
        print('\n✅ Всё в порядке')
