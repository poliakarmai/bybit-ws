"""Подсистема алертов + дедупликация v2.

v2: двухуровневая дедупликация:
  1. Category-cooldown: (level, symbol) — не чаще раза в TTL на символ
  2. Normalized hash: сообщение без цифр — защита от BB%-дрифта
"""

import json
import os
import re
import hashlib
import subprocess
import time
from datetime import datetime
from . import ALERTS, EVENTS_LOG, ALERTS_LOG, HERMES_BIN, ALERT_DEDUP_FILE, ALERT_DEDUP_TTL
from . import safe_run

# Минимальный интервал между алертами одного типа на один символ
CATEGORY_COOLDOWN = {
    "STOP": 600,    # 10 мин — SL/ликвидации
    "TP": 300,      # 5 мин — тейк-профиты
    "ENTRY": 300,   # 5 мин — входы
    "INFO": 120,    # 2 мин — инфо (перегрев и т.д.)
}


def _extract_symbol(msg):
    """Извлечь символ из сообщения (напр. 'SOLUSDT', 'BTCUSDT')."""
    m = re.search(r'\b([A-Z0-9]{4,12}USDT)\b', msg)
    return m.group(1) if m else None


def _normalize_msg(msg):
    """Нормализовать сообщение: убрать цифры, цены, проценты, таймстемпы."""
    # Убираем доллар-цены: $1.2345, $123.45
    msg = re.sub(r'\$\d+\.?\d*', '$X', msg)
    # Убираем проценты: 95%, 0.5%
    msg = re.sub(r'\d+\.?\d*%', 'X%', msg)
    # Убираем числа: 123.45, 0.001
    msg = re.sub(r'\b\d+\.?\d*\b', 'N', msg)
    # Схлопываем множественные пробелы
    msg = re.sub(r'\s+', ' ', msg).strip()
    return msg


def log_event(msg):
    """Записать событие в events.log с автоматической ротацией."""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}\n'

    # Ротация лога при превышении max_size_mb
    try:
        _rotate_if_needed(EVENTS_LOG)
    except Exception:
        pass

    with open(EVENTS_LOG, 'a') as f:
        f.write(line)


def _rotate_if_needed(log_path: str):
    """Ротация лог-файла при превышении размера."""
    max_size_mb = 50
    max_files = 7
    try:
        from .config import Config
        cfg = Config()
        max_size_mb = cfg.logging.get('max_size_mb', 50)
        max_files = cfg.logging.get('max_files', 7)
    except Exception:
        pass

    max_bytes = max_size_mb * 1024 * 1024
    if not os.path.exists(log_path):
        return

    size = os.path.getsize(log_path)
    if size < max_bytes:
        return

    # Сдвигаем файлы: events.log → events.log.1 → events.log.2 → ...
    for i in range(max_files, 0, -1):
        src = f'{log_path}.{i-1}' if i > 1 else log_path
        dst = f'{log_path}.{i}'
        if os.path.exists(src):
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
    # Создаём новый пустой файл
    open(log_path, 'w').close()


def add_alert(level, msg):
    ts = datetime.now().strftime('%H:%M:%S')
    entry = f'[{ts}] [{level}] {msg}'
    ALERTS.append(entry)
    with open(ALERTS_LOG, 'a') as f:
        f.write(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {entry}\n')
    log_event(f'⚠️ {level}: {msg}')
    if level in ('STOP', 'TP', 'ENTRY'):
        emoji = '\U0001F6D1' if level == 'STOP' else ('\U0001F3AF' if level == 'TP' else '\U0001F4CC')
        send_telegram_alert(f'{emoji} {msg}', level=level)


def get_alerts():
    global ALERTS
    out = list(ALERTS)
    ALERTS = []
    return out


def _is_duplicate(msg, level="INFO"):
    """Двухуровневая проверка дубликата.

    Уровень 1: (level, symbol) — категорийный кулдаун.
    Уровень 2: normalized hash — сообщение без цифр.
    """
    try:
        if os.path.exists(ALERT_DEDUP_FILE):
            with open(ALERT_DEDUP_FILE) as f:
                dedup = json.load(f)
        else:
            dedup = {}

        now = time.time()
        sym = _extract_symbol(msg)
        cooldown = CATEGORY_COOLDOWN.get(level, 120)

        # Очистка старых записей
        dedup = {k: v for k, v in dedup.items() if now - v < max(ALERT_DEDUP_TTL, cooldown)}

        # Уровень 1: категорийный кулдаун по символу
        if sym:
            cat_key = f"cat:{level}:{sym}"
            if cat_key in dedup and (now - dedup[cat_key]) < cooldown:
                return True
            dedup[cat_key] = now

        # Уровень 2: нормализованный хеш
        norm = _normalize_msg(msg)
        norm_key = hashlib.md5(norm.encode()).hexdigest()
        if norm_key in dedup and (now - dedup[norm_key]) < ALERT_DEDUP_TTL:
            return True
        dedup[norm_key] = now

        with open(ALERT_DEDUP_FILE, 'w') as f:
            json.dump(dedup, f)
        return False
    except Exception:
        return False


def send_telegram_alert(msg, level="INFO"):
    """Отправить алерт в Telegram с дедупликацией."""
    # Корреляции NEVER шлём юзеру — только в логи
    if 'концентрационный риск' in msg:
        return
    if _is_duplicate(msg, level):
        log_event(f'🔇 Дедупликация [{level}]: пропущен алерт')
        return
    try:
        r = safe_run(
            [HERMES_BIN, 'send', '--to', 'telegram:Poliakarm', msg],
            timeout=15
        )
        if r.returncode != 0:
            log_event(f'⚠️ Telegram send failed (rc={r.returncode}): {r.stderr.strip()[:200]}')
    except subprocess.TimeoutExpired:
        log_event('⚠️ Telegram send timeout (15s)')
    except FileNotFoundError:
        log_event('⚠️ Telegram send failed: hermes binary not found')
    except Exception as e:
        log_event(f'⚠️ Telegram send error: {e}')
