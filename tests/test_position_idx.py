"""Тесты _order_with_idx_fallback / _position_idx (задача D, rev.2).

Проверяем стратегию «попробуй дефолт 0 → исправься по 10001 → запомни в кэш»
без зондирования режима позиции и без живых API.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_position_idx.py -q
"""
import importlib
import os
import sys
import types
from typing import Any

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')


class _NoDB:
    def save_pump_state(self, *a, **k):
        pass


def _install_stubs():
    """Заглушки bybit_ws.api и bybit_ws.state_db — без сети и без боевого state.db."""
    api_stub = types.ModuleType('bybit_ws.api')
    setattr(api_stub, 'bybit', lambda *a, **k: {'retCode': 0, 'result': {'list': []}})
    setattr(api_stub, 'bybit_async', None)
    setattr(api_stub, 'fetch_atr', lambda *a, **k: 0.0)
    sys.modules['bybit_ws.api'] = api_stub

    db_stub = types.ModuleType('bybit_ws.state_db')
    setattr(db_stub, 'db', _NoDB())
    sys.modules['bybit_ws.state_db'] = db_stub


_install_stubs()

# importlib — чтобы статический анализатор не пытался резолвить bybit_ws (симлинк вне sys.path).
pump_detect: Any = importlib.import_module('bybit_ws.pump_detect')

# Заглушки нужны были только на время импорта pump_detect (он тянет `bybit` и `db`).
# Держать их в sys.modules нельзя: другие тесты в той же сессии pytest получат фейковый
# bybit_ws.state_db и упадут на импорте реального StateDB. pump_detect уже держит ссылки
# на объекты заглушек, поэтому pop для него безвреден.
for _stub_name in ('bybit_ws.api', 'bybit_ws.state_db'):
    sys.modules.pop(_stub_name, None)


class FakeBybit:
    """Мок bybit: возвращает заранее заданные ответы, запоминает аргументы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        # Снимок тела на момент вызова — важно для проверки positionIdx в ретрае.
        self.calls.append({'method': method, 'path': path, 'body': dict(body) if body else body})
        if self.responses:
            return self.responses.pop(0)
        return {'retCode': 0, 'retMsg': 'OK'}


def _err_idx():
    return {'retCode': 10001, 'retMsg': 'position idx not match position mode'}


def _ok():
    return {'retCode': 0, 'retMsg': 'OK', 'result': {'orderId': 'x'}}


def _fresh(monkeypatch):
    """Очистить кэш режима перед каждым тестом."""
    pump_detect._POS_MODE_CACHE.clear()
    monkeypatch.setattr(pump_detect, 'bybit', lambda *a, **k: _ok())


def test_default_is_one_way_on_empty_cache(monkeypatch):
    """1. На пустом кэше _position_idx возвращает дефолт 0 (one-way)."""
    _fresh(monkeypatch)
    assert pump_detect._position_idx('NEWUSDT', 'Sell') == 0
    assert pump_detect._position_idx('NEWUSDT', 'Buy') == 0


def test_one_way_first_try_ok(monkeypatch):
    """2. One-way: первый ответ retCode 0 → used_idx == 0, кэш пуст."""
    _fresh(monkeypatch)
    fake = FakeBybit([_ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'OWUSDT', 'side': 'Sell'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'OWUSDT', 'Sell')

    assert resp['retCode'] == 0
    assert used_idx == 0
    assert 'OWUSDT' not in pump_detect._POS_MODE_CACHE
    assert len(fake.calls) == 1
    assert fake.calls[0]['body']['positionIdx'] == 0


def test_hedge_sell_falls_back_to_2_and_caches(monkeypatch):
    """3. Hedge Sell: 10001 → ретрай с positionIdx 2, used_idx == 2, кэш == 2."""
    _fresh(monkeypatch)
    fake = FakeBybit([_err_idx(), _ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'HEDGEUSDT', 'side': 'Sell'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'HEDGEUSDT', 'Sell')

    assert resp['retCode'] == 0
    assert used_idx == 2
    assert pump_detect._POS_MODE_CACHE['HEDGEUSDT'] == 2
    assert len(fake.calls) == 2
    assert fake.calls[0]['body']['positionIdx'] == 0
    assert fake.calls[1]['body']['positionIdx'] == 2


def test_hedge_buy_falls_back_to_1(monkeypatch):
    """4. Hedge Buy: 10001 → ретрай с positionIdx 1, used_idx == 1, кэш == 1."""
    _fresh(monkeypatch)
    fake = FakeBybit([_err_idx(), _ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'HEDGEUSDT', 'side': 'Buy'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'HEDGEUSDT', 'Buy')

    assert resp['retCode'] == 0
    assert used_idx == 1
    assert pump_detect._POS_MODE_CACHE['HEDGEUSDT'] == 1
    assert fake.calls[1]['body']['positionIdx'] == 1


def test_cached_2_falls_back_to_0(monkeypatch):
    """5. Обратная симметрия: кэш держит 2, 10001 → ретрай с 0."""
    _fresh(monkeypatch)
    pump_detect._POS_MODE_CACHE['SYMUSDT'] = 2
    fake = FakeBybit([_err_idx(), _ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'SYMUSDT', 'side': 'Sell'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'SYMUSDT', 'Sell')

    assert resp['retCode'] == 0
    assert used_idx == 0
    assert fake.calls[0]['body']['positionIdx'] == 2
    assert fake.calls[1]['body']['positionIdx'] == 0
    assert pump_detect._POS_MODE_CACHE['SYMUSDT'] == 0


def test_both_fail_no_exception_cache_untouched(monkeypatch):
    """6. Оба ответа 10001 → возвращается ошибка, исключения нет, кэш не отравлен."""
    _fresh(monkeypatch)
    pump_detect._POS_MODE_CACHE['SYMUSDT'] = 2
    fake = FakeBybit([_err_idx(), _err_idx()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'SYMUSDT', 'side': 'Sell'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'SYMUSDT', 'Sell')

    assert resp['retCode'] == 10001
    assert used_idx == 0  # индекс последней попытки
    assert pump_detect._POS_MODE_CACHE['SYMUSDT'] == 2  # прежнее значение сохранено
    assert len(fake.calls) == 2


def test_non_idx_error_no_retry(monkeypatch):
    """7. 10001 с retMsg 'qty invalid' — не про индекс → ретрая НЕТ, один вызов."""
    _fresh(monkeypatch)
    fake = FakeBybit([{'retCode': 10001, 'retMsg': 'qty invalid'}])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'OWUSDT', 'side': 'Sell'}
    resp, used_idx = pump_detect._order_with_idx_fallback(body, 'OWUSDT', 'Sell')

    assert resp['retCode'] == 10001
    assert used_idx == 0
    assert len(fake.calls) == 1
    assert 'OWUSDT' not in pump_detect._POS_MODE_CACHE


def test_position_idx_written_into_body(monkeypatch):
    """8. _order_with_idx_fallback кладёт positionIdx в переданный body (in-place)."""
    _fresh(monkeypatch)
    fake = FakeBybit([_ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'OWUSDT', 'side': 'Sell', 'qty': '1'}
    pump_detect._order_with_idx_fallback(body, 'OWUSDT', 'Sell')

    assert 'positionIdx' in body
    assert body['positionIdx'] == 0
    assert fake.calls[0]['body']['positionIdx'] == 0


def test_explicit_idx_used_for_sl_tp(monkeypatch):
    """Доп.: явный idx (used_idx от входа) отправляется как есть, без резолва из кэша."""
    _fresh(monkeypatch)
    pump_detect._POS_MODE_CACHE['SYMUSDT'] = 0
    fake = FakeBybit([_ok()])
    monkeypatch.setattr(pump_detect, 'bybit', fake)

    body = {'symbol': 'SYMUSDT', 'stopLoss': '1.0'}
    resp, used_idx = pump_detect._order_with_idx_fallback(
        body, 'SYMUSDT', 'Sell', path='/v5/position/trading-stop', idx=2)

    assert resp['retCode'] == 0
    assert used_idx == 2
    assert fake.calls[0]['path'] == '/v5/position/trading-stop'
    assert fake.calls[0]['body']['positionIdx'] == 2
