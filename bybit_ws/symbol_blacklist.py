"""Перманентный чёрный список торговых символов (v1.0).

Символы, в которые НЕ входить вообще (например, после крупных убытков).
Хранится в JSON-файле в DATA_DIR, формат:

    {
        "STGUSDT": {"reason": "крупный SL -$50", "added_at": 1712345678.9},
        ...
    }

API:
- load_blacklist()         → dict {sym: {reason, added_at}}
- is_blacklisted(sym)      → bool
- add_to_blacklist(sym, reason='')  → None (атомарно: read-modify-write)
- remove_from_blacklist(sym)        → None
- list_blacklist()         → dict (синоним load_blacklist)

Все функции принимают опциональный path — для тестов с tmp_path.
Без path: путь берётся из DATA_DIR (импортируется только внутри функций,
чтобы тесты могли работать без поднятого окружения).
"""

import json
import os
import time
from typing import Optional

# DATA_DIR читаем только внутри функций (на уровне модуля — запрещено,
# иначе тесты на tmp_path сломаются из-за side-effect-импорта).


def _blacklist_path(path: Optional[str] = None) -> str:
    """Путь к JSON-файлу blacklist. Если path задан — вернуть его, иначе DATA_DIR/blacklist.json."""
    if path is not None:
        return path
    from . import DATA_DIR
    return os.path.join(DATA_DIR, 'symbol_blacklist.json')


def load_blacklist(path: Optional[str] = None) -> dict:
    """Прочитать JSON, вернуть dict {sym: {reason, added_at}}.

    При отсутствии файла или битом JSON — вернуть {} (НЕ бросать исключение,
    писать лог через log_event).
    """
    fp = _blacklist_path(path)
    if not os.path.exists(fp):
        return {}
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Защита от неожиданного формата: только dict
        if not isinstance(data, dict):
            from .alerts import log_event
            log_event(f'⚠️ symbol_blacklist: ожидался dict, а {type(data).__name__} — сбрасываем')
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        from .alerts import log_event
        log_event(f'⚠️ symbol_blacklist: не удалось прочитать {fp}: {e}')
        return {}


def is_blacklisted(sym: str, path: Optional[str] = None) -> bool:
    """Есть ли sym в blacklist."""
    return sym in load_blacklist(path)


def _save_blacklist(data: dict, path: Optional[str] = None) -> None:
    """Атомарно сохранить dict в JSON-файл (write через временный файл + rename)."""
    fp = _blacklist_path(path)
    try:
        # Атомарная запись: пишем во временный файл рядом, потом rename
        tmp = fp + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
    except OSError as e:
        from .alerts import log_event
        log_event(f'⚠️ symbol_blacklist: не удалось записать {fp}: {e}')


def add_to_blacklist(sym: str, reason: str = '', path: Optional[str] = None) -> None:
    """Добавить sym в blacklist. Read-modify-write + лог."""
    data = load_blacklist(path)
    data[sym] = {'reason': reason, 'added_at': time.time()}
    _save_blacklist(data, path)
    from .alerts import log_event
    log_event(f'🚫 symbol_blacklist: добавлен {sym} (reason={reason!r})')


def remove_from_blacklist(sym: str, path: Optional[str] = None) -> None:
    """Удалить sym из blacklist. Если нет — тихо (идемпотентно)."""
    data = load_blacklist(path)
    if sym not in data:
        return
    del data[sym]
    _save_blacklist(data, path)
    from .alerts import log_event
    log_event(f'✅ symbol_blacklist: удалён {sym}')


def list_blacklist(path: Optional[str] = None) -> dict:
    """Вернуть весь dict blacklist (синоним load_blacklist)."""
    return load_blacklist(path)
