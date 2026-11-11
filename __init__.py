"""
Bybit WS Monitor v3.10.1 — модульный монитор позиций и ордеров Bybit.

Модули:
  constants  — общие константы и safe_run (разрыв цикла импортов)
  api        — запросы к Bybit API
  alerts     — система алертов + дедупликация
  snapshot   — снепшоты и сравнение
  auto_tp    — авто-TP с retry
  auto_sl    — авто-SL для позиций без стопа (пропускает JUNK)
  auto_short — авто-SHORT + шлак-режим
  trailing_sl— трейлинг SL
  junk_trail — трейлинг-TP для JUNK-шортов
  overbought — сканер перегретых монет
  auto_entry — авто-вход по scoring
  pump_detect— детектор пампов (24ч + недельные)
  dca        — DCA-докупки
  sl_reentry — перезаход после SL (только LONG)
  health     — проверки здоровья (ликвидация, squeeze, funding, etc.)
  correlation— корреляционная матрица
  cleanup    — авто-снятие просроченных ордеров
  reporting  — сводки, трейд-журнал, аудит
  metrics    — метрики успешности
  main       — главный цикл
"""

import os

# ── Реэкспорт из constants.py для обратной совместимости ──
from .constants import (
    ALERT_DEDUP_FILE,
    ALERT_DEDUP_TTL,
    ALERTS,
    ALERTS_LOG,
    BYBIT_CLI,
    COVERAGE_CHECK_INTERVAL,
    DATA_DIR,
    EVENTS_LOG,
    HERMES_BIN,
    HOME,
    METRICS_FILE,
    ORDERS_METADATA,
    ORDERS_SNAPSHOT,
    POSITIONS_SNAPSHOT,
    WATCHDOG_LAST,
    WATCHLIST_UPDATED_FILE,
    safe_run,
)

__all__ = [
    "ALERT_DEDUP_FILE",
    "ALERT_DEDUP_TTL",
    "ALERTS",
    "ALERTS_LOG",
    "BYBIT_CLI",
    "COVERAGE_CHECK_INTERVAL",
    "DATA_DIR",
    "EVENTS_LOG",
    "HERMES_BIN",
    "HOME",
    "METRICS_FILE",
    "ORDERS_METADATA",
    "ORDERS_SNAPSHOT",
    "POSITIONS_SNAPSHOT",
    "WATCHDOG_LAST",
    "WATCHLIST_UPDATED_FILE",
    "safe_run",
]

# Таймауты
GRID_TIMEOUTS = {'M5': 7200, 'M3': 1800, 'OTHER': 7200}

# Trailing SL
TRAIL_SL_PERCENT = 0.15
TRAIL_CHECK_INTERVAL = 5

# Лимиты и защита
MAX_POSITION_VALUE = 40
DAILY_DRAWDOWN_LIMIT = 1.0  # отключено по просьбе пользователя
SHORT_ALERT_COOLDOWN = 14400  # 4 часа — не спамить перегревами
SHORT_ALERT_LAST = {}
SHORT_ALERT_FILE = os.path.join(DATA_DIR, 'short_alert_last.json')

def _load_short_alerts():
    """Загрузить persistent стейт SHORT_ALERT_LAST из файла."""
    global SHORT_ALERT_LAST
    try:
        if os.path.exists(SHORT_ALERT_FILE):
            import json as _json
            with open(SHORT_ALERT_FILE) as f:
                SHORT_ALERT_LAST = _json.load(f)
    except Exception:
        pass

def _save_short_alerts():
    """Сохранить SHORT_ALERT_LAST в файл (вызывается из overbought.py)."""
    try:
        import json as _json
        with open(SHORT_ALERT_FILE + '.tmp', 'w') as f:
            _json.dump(SHORT_ALERT_LAST, f)
        os.replace(SHORT_ALERT_FILE + '.tmp', SHORT_ALERT_FILE)
    except Exception:
        pass

_load_short_alerts()

# Auto-TP failure tracker → retry с backoff
TP_FAIL_COUNT = {}
TP_FAIL_BACKOFF = {}
TP_FAIL_DELAYS = [30, 60, 120, 300]
TP_PERM_SKIP = set()
TP_PERM_SKIP_SIZES = {}
TP_SKIP_FILE = os.path.join(DATA_DIR, 'tp_skip.json')
TP_MAX_FAILS = len(TP_FAIL_DELAYS)

# Фаза 5.4: Режимные флаги LONG/SHORT
REGIME_LONG_ENABLED = True
REGIME_SHORT_ENABLED = True

# Глобальные
DAILY_START_EQUITY = None
SHUTDOWN_REQUESTED = False

# ── Безопасные операции с логированием ──

def safe_op(fn, *args, default=None, desc='', **kwargs):
    """Выполнить fn(*args, **kwargs), логируя исключения в events.log."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        try:
            from datetime import UTC, datetime
            ts = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')
            name = getattr(fn, '__name__', str(fn))
            ctx = f' ({desc})' if desc else ''
            with open(EVENTS_LOG, 'a') as f:
                f.write(f'[{ts}] ⚠️ EXCEPTION {name}{ctx}: {e}\n')
        except Exception:
            pass

# ── Position mode detection ──

# Режим позиций задаётся ПО СИМВОЛУ: 10.11.2026 на живом счёте одновременно
# GRAMUSDT positionIdx=0 (one-way) и LTCUSDT positionIdx=1 (hedge). Глобальный
# флаг отправлял Sell с idx=1 (сторона Buy) → биржа отклоняла шорт.
_HEDGE_SYMS: set = set()


def _detect_position_mode():
    """Заполнить кэш hedge-символов по открытым позициям."""
    try:
        from .api import bybit
        resp = bybit('GET', '/v5/position/list?category=linear&settleCoin=USDT')
        if resp.get('retCode') == 0:
            for p in resp['result']['list']:
                if float(p.get('size', 0)) > 0 and int(p.get('positionIdx', 0)) in (1, 2):
                    _HEDGE_SYMS.add(p['symbol'])
    except Exception:
        pass


def position_idx(sym: str, side: str) -> int:
    """positionIdx для ордера: hedge → Buy=1 / Sell=2, one-way → 0.

    ponytail: символ без открытой позиции считается one-way, пока первый принятый
    ордер не покажет обратное (learn_position_idx). Эндпоинта «режим символа»
    в Bybit v5 нет — при отказе 10001/130025 нужен повтор с idx=1/2.
    """
    if sym in _HEDGE_SYMS:
        return 1 if side == 'Buy' else 2
    return 0


def learn_position_idx(sym: str, idx: int) -> None:
    """Запомнить режим символа по успешно принятому ордеру."""
    if idx in (1, 2):
        _HEDGE_SYMS.add(sym)
    elif idx == 0:
        _HEDGE_SYMS.discard(sym)


_detect_position_mode()
