"""Регресс на роутинг /rpc/dash/*: после перевода в приватные они отдавали 404.

26.10: публичный блок в do_GET сняли, но маршруты /rpc/dash/{all,risk,signals}
остались только внутри снятого блока → 404 даже с валидным токеном.
Живой потребитель: крон morning-digest (/rpc/dash/risk).
"""
import inspect

from bybit_ws import rpc


def test_dash_routes_exist_in_dispatcher():
    src = inspect.getsource(rpc.RPCHandler.do_GET)
    for path, handler in (
        ('/rpc/dash/all', '_handle_all'),
        ('/rpc/dash/risk', '_handle_risk'),
        ('/rpc/dash/signals', '_handle_signals'),
    ):
        assert f'"{path}"' in src, f'{path} не маршрутизируется'
        assert handler in src


def test_dash_paths_not_public():
    """Публичным может быть только /health."""
    src = inspect.getsource(rpc.RPCHandler.do_GET)
    public_block = src.split('if not self._check_auth()')[0]
    assert '"/health"' in public_block
    for p in ('/rpc/dash/', '/rpc/paths', '/dashboard'):
        assert p not in public_block, f'{p} остался публичным'
