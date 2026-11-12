"""Кеш world-модели общий на пачку, а не на символ.

Предсказание считается сразу для _WORLD_CACHE_SYMBOLS (13 шт), поэтому символ
вне списка не кешировался никогда и каждый вызов пересчитывал всю пачку через
torch. Замер: 40с из 120с heavy cycle.
"""
import bybit_ws.lstm_world_model as wm


def test_symbol_outside_batch_does_not_recompute(monkeypatch):
    calls = []
    monkeypatch.setattr(wm, '_world_cache', {'BTCUSDT': {'regime': 'RANGE', 'predicted_return_pct': 0.1}})
    monkeypatch.setattr(wm, '_world_cache_ts', {'BTCUSDT': wm.time.time()})
    monkeypatch.setattr(wm, '_ensure_world_model',
                        lambda: calls.append(1) or (_ for _ in ()).throw(AssertionError('пересчёт!')))

    # Символа нет в пачке → отдаём None, но НЕ пересчитываем всё заново
    assert wm.get_cached_world_prediction('NOTINBATCHUSDT') is None
    assert calls == []
    # Символ из пачки — из кеша
    assert wm.get_cached_world_prediction('BTCUSDT')['regime'] == 'RANGE'


def test_stale_batch_recomputes(monkeypatch):
    monkeypatch.setattr(wm, '_world_cache', {'BTCUSDT': {'regime': 'OLD'}})
    monkeypatch.setattr(wm, '_world_cache_ts', {'BTCUSDT': wm.time.time() - wm._CACHE_TTL - 10})
    monkeypatch.setattr(wm, '_ensure_world_model',
                        lambda: (_ for _ in ()).throw(RuntimeError('нет модели')))

    # Протухло → идём пересчитывать (здесь падаем на модели и возвращаем None)
    assert wm.get_cached_world_prediction('BTCUSDT') is None
