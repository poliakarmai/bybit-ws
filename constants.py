"""
Общие константы и утилиты bybit-ws.

Вынесены из __init__.py чтобы разорвать цикл:
  __init__.py → api.py → alerts.py → __init__.py

Теперь: __init__.py → constants ← alerts.py (без цикла)
"""
import os
import signal
import subprocess

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

# Алерты
ALERTS = []
ALERT_DEDUP_FILE = os.path.join(DATA_DIR, 'last_alerts.json')
ALERT_DEDUP_TTL = 300

# Watchdog
WATCHDOG_LAST = 0.0

# TP/SL coverage
COVERAGE_CHECK_INTERVAL = 480

# Метрики
METRICS_FILE = os.path.join(DATA_DIR, 'metrics.json')

# Watchlist rotation
WATCHLIST_UPDATED_FILE = os.path.join(DATA_DIR, 'watchlist_updated.txt')

# ── safe_run ──

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
