"""Регрессия: throttle SL в unified_sl.

Контракт:

    SL_THROTTLE = 120
    SL_ERROR_BACKOFF = 15
    def _throttled(sym: str) -> bool      # только читает, слот НЕ занимает
    def _mark(sym: str, delay: int = SL_THROTTLE) -> None

Раньше `_throttled()` сам записывал `_last_sl_update[sym] = now` (занимал слот),
поэтому ошибка API (`place_stop_loss` → None) блокировала повтор на полные
SL_THROTTLE=120с. Теперь слот занимает только `_mark()`, а на ошибку ставится
короткий backoff SL_ERROR_BACKOFF=15с.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_sl_throttle.py -q
"""
import os
import sys
import time

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')

import pytest  # noqa: E402


@pytest.fixture()
def usl():
    """Модуль bybit_ws.unified_sl с чистым состоянием throttle."""
    mod = pytest.importorskip('bybit_ws.unified_sl')
    # Чистим состояние, чтобы порядок тестов не влиял.
    for name in ('_last_sl_update', '_next_sl_update'):
        store = getattr(mod, name, None)
        if isinstance(store, dict):
            store.clear()
    yield mod
    for name in ('_last_sl_update', '_next_sl_update'):
        store = getattr(mod, name, None)
        if isinstance(store, dict):
            store.clear()


# ── Константы ──────────────────────────────────────────────

def test_constants(usl):
    assert usl.SL_THROTTLE == 120, usl.SL_THROTTLE
    assert usl.SL_ERROR_BACKOFF == 15, usl.SL_ERROR_BACKOFF


# ── _throttled / _mark ─────────────────────────────────────

def test_throttled_does_not_take_slot(usl):
    """_throttled только читает: два вызова подряд оба True."""
    assert hasattr(usl, '_throttled'), 'unified_sl._throttled отсутствует'
    assert hasattr(usl, '_mark'), 'unified_sl._mark отсутствует'

    assert usl._throttled('AAA') is True, 'первый вызов должен быть разрешён'
    assert usl._throttled('AAA') is True, (
        '_throttled не должен занимать слот — второй вызов обязан быть True'
    )
    # слот так и не занят: _mark(-1) освобождает слот, значит после него
    # _throttled обязан быть True (если бы _throttled занимал слот — был бы False)
    usl._mark('AAA', -1)
    assert usl._throttled('AAA') is True, '_throttled занимает слот (побочный эффект)'


def test_throttled_does_not_take_slot_via_next_update(usl):
    """_throttled не пишет в _next_sl_update (слот занимает только _mark)."""
    next_update = getattr(usl, '_next_sl_update', None)
    assert isinstance(next_update, dict), 'unified_sl._next_sl_update отсутствует'

    assert usl._throttled('CCC') is True
    assert usl._throttled('CCC') is True
    assert 'CCC' not in next_update, f'_throttled занял слот: {next_update}'


def test_mark_sets_next_update(usl):
    """_mark('DDD') занимает слот в _next_sl_update на ~SL_THROTTLE."""
    next_update = getattr(usl, '_next_sl_update', None)
    assert isinstance(next_update, dict), 'unified_sl._next_sl_update отсутствует'

    usl._mark('DDD')
    assert 'DDD' in next_update, next_update
    delay = next_update['DDD'] - time.time()
    assert abs(delay - usl.SL_THROTTLE) <= 2, delay


def test_mark_blocks_then_negative_delay_releases(usl):
    """_mark('AAA') → False; _mark('AAA', -1) → True снова."""
    assert usl._throttled('AAA') is True

    usl._mark('AAA')
    assert usl._throttled('AAA') is False, 'после _mark слот должен быть занят'

    usl._mark('AAA', -1)
    assert usl._throttled('AAA') is True, 'после _mark(-1) слот должен освободиться'


def test_mark_default_delay_is_throttle(usl):
    """_mark без аргумента ставит паузу ~SL_THROTTLE (проверка через _throttled)."""
    usl._mark('BBB')
    assert usl._throttled('BBB') is False, '_mark без аргумента не занял слот'
    usl._mark('BBB', -1)
    assert usl._throttled('BBB') is True


# ── manage_sl: ошибка API → короткий backoff ───────────────

def test_place_stop_loss_none_sets_error_backoff(usl, monkeypatch):
    """place_stop_loss → None: backoff SL_ERROR_BACKOFF, а не SL_THROTTLE."""
    calls = []

    def fake_place_stop_loss(sym, idx, side, size, price):
        calls.append((sym, idx, side, size, price))
        return None  # API error

    monkeypatch.setattr(usl, 'place_stop_loss', fake_place_stop_loss)

    sym = 'AAAUSDT'
    position = {
        'entry': 100.0, 'mark': 106.0, 'side': 'Buy', 'positionIdx': 0,
        'stopLoss': 99.0, 'size': 10.0, 'leverage': 3,
    }

    alerts = usl.manage_sl({sym: position}, cycle=0)

    assert calls, 'place_stop_loss не был вызван — SL-таргет не вычислился'
    assert isinstance(alerts, list), alerts

    next_update = getattr(usl, '_next_sl_update', None)
    assert isinstance(next_update, dict), 'unified_sl._next_sl_update отсутствует'
    assert sym in next_update, f'backoff не выставлен: {next_update}'

    delay = next_update[sym] - time.time()
    assert delay <= usl.SL_ERROR_BACKOFF + 1, (
        f'после ошибки API ожидали backoff <= {usl.SL_ERROR_BACKOFF}с, получили {delay:.1f}с '
        f'(похоже, ставится SL_THROTTLE={usl.SL_THROTTLE})'
    )
    assert delay > 0, f'backoff уже истёк: {delay:.1f}с'


def test_place_stop_loss_ok_sets_throttle(usl, monkeypatch):
    """Успешная постановка SL → обычный throttle SL_THROTTLE."""
    monkeypatch.setattr(usl, 'place_stop_loss', lambda *a, **k: {'retCode': 0})

    sym = 'BBBUSDT'
    position = {
        'entry': 100.0, 'mark': 106.0, 'side': 'Buy', 'positionIdx': 0,
        'stopLoss': 99.0, 'size': 10.0, 'leverage': 3,
    }

    usl.manage_sl({sym: position}, cycle=0)

    next_update = getattr(usl, '_next_sl_update', None)
    assert isinstance(next_update, dict), 'unified_sl._next_sl_update отсутствует'
    assert sym in next_update, f'слот throttle не занят: {next_update}'

    delay = next_update[sym] - time.time()
    assert delay > usl.SL_ERROR_BACKOFF + 1, (
        f'при успехе ожидали throttle ~{usl.SL_THROTTLE}с, получили {delay:.1f}с'
    )
    assert delay <= usl.SL_THROTTLE + 1, delay
