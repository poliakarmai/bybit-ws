"""Регрессия: пауза RPC блокирует ВСЕ источники входов (единая точка `_maybe_execute`).

Контракт (main_async.py):

    async def _maybe_execute(fn, entry_info, label='') -> tuple[bool, str]:
        ...

Пауза (`rpc_state['paused'] is True`) → `(False, 'paused')` и `fn` НЕ вызывается.
Без паузы → `await run_in_thread(fn, entry_info)`.

Инвариант по исходнику: ни один `execute_*` не вызывается напрямую через
`run_in_thread(...)` в обход `_maybe_execute` — иначе источник снова сможет
войти на паузе.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_pause_guard.py -q
"""
import asyncio
import os
import re
import sys
from pathlib import Path

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MAIN_ASYNC = ROOT / 'main_async.py'


@pytest.fixture(scope='module')
def m():
    """Модуль bybit_ws.main_async (без сети: импорт только определяет функции)."""
    return pytest.importorskip('bybit_ws.main_async')


@pytest.fixture()
def rpc():
    """Настоящий bybit_ws.rpc — подменяем только содержимое rpc_state."""
    return pytest.importorskip('bybit_ws.rpc')


class _FakeFn:
    """Синхронный «исполнитель входа» со счётчиком вызовов.

    Имитирует `execute_*` из main_async: у него есть `__name__`
    (его использует лог паузы) и он возвращает (ok, err) как run_in_thread.
    """

    def __init__(self, name='execute_fake'):
        self.__name__ = name
        self.calls = []

    def __call__(self, entry_info):
        self.calls.append(entry_info)
        return True, 'ok'


def test_paused_blocks_entry(m, rpc, monkeypatch):
    """paused=True → (False, 'paused'), fn не вызван."""
    assert hasattr(m, '_maybe_execute'), 'main_async._maybe_execute отсутствует'

    monkeypatch.setitem(rpc.rpc_state, 'paused', True)
    fake = _FakeFn()

    result = asyncio.run(m._maybe_execute(fake, {'symbol': 'X'}))

    assert result == (False, 'paused'), f'ожидали (False, "paused"), получили {result!r}'
    assert fake.calls == [], f'на паузе вход не должен исполняться, вызовы: {fake.calls}'


def test_paused_blocks_entry_with_label(m, rpc, monkeypatch):
    """label не обходит паузу."""
    assert hasattr(m, '_maybe_execute'), 'main_async._maybe_execute отсутствует'

    monkeypatch.setitem(rpc.rpc_state, 'paused', True)
    fake = _FakeFn()

    result = asyncio.run(m._maybe_execute(fake, {'symbol': 'X'}, 'funding'))

    assert result == (False, 'paused'), result
    assert fake.calls == [], fake.calls


def test_unpaused_executes_once(m, rpc, monkeypatch):
    """paused=False → fn вызван ровно один раз с entry_info."""
    assert hasattr(m, '_maybe_execute'), 'main_async._maybe_execute отсутствует'

    monkeypatch.setitem(rpc.rpc_state, 'paused', False)
    fake = _FakeFn()
    entry_info = {'symbol': 'X'}

    result = asyncio.run(m._maybe_execute(fake, entry_info))

    assert len(fake.calls) == 1, f'ожидали ровно 1 вызов, получили {len(fake.calls)}'
    assert fake.calls[0] == entry_info, fake.calls
    # run_in_thread возвращает (result, error)
    assert isinstance(result, tuple) and len(result) == 2, result
    assert result[1] is None, f'run_in_thread вернул ошибку: {result!r}'


def test_no_direct_execute_calls_outside_maybe_execute():
    """Инвариант по исходнику: нет `run_in_thread(execute_...)` вне _maybe_execute."""
    src = MAIN_ASYNC.read_text(encoding='utf-8')
    lines = src.splitlines()

    # Границы тела _maybe_execute (по отступу).
    start = None
    for i, line in enumerate(lines):
        if re.match(r'\s*async def _maybe_execute\b', line):
            start = i
            break
    assert start is not None, 'в main_async.py нет определения _maybe_execute'

    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent:
            end = j
            break

    pattern = re.compile(r'run_in_thread\(\s*execute_')
    offenders = [
        (i + 1, lines[i].strip())
        for i in range(len(lines))
        if pattern.search(lines[i]) and not (start <= i < end)
    ]

    assert offenders == [], (
        'прямой вызов run_in_thread(execute_*) в обход _maybe_execute '
        f'(пауза не сработает): {offenders}'
    )
