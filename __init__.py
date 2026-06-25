"""
Bybit WS Monitor v3.10.1 — модульный монитор позиций и ордеров Bybit.

Модули:
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

HOME = os.path.expanduser('~')
DATA_DIR = os.path.join(HOME, '.local', 'share', 'bybit-ws')
os.makedirs(DATA_DIR, exist_ok=True)

EVENTS_LOG = os.path.join(DATA_DIR, 'events.log')
ALERTS_LOG = os.path.join(DATA_DIR, 'alerts.log')
POSITIONS_SNAPSHOT = os.path.join(DATA_DIR, 'positions.json')
ORDERS_SNAPSHOT = os.path.join(DATA_DIR, 'orders.json')
ORDERS_METADATA = os.path.join(DATA_DIR, 'orders_metadata.json')
BYBIT_CLI = os.path.join(HOME, '.local', 'bin', 'bybit')
HERMES_BIN = os.path.join(HOME, '.local', 'bin', 'hermes')

import signal, subprocess
def safe_run(cmd, timeout=15):
    """subprocess.run без зомби: Popen + start_new_session + killpg при таймауте."""
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait(timeout=5)
        raise

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
        pass  # файла нет или битый — ок, начинаем с пустого

def _save_short_alerts():
    """Сохранить SHORT_ALERT_LAST в файл (вызывается из overbought.py)."""
    try:
        import json as _json
        with open(SHORT_ALERT_FILE + '.tmp', 'w') as f:
            _json.dump(SHORT_ALERT_LAST, f)
        os.replace(SHORT_ALERT_FILE + '.tmp', SHORT_ALERT_FILE)
    except Exception:
        pass

# Загружаем при импорте
_load_short_alerts()

# Auto-TP failure tracker → retry с backoff
TP_FAIL_COUNT = {}
TP_FAIL_BACKOFF = {}       # {sym: next_retry_timestamp}
TP_FAIL_DELAYS = [30, 60, 120, 300]  # backoff: 30с, 1мин, 2мин, 5мин
TP_PERM_SKIP = set()        # перманентный скип — монеты где qty < мин. лота
TP_PERM_SKIP_SIZES = {}     # {sym: size} — размер позиции на момент скипа
TP_SKIP_FILE = os.path.join(DATA_DIR, 'tp_skip.json')  # персистентность PERM_SKIP
TP_MAX_FAILS = len(TP_FAIL_DELAYS)

# Дедупликация алертов
ALERT_DEDUP_FILE = os.path.join(DATA_DIR, 'last_alerts.json')
ALERT_DEDUP_TTL = 300  # 5 минут

# Фаза 5.4: Режимные флаги LONG/SHORT (устанавливаются из main_async.py)
REGIME_LONG_ENABLED = True
REGIME_SHORT_ENABLED = True

# Watchdog
WATCHDOG_LAST = 0.0

# Глобальные
DAILY_START_EQUITY = None
SHUTDOWN_REQUESTED = False
ALERTS = []

# Watchlist rotation — обновлять раз в 24ч
WATCHLIST_UPDATED_FILE = os.path.join(DATA_DIR, 'watchlist_updated.txt')

# TP/SL coverage summary interval (каждые 480 циклов = 4 часа)
COVERAGE_CHECK_INTERVAL = 480

# Метрики
METRICS_FILE = os.path.join(DATA_DIR, 'metrics.json')

# ── Безопасные операции с логированием ──

def safe_op(fn, *args, default=None, desc='', **kwargs):
    """Выполнить fn(*args, **kwargs), логируя исключения в events.log.
    
    Возвращает результат fn или default при ошибке.
    Не роняет вызывающий код.
    """
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
        return default
