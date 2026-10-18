"""Регресс 18.10.2026: просроченные лимитки → ОДИН алерт, без отмены.

TTL-уборка (`check_expired_orders`/`apply_cancel_expired`) в живом цикле не
вызывается (звалась только из заглушки main.py), поэтому лимитки старше
GRID_TIMEOUTS жили на бирже бесконечно. Авто-отмена небезопасна: createType
не отличает ордера бота от ручных (замер 18.10: TP-лимитки бота тоже
CreateByUser) → в live-цикл подключён alert-only `report_expired_orders`.

Запуск: cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_stale_limit_orders.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

from bybit_ws import cleanup as cl  # noqa: E402


def _order(sym, created_ts, status='New'):
    return {'symbol': sym, 'orderId': sym.lower(), 'kind': 'LIMIT_ENTRY',
            'status': status, 'qty': 1.0, 'price': 1.0,
            'createdTime': str(int(created_ts * 1000))}


def test_fresh_order_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, 'ORDERS_METADATA', str(tmp_path / 'meta.json'))
    now = 1_800_000_000.0
    assert cl.report_expired_orders({'k': _order('SUSDT', now - 600)}, now) == []


def test_expired_order_alerted_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, 'ORDERS_METADATA', str(tmp_path / 'meta.json'))
    now = 1_800_000_000.0
    orders = {'k': _order('OUSDT', now - 10 * 3600)}   # 10ч > TTL 2ч

    msgs = cl.report_expired_orders(orders, now)
    assert len(msgs) == 1, msgs
    assert 'OUSDT' in msgs[0] and '10ч' in msgs[0]

    # второй вызов в том же цикле/следующем — тишина (флаг в ORDERS_METADATA)
    assert cl.report_expired_orders(orders, now) == []


def test_non_limit_entry_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, 'ORDERS_METADATA', str(tmp_path / 'meta.json'))
    now = 1_800_000_000.0
    o = _order('XUSDT', now - 99 * 3600)
    o['kind'] = 'TP'                                    # TP/SL не наша забота
    assert cl.report_expired_orders({'k': o}, now) == []


if __name__ == '__main__':
    import tempfile, pathlib
    class MP:  # мини-подстановка monkeypatch для запуска без pytest
        def __init__(s): s.dir = tempfile.mkdtemp()
        def setattr(s, obj, name, val): setattr(obj, name, val)
    for fn in (test_fresh_order_is_silent, test_expired_order_alerted_once,
               test_non_limit_entry_ignored):
        fn(pathlib.Path(MP().dir), MP())
    print('✅ stale limit orders: alert once, no cancel')
