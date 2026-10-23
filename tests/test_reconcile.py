"""reconcile.py: сплит open/close, фандинг, maker/taker, чтение БД. Без сети.

Фикстура повторяет форму живого ответа transaction-log (замер 2026-10-23):
change = cashFlow - fee, у открывающих cashFlow = 0.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, '/home/openclaw')
sys.path.insert(0, '/home/openclaw/bybit-ws')

import reconcile  # noqa: E402
from bybit_ws.state_db import StateDB  # noqa: E402


# ── фикстура: 2 символа, 3 открытия + 3 закрытия + фандинг ────────────
def tr(sym, side, qty, price, fee, cf, fee_rate, t=1_790_000_000):
    return {'type': 'TRADE', 'symbol': sym, 'side': side, 'qty': str(qty),
            'tradePrice': str(price), 'fee': str(fee), 'feeRate': str(fee_rate),
            'cashFlow': str(cf), 'change': str(round(cf - fee, 10)),
            'transactionTime': str(t * 1000)}


def st(sym, chg, rate, t=1_790_000_000):
    return {'type': 'SETTLEMENT', 'symbol': sym, 'cashFlow': '0',
            'change': str(chg), 'feeRate': str(rate),
            'transactionTime': str(t * 1000)}


RECORDS = [
    # AKEUSDT: taker 0.0011 → открытие SHORT, закрытие двумя частями (maker+taker)
    tr('AKEUSDT', 'Sell', 100, 1.0, 0.110, 0.0, 0.0011),
    tr('AKEUSDT', 'Buy', 60, 0.9, 0.054, 6.0, 0.0011),
    tr('AKEUSDT', 'Buy', 40, 0.8, 0.0144, 8.0, 0.0004),   # maker
    # BTCUSDT: taker 0.001 → закрытие LONG (open был вне окна)
    tr('BTCUSDT', 'Sell', 0.1, 60000, 6.6, 100.0, 0.001),
    # SETTLEMENT = фандинг, сумма в change
    st('AKEUSDT', -0.25, 0.0001),
    st('AKEUSDT', -0.5, 0.0001),
    st('BTCUSDT', 0.75, -0.00005),
]


def test_split_and_totals():
    a = reconcile.collect(RECORDS)
    assert a['n_trade'] == 4 and a['n_settle'] == 3
    assert a['n_open'] == 1 and a['n_close'] == 3
    assert a['fee'] == pytest.approx(0.110 + 0.054 + 0.0144 + 6.6)
    assert a['fee_open'] == pytest.approx(0.110)
    assert a['fee_close'] == pytest.approx(0.054 + 0.0144 + 6.6)
    # главное утверждение ТЗ: сплит не теряет ни цента
    assert a['fee_open'] + a['fee_close'] == pytest.approx(a['fee'])
    assert a['cashflow_trade'] == pytest.approx(6.0 + 8.0 + 100.0)
    assert a['change_trade'] == pytest.approx(0.0 - 0.110 + (6.0 - 0.054)
                                              + (8.0 - 0.0144) + (100.0 - 6.6))
    assert a['change_settle'] == pytest.approx(-0.25 - 0.5 + 0.75)
    # identity: Σ change(закрытия) = Σ pnl(closedPnl); открытие = −fee
    assert a['change_close'] == pytest.approx((6.0 - 0.054) + (8.0 - 0.0144) + (100.0 - 6.6))
    assert a['change_open'] == pytest.approx(-0.110)


def test_maker_taker_by_feerate():
    a = reconcile.collect(RECORDS)
    # AKEUSDT: два тарифа → 0.0004 = maker; BTCUSDT один тариф → неоднозначно
    assert a['maker_n'] == 1 and a['taker_n'] == 2 and a['amb_n'] == 1
    assert a['maker_fee'] == pytest.approx(0.0144)
    assert a['taker_fee'] == pytest.approx(0.110 + 0.054)   # BTC — в «неоднозначно»


def test_empty():
    a = reconcile.collect([])
    assert a['n_trade'] == 0 and a['fee'] == 0.0 and a['change_settle'] == 0.0


def test_paging_returns_every_record_once():
    """Курсор + стыки окон: ни одна запись не теряется и не дублируется."""
    base = 1_790_000_000_000
    recs = [{'id': f'r{i}', 'transactionTime': str(base + i * 700),
             'type': 'TRADE', 'symbol': 'X', 'fee': '0', 'cashFlow': '0',
             'change': '0', 'feeRate': '0.001'} for i in range(130)]
    pages = []

    def fake_fetch(method, path):
        qs = dict(kv.split('=', 1) for kv in path.split('?', 1)[1].split('&'))
        lo, hi = int(qs['startTime']), int(qs['endTime'])
        off = int(qs.get('cursor') or 0)
        hit = sorted((r for r in recs if lo <= int(r['transactionTime']) <= hi),
                     key=lambda r: -int(r['transactionTime']))
        chunk = hit[off:off + 50]
        pages.append(len(hit))
        return {'retCode': 0, 'result': {
            'list': chunk,
            'nextPageCursor': str(off + 50) if off + 50 < len(hit) else ''}}

    got = list(reconcile.iter_log(1_789_000_000, 1_790_001_000, fetch=fake_fetch))
    assert max(pages) > 50, 'должно быть несколько страниц внутри окна'
    assert sorted(r['id'] for r in got) == sorted(r['id'] for r in recs)
    assert len({r['id'] for r in got}) == 130


def test_read_db_against_temp_db_and_matching_diff():
    """БД читается read-only, символ/стратегия/manual разложены по колонкам."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'state.db')
        db = StateDB(path)
        db.add_trade(symbol='AKEUSDT', side='Buy', strategy='auto', entry_price=1.0,
                     exit_price=0.9, size=100, pnl=13.9316, fees=0.1784,
                     closed_at=1_790_000_000, manual=0)
        db.add_trade(symbol='BTCUSDT', side='Sell', strategy='auto', entry_price=1.0,
                     exit_price=2.0, size=1, pnl=93.4, fees=6.6,
                     closed_at=1_790_000_000, manual=1)
        db.conn.execute("INSERT INTO funding_log (symbol, cash_flow, fee_rate, ts, external_id)"
                        " VALUES ('AKEUSDT', -0.75, 0.0001, 1790000000, 'x1'),"
                        "        ('BTCUSDT', 0.75, -0.00005, 1790000000, 'x2')")
        db.conn.commit()
        db.close()

        before = open(path, 'rb').read()
        dbr = reconcile.read_db(1_790_000_000 - 1, 1_790_000_000 + 1, path)
        assert open(path, 'rb').read() == before, 'read_db изменила файл БД'
        assert dbr['ro']['total_changes'] == 0
        assert dbr['ro']['data_version_before'] == dbr['ro']['data_version_after']

        assert dbr['n_trade'] == 2 and dbr['n_fund'] == 2
        assert dbr['pnl'] == pytest.approx(107.3316)
        assert dbr['fees'] == pytest.approx(6.7784)
        assert dbr['funding'] == pytest.approx(0.0)
        assert dbr['manual'][0] == {'n': 1, 'pnl': pytest.approx(13.9316),
                                    'fees': pytest.approx(0.1784)}
        assert dbr['manual'][1]['n'] == 1 and dbr['manual'][1]['pnl'] == pytest.approx(93.4)

        rep = reconcile.build(30, 1_790_000_000 - 1, 1_790_000_000 + 1,
                              reconcile.collect(RECORDS), dbr)
        assert rep['diff']['fee'] == pytest.approx(
            (0.110 + 0.054 + 0.0144 + 6.6) - 6.7784)
        # identity сходится: Σ change(TRADE) = Σ pnl − комиссии открытия
        assert rep['diff']['identity'] == pytest.approx(0.0, abs=1e-9)
        text = reconcile.render(rep)
        assert 'maker 1' in text and 'state.db: read-only' in text
        assert 'по symbol' in text and 'по strategy' in text
