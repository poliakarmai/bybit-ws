"""Тесты единого источника символов (bybit_ws/universe.py).

Данные изолированы: DATA_DIR и оба снапшота подменяются на tmp_path,
banned — на фиксированное множество. Файлы не пишутся в реальный DATA_DIR.
"""
import pytest

from bybit_ws import universe


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Пустые снапшоты + пустой banned в tmp_path."""
    monkeypatch.setattr(universe, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(universe, 'POSITIONS_SNAPSHOT', str(tmp_path / 'positions.json'))
    monkeypatch.setattr(universe, 'ORDERS_SNAPSHOT', str(tmp_path / 'orders.json'))
    monkeypatch.setattr(universe, '_banned', set)
    return tmp_path


def test_scan_universe_order_and_dedup(env):
    """Порядок: база → custom; дубли (в т.ч. символ из базы) убраны, не-USDT строки отброшены."""
    (env / 'watchlist_custom.txt').write_text('FOOUSDT\nBTCUSDT\nFOOUSDT\nnot_a_symbol\n')
    base = list(universe.AUTO_ENTRY_WATCH)
    syms = universe.scan_universe()
    assert syms[:len(base)] == base
    assert syms[len(base):] == ['FOOUSDT']
    assert len(syms) == len(set(syms))


def test_scan_universe_banned_excluded(env, monkeypatch):
    (env / 'watchlist_custom.txt').write_text('FOOUSDT\n')
    monkeypatch.setattr(universe, '_banned', lambda: {'FOOUSDT', 'BTCUSDT'})
    syms = universe.scan_universe()
    assert 'FOOUSDT' not in syms
    assert 'BTCUSDT' not in syms
    assert 'ETHUSDT' in syms


def test_banned_fail_open(monkeypatch):
    """Битый/отсутствующий конфиг не должен валить скан."""
    def boom():
        raise RuntimeError('no config')

    monkeypatch.setattr(universe, 'Config', boom)
    assert universe._banned() == set()


def test_watch_symbols_adds_positions_and_orders(env):
    # positions.json: ключ = символ, поля 'symbol' в значении нет
    (env / 'positions.json').write_text('{"XYZUSDT": {"size": 1.5}}')
    # orders.json: ключ = SYMBOL_uuid, символ в поле 'symbol'
    (env / 'orders.json').write_text('{"QWEUSDT_uuid-1": {"symbol": "QWEUSDT"}}')

    watch = universe.watch_symbols()
    assert set(universe.scan_universe()) <= set(watch)
    assert set(universe.LIQUID_CORE) <= set(watch)
    assert 'XYZUSDT' in watch
    assert 'QWEUSDT' in watch
    assert len(watch) == len(set(watch))


def test_watch_symbols_without_snapshots(env):
    """Нет файлов снапшотов → scan_universe() ∪ LIQUID_CORE, без падений."""
    expected = list(dict.fromkeys(universe.scan_universe() + universe.LIQUID_CORE))
    assert universe.watch_symbols() == expected
