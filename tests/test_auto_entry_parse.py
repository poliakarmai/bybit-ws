"""Регрессии auto_entry: парсер строк `bybit tickers` и single-flight скана.

История:
  * except в `_parse_ticker_line` ссылался на несуществующий `sym` → NameError;
  * условие `len(parts) >= 7` не совпадало с реальным форматом CLI (ранг + `$` +
    запятые) → {} на каждой строке → turnover24h=0 → скан молча умирал.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_auto_entry_parse.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest

# Живая строка вывода `bybit tickers` (ранг, пробелы, `$`, запятые).
REAL_LINE = '  1 BTCUSDT      $73656.100000     51,607 $3,819,640,447'


@pytest.fixture()
def ae():
    return pytest.importorskip('bybit_ws.auto_entry')


def test_bad_number_returns_empty(ae):
    """Нечисловая цена внутри строки тикера → {} без исключения."""
    assert ae._parse_ticker_line('SOLUSDT notanumber 1 2 3 4 5') == {}


def test_short_line_returns_empty(ae):
    """Слишком короткая строка → {}."""
    assert ae._parse_ticker_line('SOLUSDT 1.5') == {}


def test_real_cli_line_with_rank(ae):
    """Реальный формат CLI: символ, цена и оборот извлекаются верно."""
    out = ae._parse_ticker_line(REAL_LINE)
    assert out['symbol'] == 'BTCUSDT'
    assert out['last'] == pytest.approx(73656.1)
    assert out['turnover24h'] == pytest.approx(3819640447)


def test_header_and_separator_lines_ignored(ae):
    """Шапка и разделитель таблицы CLI — не данные."""
    assert ae._parse_ticker_line('  # Symbol       Price          Vol  Turnover') == {}
    assert ae._parse_ticker_line('-' * 60) == {}


def test_line_without_rank(ae):
    """Обратная совместимость: строка без ранга парсится так же."""
    out = ae._parse_ticker_line('SOLUSDT 145.5 1 2 3 4 1,234,567')
    assert out['symbol'] == 'SOLUSDT'
    assert out['last'] == pytest.approx(145.5)
    assert out['turnover24h'] == pytest.approx(1234567)


def test_turnover_not_zero(ae):
    """Прод-баг: turnover24h=0 → скан отбрасывал все символы и молчал."""
    assert ae._parse_ticker_line(REAL_LINE)['turnover24h'] > 0


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


def test_single_flight_skips_when_busy(monkeypatch):
    """Идущий скан не должен запускаться параллельно вторым циклом."""
    ma = pytest.importorskip('bybit_ws.main_async')
    calls = []

    def fake_scan(positions):
        calls.append(positions)
        return [{'sym': 'BTCUSDT'}]

    monkeypatch.setattr(ma, 'auto_entry_scan', fake_scan)
    monkeypatch.setattr(ma, '_ENTRY_SCAN_BUSY', True)

    assert ma._auto_entry_single_flight({}) == []
    assert calls == [], 'скан не должен вызываться, пока прошлый идёт'

    # флаг снят → скан выполняется и флаг возвращается в False
    monkeypatch.setattr(ma, '_ENTRY_SCAN_BUSY', False)
    assert ma._auto_entry_single_flight({}) == [{'sym': 'BTCUSDT'}]
    assert calls == [{}]
    assert ma._ENTRY_SCAN_BUSY is False


def test_open_entry_symbols_only_live_entry_limits(ae, monkeypatch, tmp_path):
    """Гард от дублей: в множестве только живые лимитки на вход.

    Прецедент 12.11: XRPUSDT получил две лимитки (0.9069 и 0.9078) в разных
    циклах — позиции ещё нет, проверка «позиция уже есть» не срабатывала.
    """
    import json

    import bybit_ws

    snap = {
        'XRPUSDT_a': {'symbol': 'XRPUSDT', 'status': 'New', 'kind': 'LIMIT_ENTRY'},
        'SUIUSDT_b': {'symbol': 'SUIUSDT', 'status': 'PartiallyFilled', 'kind': 'OTHER'},
        'LTCUSDT_c': {'symbol': 'LTCUSDT', 'status': 'New', 'kind': 'TP'},
        'GRAMUSDT_d': {'symbol': 'GRAMUSDT', 'status': 'Filled', 'kind': 'LIMIT_ENTRY'},
    }
    p = tmp_path / 'orders.json'
    p.write_text(json.dumps(snap))
    monkeypatch.setattr(bybit_ws, 'ORDERS_SNAPSHOT', str(p))

    assert ae._open_entry_symbols() == {'XRPUSDT', 'SUIUSDT'}


def test_open_entry_symbols_missing_file_is_empty(ae, monkeypatch, tmp_path):
    """Нет снапшота — не блокируем вход (fail-open), но и не падаем."""
    import bybit_ws
    monkeypatch.setattr(bybit_ws, 'ORDERS_SNAPSHOT', str(tmp_path / 'nope.json'))
    assert ae._open_entry_symbols() == set()
