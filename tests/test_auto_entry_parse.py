"""Регрессия находки 1 ревью xpiki: except в `_parse_ticker_line` ссылался на
несуществующий `sym` → NameError вместо `return {}` (и исключение вылетало из
скоринга символа, обрывая проход по watchlist).

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_auto_entry_parse.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest


@pytest.fixture()
def ae():
    return pytest.importorskip('bybit_ws.auto_entry')


def test_bad_number_returns_empty(ae):
    """Нечисловая цена внутри строки тикера → {} без исключения."""
    assert ae._parse_ticker_line('SOLUSDT notanumber 1 2 3 4 5') == {}


def test_short_line_returns_empty(ae):
    """Слишком короткая строка → {}."""
    assert ae._parse_ticker_line('SOLUSDT 1.5') == {}


def test_corr_sizing_halves_size(ae, monkeypatch):
    """r>0.70 → corr_sizer=0.5 реально режет маржу и количество (было: только лог)."""
    from bybit_ws import auto_tp
    monkeypatch.setattr(auto_tp, "_get_qty_limits", lambda s: (1.0, 1.0))
    margin, qty = ae._apply_corr_sizing(100.0, 30, 0.5, 10.0, 3, "SOLUSDT")
    assert margin == pytest.approx(50.0)
    assert qty == 15, f'половина от 30, получили {qty}'


def test_corr_sizing_no_op(ae):
    """Без корреляции размер не меняется."""
    assert ae._apply_corr_sizing(100.0, 30, 1.0, 10.0, 3, "SOLUSDT") == (100.0, 30)


def test_corr_sizing_never_rounds_up_to_one_coin(ae, monkeypatch):
    """Урезанная маржа не превращается в «1 целую монету» (прод-баг: 1 BTC на $5)."""
    from bybit_ws import auto_tp
    monkeypatch.setattr(auto_tp, "_get_qty_limits", lambda s: (0.001, 0.001))
    _, qty = ae._apply_corr_sizing(0.5, 2, 0.5, 74385.0, 3, "BTCUSDT")
    assert qty is None  # ниже minOrderQty → вход пропускается, а не 1 BTC


def test_valid_line_parsed(ae):
    """Валидная строка парсится (защита от «починили и сломали»)."""
    out = ae._parse_ticker_line('SOLUSDT 145.5 1 2 3 4 1,234,567')
    assert out['symbol'] == 'SOLUSDT'
    assert out['last'] == pytest.approx(145.5)
    assert out['turnover24h'] == pytest.approx(1234567)
