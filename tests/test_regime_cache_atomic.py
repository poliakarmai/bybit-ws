"""Кеш режима не должен быть виден читателю пустым.

28.10.2026 cron-watchdog прислал «regime-кеш нечитаем (Expecting value: line 1
column 1)»: predict_regime писал кеш через open('w'), что усекает файл до нуля,
и watchdog прочитал его ровно в этот момент. Читателей у файла пятеро
(watchdog, self_learn ×4, auto_short), поэтому чинится в писателе.

Тот же баг был в sl_reentry: 0-байтный файл (остаток ENOSPC) спамил
'Expecting value' в events.log каждые 30 секунд.
"""
import json
import threading
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from file_utils import safe_json_write  # noqa: E402

PAYLOAD = {"regime": "TRENDING_DOWN", "confidence": 42, "timestamp": "2026-10-28T12:00:00"}


def _spy_reads(path, write_fn, iterations):
    """Крутить читателя, пока идёт запись; вернуть ошибки чтения."""
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001 — ловим ровно этот класс
                errors.append(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for i in range(iterations):
        write_fn(path, {**PAYLOAD, "confidence": i % 100})
    stop.set()
    t.join(timeout=5)
    return errors


def test_old_style_write_is_caught_by_the_spy(tmp_path):
    """Контроль чувствительности: truncating-write ловится, иначе тест ниже пуст."""
    path = tmp_path / "old.json"

    def truncate_write(p, payload):
        with open(p, "w") as f:  # как было до фикса
            json.dump(payload, f, indent=2)

    assert _spy_reads(path, truncate_write, 2000), "спай не поймал усечённую запись"


def test_atomic_write_is_never_seen_partial(tmp_path):
    path = tmp_path / "cache.json"
    safe_json_write(str(path), PAYLOAD)
    errors = _spy_reads(path, lambda p, d: safe_json_write(str(p), d), 2000)
    assert not errors, errors[:3]
    assert json.loads(path.read_text())["regime"] == "TRENDING_DOWN"
