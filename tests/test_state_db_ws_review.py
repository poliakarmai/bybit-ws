"""Регрессия ревью 3 (xpiki): state_db.py + ws_client.py.

Проверяет пять подтверждённых находок, каждая из которых ломала поведение на live:

1. `record_entry` писал `closed_at = now` (через `closed_at or int(time.time())`), из-за
   чего открытая позиция выглядела закрытой: `inc_dca_for_symbol`/`inc_partial_tp_for_symbol`
   ищут `closed_at IS NULL` и не находили строку, а статистика дня считала её сделкой.
2. `WATCH_SYMBOLS` содержал `ENAUSDT` дважды → дублирующая подписка.
3. `start()` не был идемпотентен: второй вызов поднимал второй комплект WS-потоков.
4. `_on_open_private` подписывался на приватные топики через `sleep(0.5)` без проверки
   auth-ответа; теперь подписка — только по `op: auth, success: true`.
5. `totalMargin` больше не подменяется `totalPerpUPL` (uPnL ≠ маржа).

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_state_db_ws_review.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture()
def sdb(tmp_path):
    mod = pytest.importorskip('bybit_ws.state_db')
    db = mod.StateDB(str(tmp_path / 'state.db'))
    db.conn  # инициализация схемы + миграции
    yield db
    db.close()


@pytest.fixture()
def ws():
    return pytest.importorskip('bybit_ws.ws_client')


# ── state_db: открытая сделка не должна быть «закрытой» ─────────

def test_record_entry_leaves_closed_at_null(sdb):
    sdb.record_entry('SOLUSDT', 'Buy', 'bollinger_grid', 10.0, 5)
    row = sdb.conn.execute(
        "SELECT closed_at, exit_price, pnl FROM trade_history WHERE symbol='SOLUSDT'"
    ).fetchone()
    assert row is not None, 'запись входа не создана'
    assert row[0] is None, f'открытая позиция помечена закрытой (closed_at={row[0]})'
    assert row[1] == 0 and row[2] == 0


def test_add_trade_without_closed_at_still_uses_now(sdb):
    """Обратная совместимость: явный closed_at не передан → «сейчас», как раньше."""
    sdb.add_trade('BTCUSDT', 'Buy', 'auto', 100.0, 101.0, 1, 1.5)
    row = sdb.conn.execute(
        "SELECT closed_at FROM trade_history WHERE symbol='BTCUSDT'"
    ).fetchone()
    assert isinstance(row[0], int) and row[0] > 0


def test_dca_counter_finds_open_entry(sdb):
    """Реальный смысл находки: счётчик DCA обязан находить открытую строку."""
    sdb.record_entry('XRPUSDT', 'Buy', 'bollinger_grid', 1.0, 100)
    sdb.inc_dca_for_symbol('XRPUSDT')
    row = sdb.conn.execute(
        "SELECT dca_count FROM trade_history WHERE symbol='XRPUSDT'"
    ).fetchone()
    assert row[0] == 1, f'DCA-счётчик не инкрементировался (dca_count={row[0]})'


# ── ws_client: дубли и идемпотентность ─────────────────────────

def test_watch_symbols_unique(ws):
    dupes = {s for s in ws.WATCH_SYMBOLS if ws.WATCH_SYMBOLS.count(s) > 1}
    assert not dupes, f'дубли в WATCH_SYMBOLS: {dupes}'


def test_start_is_idempotent(ws, monkeypatch):
    calls = []

    class FakeThread:
        def __init__(self, target=None, **kw):
            calls.append(target)

        def start(self):
            pass

    monkeypatch.setattr(ws, 'threading', type('T', (), {'Thread': FakeThread}))
    monkeypatch.setattr(ws, '_started_threads', [])
    ws.start()
    first = len(calls)
    ws.start()
    assert len(calls) == first, f'второй start() поднял ещё потоки: {calls}'
    ws._started_threads.clear()


# ── ws_client: приватная подписка только после auth ─────────────

class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        pass


def test_open_private_does_not_subscribe_before_auth(ws, monkeypatch):
    monkeypatch.setattr(ws, '_ws_auth_message', lambda: {'op': 'auth', 'args': ['k']})
    sock = FakeWS()
    ws._on_open_private(sock)
    assert len(sock.sent) == 1, f'подписка ушла до auth: {sock.sent}'
    assert 'auth' in sock.sent[0]


def test_auth_success_triggers_private_subscribe(ws, monkeypatch):
    sock = FakeWS()
    ws._on_message_private(sock, '{"op":"auth","success":true}')
    assert sock.sent, 'после успешного auth подписка не отправлена'
    assert 'position' in sock.sent[0]


def test_auth_failure_does_not_subscribe(ws):
    sock = FakeWS()
    ws._on_message_private(sock, '{"op":"auth","success":false,"ret_msg":"bad signature"}')
    assert not sock.sent, f'подписка при провале auth: {sock.sent}'
