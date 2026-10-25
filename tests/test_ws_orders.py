"""Регрессия: WS-ветка `async_positions_snapshot_ws` возвращает ордера.

Контракт (main_async.py, async_positions_snapshot_ws):

    orders = {}
    try:
        orders = await fetch_orders_async()
    except Exception as e:
        log_event(f'⚠️ WS orders fetch error: {e}')

Раньше в WS-режиме возвращался `orders = {}` → auto_take_profit ставил дубли TP.
Теперь ордера берутся из `fetch_orders_async()`; при ошибке — `{}` + запись в лог.

Сеть не используется: ws_client и fetch_orders_async замоканы.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_ws_orders.py -q
"""
import asyncio
import os
import sys

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture(scope='module')
def m():
    return pytest.importorskip('bybit_ws.main_async')


WS_POSITIONS = {'BTCUSDT': {'symbol': 'BTCUSDT', 'side': 'Buy', 'size': 1.0}}


@pytest.fixture()
def ws(monkeypatch):
    """WS-клиент в «живом» режиме, без сети.

    Патчим атрибуты модуля bybit_ws.ws_client: main_async импортирует их
    внутри функции (`from .ws_client import ...`), поэтому подмена видна.
    """
    ws_client = pytest.importorskip('bybit_ws.ws_client')
    monkeypatch.setattr(ws_client, 'is_full_enabled', lambda: True)
    monkeypatch.setattr(ws_client, 'is_private_connected', lambda: True)
    monkeypatch.setattr(ws_client, 'is_private_stale', lambda *a, **k: False)
    monkeypatch.setattr(ws_client, 'get_all_positions', lambda: dict(WS_POSITIONS))
    return ws_client


def _run(m, last_rest_sync=0.0, rest_interval=10_000.0):
    """Вызов WS-ветки без REST-сверки (now - last_rest_sync < rest_interval)."""
    return asyncio.run(m.async_positions_snapshot_ws(last_rest_sync, rest_interval))


@pytest.fixture(autouse=True)
def _no_rest_sync(monkeypatch):
    """Страховка: REST-сверка не должна ходить в сеть ни при каком раскладе."""
    async def _boom(*a, **k):
        raise AssertionError('fetch_positions_and_orders не должен вызываться в этих тестах')

    import bybit_ws.main_async as _m
    monkeypatch.setattr(_m, 'fetch_positions_and_orders', _boom)


def test_ws_branch_returns_orders(m, ws, monkeypatch):
    """WS-режим: ордера из fetch_orders_async попадают в результат."""
    expected = {
        'BTCUSDT': [{'orderId': 'abc', 'symbol': 'BTCUSDT', 'orderType': 'Limit'}],
    }
    calls = []

    async def fake_fetch_orders_async():
        calls.append(1)
        return expected

    monkeypatch.setattr(m, 'fetch_orders_async', fake_fetch_orders_async)

    positions, orders, _last = _run(m)

    assert positions == WS_POSITIONS, positions
    assert orders == expected, f'WS-ветка вернула пустые/чужие ордера: {orders!r}'
    assert orders, 'ордера в WS-режиме не должны быть пустыми'
    assert len(calls) == 1, f'fetch_orders_async должен вызываться 1 раз, было {len(calls)}'


def test_ws_branch_orders_error_returns_empty_and_logs(m, ws, monkeypatch):
    """Ошибка fetch_orders_async → {} и запись в лог, без падения.

    Контракт: `orders = {}` + `log_event('⚠️ WS orders fetch error: ...')`.
    Если реализация кеширует последний снимок — он обязан быть ПУСТЫМ на старте
    (иначе это не «{} при ошибке», а устаревшие ордера).
    """
    logged = []

    async def boom():
        raise RuntimeError('orders api down')

    monkeypatch.setattr(m, 'fetch_orders_async', boom)
    monkeypatch.setattr(m, 'log_event', lambda msg, *a, **k: logged.append(str(msg)))

    # Сбрасываем возможный кеш ордеров, чтобы тест не зависел от истории процесса.
    for name in ('_ORDERS_CACHE', '_orders_cache'):
        cache = getattr(m, name, None)
        if isinstance(cache, dict):
            monkeypatch.setattr(m, name, {}, raising=False)

    positions, orders, _last = _run(m)

    assert orders == {}, f'при ошибке ожидали {{}}, получили {orders!r}'
    assert positions, 'позиции из WS-кеша должны остаться'
    assert any('WS orders fetch error' in msg for msg in logged), (
        f'нет записи в лог про ошибку ордеров: {logged}'
    )
    assert any('orders api down' in msg for msg in logged), (
        f'в логе нет текста исключения: {logged}'
    )


def test_rest_fallback_when_ws_down(m, ws, monkeypatch):
    """WS не подключён → REST-фолбэк (async_positions_snapshot), ордера тоже возвращаются."""
    monkeypatch.setattr(ws, 'is_private_connected', lambda: False)

    rest_orders = {'ETHUSDT': [{'orderId': 'r1'}]}

    async def fake_snapshot():
        return {'ETHUSDT': {'symbol': 'ETHUSDT'}}, rest_orders

    monkeypatch.setattr(m, 'async_positions_snapshot', fake_snapshot)

    positions, orders, last = _run(m, last_rest_sync=123.0)

    assert positions == {'ETHUSDT': {'symbol': 'ETHUSDT'}}, positions
    assert orders == rest_orders, orders
    assert last == 123.0, last
