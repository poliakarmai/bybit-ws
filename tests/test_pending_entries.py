"""Регрессия №8: таймаут в run_in_thread не теряет факт возможного входа.

Было: `asyncio.wait_for` отменял future, поток продолжал ставить ордер, результат
выбрасывался — бот не знал ни об исполнении, ни об ошибке.

Стало:
1. `asyncio.wait` — цикл не ждёт, поток живёт, поздний результат логируется;
2. вход, повисший по таймауту, попадает в `_PENDING_ENTRIES` и блокирует повторный
   вход по символу до сверки со снапшотом позиций.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_pending_entries.py -q
"""
import asyncio
import os
import sys
import time

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture()
def m():
    mod = pytest.importorskip('bybit_ws.main_async')
    mod._PENDING_ENTRIES.clear()
    yield mod
    mod._PENDING_ENTRIES.clear()


def test_timeout_releases_loop_without_killing_thread(m):
    """Цикл отпускается по таймауту, поток продолжает работать (не отменён).

    Меряем не общее время (asyncio.run на выходе дожидается executor-потоков),
    а состояние флага на момент возврата: поток ещё не дошёл до конца.
    """
    finished = []

    def slow():
        time.sleep(0.4)
        finished.append(1)
        return 'late'

    async def scenario():
        t0 = time.time()
        res, err = await m.run_in_thread(slow, timeout=0.05)
        return res, err, time.time() - t0, list(finished)

    res, err, elapsed, at_return = asyncio.run(scenario())

    assert res == [] and err == 'slow'
    assert elapsed < 0.3, f'цикл ждал поток {elapsed:.2f}с — таймаут не работает'
    assert at_return == [], 'поток уже завершился к возврату — таймаут не сработал'


def test_entry_timeout_marks_symbol_pending(m, monkeypatch):
    """Таймаут входа → символ в pending, а не молчаливый пропуск."""
    async def fake_timeout(fn, *a, **k):
        return [], getattr(fn, '__name__', '?')

    monkeypatch.setattr(m, 'run_in_thread', fake_timeout)
    monkeypatch.setattr(m, '_entries_paused', lambda: False)

    def execute_scalp(info):
        raise AssertionError('не должен вызываться в этом тесте')

    ok, err = asyncio.run(m._maybe_execute(execute_scalp, {'symbol': 'SOLUSDT', 'side': 'Sell'}))
    assert err == 'execute_scalp'
    assert m._PENDING_ENTRIES['SOLUSDT']['side'] == 'Sell'


def test_pending_blocks_repeat_entry(m, monkeypatch):
    """Пока судьба прошлого входа неизвестна — по символу не входим снова."""
    m._PENDING_ENTRIES['SOLUSDT'] = {'side': 'Sell', 'ts': time.time()}
    called = []

    async def fake_any(fn, *a, **k):
        called.append(fn)
        return True, None

    monkeypatch.setattr(m, 'run_in_thread', fake_any)
    monkeypatch.setattr(m, '_entries_paused', lambda: False)

    def execute_scalp(info):
        pass

    ok, err = asyncio.run(m._maybe_execute(execute_scalp, {'symbol': 'SOLUSDT', 'side': 'Sell'}))
    assert (ok, err) == (False, 'pending_verify')
    assert called == [], 'вход исполнялся, хотя символ ждёт сверки'


def test_reconcile_confirms_late_entry(m):
    """Позиция появилась → вход подтверждён, блокировка снята."""
    m._PENDING_ENTRIES['SOLUSDT'] = {'side': 'Sell', 'ts': time.time()}
    m._pending_reconcile({'SOLUSDT': {'symbol': 'SOLUSDT', 'size': 3.0}})
    assert 'SOLUSDT' not in m._PENDING_ENTRIES


def test_reconcile_expires_without_position(m):
    """Позиции нет и TTL вышел → ордер не встал, блокировка снята."""
    m._PENDING_ENTRIES['SOLUSDT'] = {'side': 'Sell', 'ts': time.time() - m.PENDING_TTL - 1}
    m._pending_reconcile({})
    assert 'SOLUSDT' not in m._PENDING_ENTRIES


def test_reconcile_keeps_pending_within_ttl(m):
    """Позиции ещё нет, но TTL не вышел (ордер в пути) → ждём дальше."""
    m._PENDING_ENTRIES['SOLUSDT'] = {'side': 'Sell', 'ts': time.time()}
    m._pending_reconcile({})
    assert 'SOLUSDT' in m._PENDING_ENTRIES
