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
from .config import Config


# ── Дедупликация предупреждений (не чаще раза в 15 мин на уровень) ──────────
_MARGIN_ALERT_FILE = os.path.join(DATA_DIR, 'margin_alert_state.json')
_MARGIN_ALERT_COOLDOWN = 900  # 15 минут между одинаковыми уровнями


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
        with open(_MARGIN_ALERT_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except IOError:
        pass


def _should_alert(level_key: str) -> bool:
    """Проверить, можно ли отправлять алерт уровня level_key."""
    state = _load_alert_state()
    now = time.time()
    # Очистка старых записей
    state = {k: v for k, v in state.items() if now - v < _MARGIN_ALERT_COOLDOWN * 2}
    if level_key in state:
        if now - state[level_key] < _MARGIN_ALERT_COOLDOWN:
            return False
    state[level_key] = now
    _save_alert_state(state)
    return True


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
