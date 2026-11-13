"""Graceful shutdown: SIGTERM будит ожидание, а не ждёт конца цикла.

Контракт (main_async.py):

    SHUTDOWN_EVENT: asyncio.Event | None
    def request_shutdown() -> None            # флаг + event + один лог
    async def _interruptible_sleep(seconds)   # True = разбудили шатдауном

Без него systemd ждал полный цикл (~95с) и добивал процесс SIGKILL'ом через
TimeoutStopSec=10.

Сеть и биржа не нужны: тесты работают только с флагом и event'ом.
Запуск:
    python3 -m pytest tests/test_graceful_shutdown.py -q
"""

import asyncio
import os
import sys
import time
from pathlib import Path

os.environ['BYBIT_LOG_SILENT'] = '1'

import pytest

# Репо-worktree первым в sys.path: иначе `bybit_ws` резолвится в боевую
# копию /home/openclaw/bybit_ws (симлинк на /home/openclaw/bybit-ws),
# где этого кода ещё нет, — тест проверял бы чужой файл.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def clean_globals():
    """Модульные глобалы восстанавливаются: тесты не текут друг в друга."""
    m = __import__('bybit_ws.main_async', fromlist=['main_async'])  # не importorskip: поломка = FAIL, не skip
    # bybit_ws/main_async.py — симлинк на корневой модуль: сверяем по нему.
    assert Path(m.__file__).resolve() == (ROOT / 'main_async.py').resolve(), f'импортирован не тот модуль: {m.__file__}'
    saved = (m.SHUTDOWN, m.SHUTDOWN_EVENT, m._shutdown_logged)
    m.SHUTDOWN = False
    m._shutdown_logged = False
    m.SHUTDOWN_EVENT = asyncio.Event()
    try:
        yield m
    finally:
        m.SHUTDOWN, m.SHUTDOWN_EVENT, m._shutdown_logged = saved
        # asyncio.run() гасит текущий loop → легаси-тесты соседей (get_event_loop().run_until_complete)
        # падают с RuntimeError. Возвращаем им loop, иначе 12 чужих тестов краснеют от нашего файла.
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_interruptible_sleep_wakes_on_event(clean_globals):
    """Event выставлен через 0.05с → сон прерывается, а не идёт 30с."""
    m = clean_globals
    event = asyncio.Event()
    m.SHUTDOWN_EVENT = event

    async def body():
        async def setter():
            await asyncio.sleep(0.05)
            event.set()

        task = asyncio.ensure_future(setter())
        t0 = time.monotonic()
        woke = await m._interruptible_sleep(30)
        elapsed = time.monotonic() - t0
        await task
        return woke, elapsed

    woke, elapsed = asyncio.run(body())
    assert woke is True
    assert elapsed < 2.0, f'sleep не прервался: {elapsed:.2f}s'


def test_interruptible_sleep_times_out(clean_globals):
    """Никто не будит → обычный таймаут, False."""
    m = clean_globals
    m.SHUTDOWN_EVENT = asyncio.Event()

    async def body():
        t0 = time.monotonic()
        woke = await m._interruptible_sleep(0.05)
        return woke, time.monotonic() - t0

    woke, elapsed = asyncio.run(body())
    assert woke is False
    assert elapsed < 1.0, f'таймаут длиннее ожидаемого: {elapsed:.2f}s'


def test_request_shutdown_sets_flag_and_event(clean_globals):
    """request_shutdown() — единственная точка: флаг И event."""
    m = clean_globals
    event = asyncio.Event()
    m.SHUTDOWN_EVENT = event

    m.request_shutdown()

    assert m.SHUTDOWN is True
    assert event.is_set() is True


def test_fresh_process_flags(clean_globals):
    """Повторный вызов не падает и не сбрасывает флаг."""
    m = clean_globals
    m.SHUTDOWN = False
    m._shutdown_logged = False
    m.SHUTDOWN_EVENT = asyncio.Event()

    m.request_shutdown()
    m.request_shutdown()  # второй раз — без исключения, флаг на месте

    assert m.SHUTDOWN is True
    assert m.SHUTDOWN_EVENT.is_set() is True