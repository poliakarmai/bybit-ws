"""Прогрев kline-кеша после WS-подписки.

Bybit kline-стрим отдаёт снапшот из одной свечи, а BB нужен 21 → без прогрева
_bb_cache пуст и _get_bb_ws() навсегда уходит в REST.
"""
import pytest

from bybit_ws import ws_client as w


@pytest.fixture()
def clean_cache():
    klines, bbs = dict(w._kline_cache), dict(w._bb_cache)
    w._kline_cache.clear()
    w._bb_cache.clear()
    yield
    w._kline_cache.clear()
    w._kline_cache.update(klines)
    w._bb_cache.clear()
    w._bb_cache.update(bbs)


def _fake_bybit(n=25):
    """n дневных свечей, как отдаёт Bybit: новые первыми."""
    rows = [[str(1000 * i), '1', '2', '0.5', str(10 + i), '5'] for i in range(n)]
    rows.reverse()

    def fake(method, path, **kw):
        return {'retCode': 0, 'result': {'list': rows}}

    return fake


def test_warmup_fills_bb_cache(clean_cache, monkeypatch):
    monkeypatch.setattr('bybit_ws.api.bybit', _fake_bybit())
    w._warmup_klines(['FOOUSDT'])

    assert 'FOOUSDT' in w._kline_cache
    assert len(w._kline_cache['FOOUSDT']['D']) == 25
    bb = w._bb_cache['FOOUSDT']['D']
    # closes 10..34, последняя свеча = 34 → позиция у верхней полосы
    assert bb['bb_pos'] > 90
    assert bb['lower'] < bb['middle'] < bb['upper']


def test_warmup_survives_bad_response(clean_cache, monkeypatch):
    """Ошибка API по символу не валит прогрев остальных."""
    rows = [[str(1000 * i), '1', '2', '0.5', str(10 + i), '5'] for i in range(25)]

    def fake(method, path, **kw):
        if 'BADUSDT' in path:
            raise RuntimeError('boom')
        return {'retCode': 0, 'result': {'list': rows}}

    monkeypatch.setattr('bybit_ws.api.bybit', fake)
    w._warmup_klines(['BADUSDT', 'FOOUSDT'])
    assert 'BADUSDT' not in w._bb_cache
    assert 'FOOUSDT' in w._bb_cache
