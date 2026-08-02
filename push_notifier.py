"""
Модуль Push-уведомлений — ntfy + Telegram fallback (Фаза 6.4).

Провайдеры:
  1. ntfy (бесплатный, self-hosted или ntfy.sh) — первичный канал
  2. Telegram (через существующий send_telegram_alert) — fallback

Приоритеты:
  CRITICAL — SL сработал, ликвидация, circuit breaker, экстренное закрытие
  HIGH     — вход в позицию, TP, DCA-докупка
  NORMAL   — сигналы, конфлюенс, информационные сообщения

Кастомные звуки:
  CRITICAL → "siren" (ntfy) / громкий алерт
  HIGH     → "up" (ntfy)
  NORMAL   → без звука

Дедупликация:
  Не слать одинаковый алерт чаще чем раз в 5 минут (на основе хеша сообщения).

Конфигурация (env):
  NTFY_TOPIC   — имя топика (обязательно для ntfy)
  NTFY_SERVER  — URL сервера (по умолчанию https://ntfy.sh)
  PUSH_ENABLED — 1=включено, 0=выключено (default 1)
"""

import hashlib
import json
import os
import time
from datetime import datetime
from typing import Optional

import httpx

# ── Конфигурация из env ──────────────────────────────────────────────────────

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PUSH_ENABLED = os.environ.get("PUSH_ENABLED", "1") == "1"

# ── Приоритеты ───────────────────────────────────────────────────────────────

# Маппинг приоритетов ntfy: https://docs.ntfy.sh/publish/#message-priority
NTFY_PRIORITY_MAP = {
    "CRITICAL": "max",   # 5 — max приоритет, вибрация даже в DnD
    "HIGH": "high",      # 4 — высокий приоритет
    "NORMAL": "default", # 3 — обычный
}

# Маппинг кастомных звуков (Android notification channel)
NTFY_TAGS_MAP = {
    "CRITICAL": "warning,siren",     # siren + warning tag
    "HIGH": "arrow_up",              # стрелка вверх
    "NORMAL": "bell",                # колокольчик
}

# ── Маппинг уровней алертов на push-приоритеты ──────────────────────────────

LEVEL_TO_PUSH_PRIORITY = {
    "STOP": "CRITICAL",
    "TP": "HIGH",
    "ENTRY": "HIGH",
    "CONFLUENCE": "NORMAL",
    "INFO": "NORMAL",
}

# ── Дедупликация (in-memory, 5 минут) ────────────────────────────────────────

# Путь к persistent-дедупликации (выживает рестарты)
_ALERT_PUSH_DEDUP_FILE = os.path.expanduser(
    "~/.local/share/bybit-ws/push_dedup.json"
)
_DEDUP_TTL = 300  # 5 минут — не слать одинаковый алерт чаще этого
_dedup_cache: dict[str, float] = {}  # in-memory кэш (быстрый lookup)
_dedup_loaded = False


def _load_dedup():
    """Загрузить persistent кэш дедупликации из файла."""
    global _dedup_cache, _dedup_loaded
    if _dedup_loaded:
        return
    _dedup_loaded = True
    try:
        if os.path.exists(_ALERT_PUSH_DEDUP_FILE):
            with open(_ALERT_PUSH_DEDUP_FILE) as f:
                raw = json.load(f)
            now = time.time()
            # Очищаем просроченные
            _dedup_cache = {
                k: v for k, v in raw.items()
                if isinstance(v, (int, float)) and now - v < _DEDUP_TTL * 2
            }
    except Exception:
        _dedup_cache = {}


def _save_dedup():
    """Сохранить кэш дедупликации в файл."""
    try:
        os.makedirs(os.path.dirname(_ALERT_PUSH_DEDUP_FILE), exist_ok=True)
        with open(_ALERT_PUSH_DEDUP_FILE + ".tmp", "w") as f:
            json.dump(_dedup_cache, f)
        os.replace(_ALERT_PUSH_DEDUP_FILE + ".tmp", _ALERT_PUSH_DEDUP_FILE)
    except Exception:
        pass  # не фатально — потеряем дедупликацию при рестарте


def _push_key(msg: str, priority: str) -> str:
    """Создать ключ дедупликации: SHA256 первых 120 символов + приоритет."""
    if not isinstance(msg, str):
        msg = str(msg)
    normalized = msg.strip()[:120]
    h = hashlib.sha256(f"{priority}:{normalized}".encode()).hexdigest()[:16]
    return h


def _is_push_duplicate(msg: str, priority: str) -> bool:
    """Проверить, не отправляли ли мы это сообщение в последние 5 минут."""
    _load_dedup()
    now = time.time()
    key = _push_key(msg, priority)

    if key in _dedup_cache and (now - _dedup_cache[key]) < _DEDUP_TTL:
        return True

    _dedup_cache[key] = now
    _save_dedup()
    return False


# ── HTTP-сессия (переиспользование) ──────────────────────────────────────────

_http_session: Optional[httpx.Client] = None


def _get_http() -> httpx.Client:
    """Ленивое создание httpx-сессии с таймаутом."""
    global _http_session
    if _http_session is None:
        _http_session = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": "bybit-ws-push/1.0"},
        )
    return _http_session


# ── Отправка через ntfy ──────────────────────────────────────────────────────

def _send_ntfy(
    msg: str,
    title: str = "",
    priority: str = "NORMAL",
    tags: str = "",
) -> bool:
    """
    Отправить push-уведомление через ntfy.

    Возвращает True при успехе, False при ошибке.
    """
    if not NTFY_TOPIC:
        return False

    ntfy_priority = NTFY_PRIORITY_MAP.get(priority, "default")
    ntfy_tags = tags or NTFY_TAGS_MAP.get(priority, "")

    # Очищаем от не-ASCII (эмодзи в заголовках HTTP ломают httpx)
    # .strip() — иначе после вырезания эмодзи остаётся лидирующий пробел
    safe_title = (title or "Bybit WS Alert").encode("ascii", errors="ignore").decode("ascii").strip()
    safe_tags = ntfy_tags.encode("ascii", errors="ignore").decode("ascii") if ntfy_tags else ""

    headers = {
        "Title": safe_title,
        "Priority": ntfy_priority,
    }
    if safe_tags:
        headers["Tags"] = safe_tags

    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"

    try:
        client = _get_http()
        resp = client.post(url, content=msg.encode("utf-8"), headers=headers)
        if resp.status_code in (200, 201, 202):
            return True
        # Логируем ошибку, но не роняем процесс
        print(
            f"[push] ntfy returned {resp.status_code}: {resp.text[:200]}",
            flush=True,
        )
        return False
    except httpx.TimeoutException:
        print("[push] ntfy timeout (10s)", flush=True)
        return False
    except httpx.ConnectError:
        print("[push] ntfy connection failed", flush=True)
        return False
    except Exception as e:
        print(f"[push] ntfy error: {e}", flush=True)
        return False


# ── Главная функция отправки ─────────────────────────────────────────────────

def send_push(
    msg: str,
    level: str = "INFO",
    title: str = "",
    priority: str = "",
    tags: str = "",
    telegram_fallback: bool = True,  # False для trading-алертов (уже в супергруппе)
) -> bool:
    """
    Отправить push-уведомление: сначала ntfy, при неудаче — Telegram fallback.

    Args:
        msg: Текст сообщения.
        level: Уровень алерта (STOP, TP, ENTRY, CONFLUENCE, INFO).
               Автоматически маппится на push-приоритет.
        title: Заголовок уведомления (необязательно).
        priority: Явный приоритет (CRITICAL/HIGH/NORMAL). Если не указан,
                  вычисляется из level.
        tags: Кастомные теги/звуки (необязательно).

    Returns:
        True если хотя бы один канал отправил успешно, иначе False.
    """
    # Проверяем глобальный флаг
    if not PUSH_ENABLED:
        return False

    # Определяем приоритет
    if not priority:
        priority = LEVEL_TO_PUSH_PRIORITY.get(level, "NORMAL")

    # Дедупликация: не слать одно и то же чаще 5 мин
    if _is_push_duplicate(msg, priority):
        return True  # молча — считаем «успехом», чтобы не спамить лог

    # Формируем заголовок с эмодзи
    emoji_map = {
        "CRITICAL": "🚨",
        "HIGH": "⚡",
        "NORMAL": "📢",
    }
    emoji = emoji_map.get(priority, "📢")

    if not title:
        # Авто-заголовок на основе level
        level_titles = {
            "STOP": f"{emoji} STOP LOSS / LIQUIDATION",
            "TP": f"{emoji} TAKE PROFIT",
            "ENTRY": f"{emoji} NEW POSITION",
            "CONFLUENCE": f"{emoji} STRONG CONFLUENCE",
            "INFO": f"{emoji} INFO",
        }
        title = level_titles.get(level, f"{emoji} Alert")

    # Санитайзинг title для HTTP-заголовков (RFC 7230)
    # | невалиден в HTTP-заголовках → заменяем на -
    title = title.replace('/', '-').replace('|', '-').replace('\n', ' ')

    # Пробуем ntfy
    ntfy_ok = _send_ntfy(
        msg=msg,
        title=title,
        priority=priority,
        tags=tags,
    )

    if ntfy_ok:
        return True

    # Fallback: Telegram (только если telegram_fallback=True — избегаем дублей)
    if not telegram_fallback:
        return False

    try:
        from .alerts import send_telegram_alert

        # Добавляем префикс приоритета для Telegram
        prefix_map = {
            "CRITICAL": "🚨🚨🚨",
            "HIGH": "⚡",
            "NORMAL": "ℹ️",
        }
        prefix = prefix_map.get(priority, "")
        telegram_msg = f"{prefix} {msg}" if prefix else msg

        send_telegram_alert(telegram_msg, level=level)
        print(f"[push] ntfy failed, sent via Telegram fallback", flush=True)
        return True
    except Exception as e:
        print(f"[push] Telegram fallback also failed: {e}", flush=True)
        return False


# ── Событийно-ориентированные хелперы ────────────────────────────────────────

def send_critical_alert(msg: str, title: str = "") -> bool:
    """Отправить CRITICAL-алерт (SL, ликвидация, emergency). Без Telegram-дубля."""
    return send_push(
        msg=msg,
        level="STOP",
        title=title or "🚨 CRITICAL ALERT",
        priority="CRITICAL",
        tags="warning,siren",
        telegram_fallback=False,
    )


def send_high_alert(msg: str, level: str = "ENTRY", title: str = "") -> bool:
    """Отправить HIGH-алерт (вход, TP). Без Telegram-дубля."""
    return send_push(
        msg=msg,
        level=level,
        title=title,
        priority="HIGH",
        tags="arrow_up",
        telegram_fallback=False,
    )


def send_normal_alert(msg: str, level: str = "INFO", title: str = "") -> bool:
    """Отправить NORMAL-алерт (сигналы, конфлюенс, инфо)."""
    return send_push(
        msg=msg,
        level=level,
        title=title,
        priority="NORMAL",
    )


# ── Состояние канала ─────────────────────────────────────────────────────────

def get_push_status() -> dict:
    """Вернуть статус push-каналов."""
    return {
        "enabled": PUSH_ENABLED,
        "ntfy_topic": NTFY_TOPIC if NTFY_TOPIC else "(not set)",
        "ntfy_server": NTFY_SERVER,
        "ntfy_configured": bool(NTFY_TOPIC),
        "telegram_fallback": True,  # всегда доступен через существующий alerts.py
    }
