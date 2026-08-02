"""
Structured logging module — JSON-логи для Grafana Loki / grep-friendly.

Замена print()/log_event() в bybit-ws:
  - Все существующие вызовы log_event() продолжают работать
  - При STRUCTURED_LOGGING=1 — добавляется JSON-строка в events.jsonl
  - JSON содержит: ts, level, message, cycle, positions_count, module

Использование:
  from .structured_log import log_info, log_warn, log_error, log_critical

  log_info("cycle start", cycle=42, positions=5)
  → [2026-06-28 18:30:00] cycle start
  → {"ts":"...","level":"INFO","message":"cycle start","cycle":42,"positions":5}
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger('bybit.structured')

STRUCTURED_ENABLED = os.environ.get('STRUCTURED_LOGGING', '0') == '1'
LOG_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
JSON_LOG = LOG_DIR / 'events.jsonl'
_MAX_JSON_SIZE = 50 * 1024 * 1024  # 50 MB rotation


def _write_json(entry: Dict[str, Any]):
    """Записать JSON-строку в events.jsonl."""
    if not STRUCTURED_ENABLED:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Ротация при превышении
        if JSON_LOG.exists() and JSON_LOG.stat().st_size > _MAX_JSON_SIZE:
            backup = JSON_LOG.with_suffix('.jsonl.1')
            if backup.exists():
                backup.unlink()
            JSON_LOG.rename(backup)
        with open(JSON_LOG, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass  # Не роняем основной поток из-за логов


def _make_entry(level: str, message: str, **kwargs) -> dict:
    """Сформировать JSON-запись."""
    entry = {
        'ts': datetime.now().isoformat(),
        'level': level,
        'message': message,
    }
    entry.update(kwargs)
    return entry


def log_info(message: str, **kwargs):
    """Инфо-событие (DEBUG/INFO уровень)."""
    logger.info(message)
    if STRUCTURED_ENABLED:
        _write_json(_make_entry('INFO', message, **kwargs))


def log_warn(message: str, **kwargs):
    """Предупреждение."""
    logger.warning(message)
    if STRUCTURED_ENABLED:
        _write_json(_make_entry('WARN', message, **kwargs))


def log_error(message: str, **kwargs):
    """Ошибка."""
    logger.error(message)
    if STRUCTURED_ENABLED:
        _write_json(_make_entry('ERROR', message, **kwargs))


def log_critical(message: str, **kwargs):
    """Критическое событие (black swan, ликвидация)."""
    logger.critical(message)
    if STRUCTURED_ENABLED:
        _write_json(_make_entry('CRITICAL', message, **kwargs))


def log_cycle(cycle: int, positions: int, orders: int, elapsed: float):
    """Логирование цикла main loop."""
    if STRUCTURED_ENABLED:
        _write_json(_make_entry(
            'INFO', f'cycle #{cycle}',
            cycle=cycle, positions=positions, orders=orders,
            elapsed_sec=round(elapsed, 2)
        ))
