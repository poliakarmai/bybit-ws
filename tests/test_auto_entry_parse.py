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

import pytest  # noqa: E402


@pytest.fixture()
def ae():
    return pytest.importorskip('bybit_ws.auto_entry')


def test_bad_number_returns_empty(ae):
    """Нечисловая цена внутри строки тикера → {} без исключения."""
    assert ae._parse_ticker_line('SOLUSDT notanumber 1 2 3 4 5') == {}


def test_short_line_returns_empty(ae):
    """Слишком короткая строка → {}."""
    assert ae._parse_ticker_line('SOLUSDT 1.5') == {}


def test_corr_sizing_halves_size(ae):
    """r>0.70 → corr_sizer=0.5 реально режет маржу и количество (было: только лог)."""
    margin, qty = ae._apply_corr_sizing(100.0, 30, 0.5, 10.0, 3)
    assert margin == pytest.approx(50.0)
    assert qty == 15, f'половина от 30, получили {qty}'


def test_corr_sizing_no_op(ae):
    """Без корреляции размер не меняется."""
    assert ae._apply_corr_sizing(100.0, 30, 1.0, 10.0, 3) == (100.0, 30)


def test_corr_sizing_never_zero_qty(ae):
    """Урезанная маржа не превращается в нулевой ордер."""
    _, qty = ae._apply_corr_sizing(0.5, 2, 0.5, 10.0, 3)
    assert qty >= 1


def test_valid_line_parsed(ae):
    """Валидная строка парсится (защита от «починили и сломали»)."""
    out = ae._parse_ticker_line('SOLUSDT 145.5 1 2 3 4 1,234,567')
    assert out['symbol'] == 'SOLUSDT'
    assert out['last'] == pytest.approx(145.5)
    assert out['turnover24h'] == pytest.approx(1234567)
