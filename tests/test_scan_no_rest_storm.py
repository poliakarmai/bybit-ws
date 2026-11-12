"""Скан без REST-шторма: klines и funding берутся из кеша/bulk.

46 символов × отдельный REST = ~30с латентности на цикл. Проверяем, что
_count_down_days читает WS-кеш, а _funding_rate делает ОДИН запрос на всех.
"""
from bybit_ws import auto_entry as ae


def _rows(closes):
    """Свечи как отдаёт Bybit: новые первыми, [ts,o,h,l,c,v]."""
    return [[str(1000 * i), '1', '1', '1', str(c), '1']
            for i, c in enumerate(closes)][::-1]


def test_count_down_days_from_ws_cache(monkeypatch):
    """WS-кеш есть → REST не вызывается вообще."""
    calls = []

    def no_rest(*a, **kw):
        calls.append(a)
        raise AssertionError('REST не должен вызываться при живом кеше')

    # 3 дня падения подряд: 10 → 9 → 8 → 7
    monkeypatch.setattr(ae, '_ws_kline',
                        lambda sym, interval='D': [[i, 1, 1, 1, c, 1]
                                                   for i, c in enumerate([10, 9, 8, 7])])
    monkeypatch.setattr(ae, 'bybit', no_rest)
    assert ae._count_down_days('FOOUSDT') == 3
    assert calls == []


def test_count_down_days_rest_fallback(monkeypatch):
    """Кеша нет → падаем в REST, результат тот же."""
    monkeypatch.setattr(ae, 'bybit',
                        lambda *a, **kw: {'retCode': 0,
                                          'result': {'list': _rows([10, 9, 8, 7])}})
    assert ae._count_down_days('NOCACHEUSDT') == 3


def test_calc_rsi_from_ws_cache(monkeypatch):
    """RSI считается из WS-кеша и совпадает с REST-формулой."""
    # 15 свечей: 14 подряд растущих на 1 → RSI = 100
    up = [[i, 1, 1, 1, 100 + i, 1] for i in range(15)]
    monkeypatch.setattr(ae, '_ws_kline', lambda sym, interval='D': up)
    monkeypatch.setattr(ae, 'bybit', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('REST!')))
    assert ae._calc_rsi('FOOUSDT') == 100.0


def test_funding_rate_single_bulk_request(monkeypatch):
    """Один запрос на весь скан: 3 символа → 1 вызов API."""
    calls = []

    def fake(method, path, **kw):
        calls.append(path)
        return {'retCode': 0, 'result': {'list': [
            {'symbol': 'AAAUSDT', 'fundingRate': '0.00003'},
            {'symbol': 'BBBUSDT', 'fundingRate': '-0.00015'},
            {'symbol': 'CCCUSDT', 'fundingRate': ''},
        ]}}

    monkeypatch.setattr(ae, 'bybit', fake)
    monkeypatch.setattr(ae, '_FUNDING_CACHE', {})

    assert ae._funding_rate('AAAUSDT') == 0.00003
    assert ae._funding_rate('BBBUSDT') == -0.00015
    assert ae._funding_rate('CCCUSDT') == 0.0   # пустая строка → 0, не падаем
    assert ae._funding_rate('ZZZUSDT') is None  # нет символа → default score
    assert len(calls) == 1
