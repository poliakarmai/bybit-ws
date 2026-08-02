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
    HOME, DATA_DIR,
    EVENTS_LOG, ALERTS_LOG,
    POSITIONS_SNAPSHOT, ORDERS_SNAPSHOT, ORDERS_METADATA,
    BYBIT_CLI, HERMES_BIN,
    ALERTS, ALERT_DEDUP_FILE, ALERT_DEDUP_TTL,
    WATCHDOG_LAST, COVERAGE_CHECK_INTERVAL,
    METRICS_FILE, WATCHLIST_UPDATED_FILE,
    safe_run,
)

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
            from datetime import datetime
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            name = getattr(fn, '__name__', str(fn))
            ctx = f' ({desc})' if desc else ''
            with open(EVENTS_LOG, 'a') as f:
                f.write(f'[{ts}] ⚠️ EXCEPTION {name}{ctx}: {e}\n')
        except Exception:
            pass

# ── Position mode detection ──

POSITION_IDX = {'Buy': 0, 'Sell': 0}

def _detect_position_mode():
    """Определить режим позиций (hedge vs one-way)."""
    global POSITION_IDX
    try:
        from .api import bybit
        resp = bybit('GET', '/v5/position/list?category=linear&settleCoin=USDT')
        if resp.get('retCode') == 0:
            for p in resp['result']['list']:
                if float(p.get('size', 0)) > 0 and int(p.get('positionIdx', 0)) == 1:
                    POSITION_IDX = {'Buy': 0, 'Sell': 1}
                    break
    except Exception:
        pass

_detect_position_mode()
