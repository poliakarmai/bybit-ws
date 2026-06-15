"""Atomic file writes with filelock — защита от race condition при параллельных записях."""
import os
import json
import tempfile
from contextlib import contextmanager
from filelock import FileLock


LOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'locks')
os.makedirs(LOCK_DIR, exist_ok=True)


@contextmanager
def locked_open(path: str, mode: str = 'w'):
    """Контекстный менеджер: блокировка + атомарная запись через temp file.
    
    Атомарно: пишем во временный файл, делаем os.replace.
    При чтении ('r') — только файловая блокировка, без атомарности.
    """
    lock_path = os.path.join(LOCK_DIR, os.path.basename(path) + '.lock')
    lock = FileLock(lock_path, timeout=5)

    with lock:
        if 'r' in mode and 'w' not in mode and 'a' not in mode:
            # Чтение: просто блокировка
            f = open(path, mode)
            try:
                yield f
            finally:
                f.close()
        else:
            # Запись: атомарно через временный файл
            dirname = os.path.dirname(path) or '.'
            with tempfile.NamedTemporaryFile(mode=mode, dir=dirname, delete=False, suffix='.tmp') as tf:
                tmp_path = tf.name
                yield tf
            # Атомарная замена
            os.replace(tmp_path, path)


def safe_json_write(path: str, data, **kwargs):
    """Атомарная запись JSON с блокировкой."""
    kwargs.setdefault('indent', 2)
    kwargs.setdefault('ensure_ascii', False)
    with locked_open(path, 'w') as f:
        json.dump(data, f, **kwargs)


def safe_json_append(path: str, record: dict):
    """Атомарный append JSON-строки в JSONL-файл."""
    with locked_open(path, 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
