"""TASK E: auto-TP и реальные лимиты символа (qtyStep / minOrderQty).

Симптом: Bybit «orderQty will be truncated to zero» → TP не ставился (XLMUSDT
qtyStep=1, QUSDT qtyStep=10), позиция шла к закрытию без тейка.

Проверяем fail-closed и пороги из instruments-info на моках, без живых API и
без боевого state.db (мокаем _get_qty_limits / _get_lot_step и place_take_profit).

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_tp_min_qty.py -q
"""
import os
import sys

# Не гадим в боевой events.log (как в test_smoke.py / test_risk_failclosed.py).
os.environ['BYBIT_LOG_SILENT'] = '1'

# bybit_ws -> bybit-ws symlink в /home/openclaw.
sys.path.insert(0, '/home/openclaw')

from bybit_ws import auto_tp  # noqa: E402


def _mkpos(sym, size, entry=100.0, side='Buy', idx=0):
    """Минимальная позиция для auto_take_profit."""
    return {sym: {'size': size, 'entry': entry, 'side': side,
                  'positionIdx': idx, 'stopLoss': None}}


def _patch_common(monkeypatch, qty_limits):
    """Изолировать auto_tp: без сети, без BB, без self-learn-файлов."""
    monkeypatch.setattr(auto_tp, 'get_bb_data', lambda *a, **k: None)
    monkeypatch.setattr(auto_tp, '_get_atr_value', lambda *a, **k: 5.0)
    monkeypatch.setattr(auto_tp, '_get_regime_tp_levels',
                        lambda: (auto_tp.ATR_TP_LEVELS, 'NEUTRAL'))
    monkeypatch.setattr(auto_tp, 'is_manual_position', lambda *a, **k: False)
    monkeypatch.setattr(auto_tp, 'is_auto_sltp', lambda *a, **k: False)
    monkeypatch.setattr(auto_tp, '_get_qty_limits', qty_limits)
    # self-learn (tp_mult) импортируется лениво внутри ATR-ветки и влияет только на
    # цену TP, не на qty/число ордеров. Если bybit_ws.journal недоступен — код сам
    # ловит ImportError и берёт tp_mult=1.0, поэтому отдельные заглушки не нужны.
    # Чистый PERM_SKIP, чтобы символ не выпал из обработки.
    auto_tp.TP_PERM_SKIP.clear()


def _spy_place_tp(monkeypatch):
    """Мок place_take_profit: возвращает True и считает вызовы."""
    calls = []

    def _fake(symbol, positionIdx, side, qty, tp_price):
        calls.append((symbol, positionIdx, side, qty, tp_price))
        return True

    monkeypatch.setattr('bybit_ws.api.place_take_profit', _fake)
    return calls


def test_qty_step_10_min_10_uncovered_20_one_action(monkeypatch):
    """1. step=10/min=10, uncovered=20 → один action на весь uncovered (20)."""
    _patch_common(monkeypatch, lambda sym: (10.0, 10.0))
    calls = _spy_place_tp(monkeypatch)

    actions = auto_tp.auto_take_profit(_mkpos('XLMTESTUSDT', 20), {})

    assert len(actions) == 1, f'ожидался 1 action, got {actions}'
    assert actions[0][3] == 20.0, f'qty всего uncovered, got {actions[0][3]}'

    auto_tp.apply_auto_tp(actions)
    assert len(calls) == 1, f'ожидался 1 ордер, got {calls}'
    assert calls[0][3] == 20.0


def test_qty_step_1_min_1_uncovered_2_one_action(monkeypatch):
    """2. step=1/min=1, uncovered=2 → куски 0.8/0.7/0.5 < 1 → один action на 2."""
    _patch_common(monkeypatch, lambda sym: (1.0, 1.0))
    calls = _spy_place_tp(monkeypatch)

    actions = auto_tp.auto_take_profit(_mkpos('TINYTESTUSDT', 2), {})

    assert len(actions) == 1, f'ожидался 1 action, got {actions}'
    assert actions[0][3] == 2.0

    auto_tp.apply_auto_tp(actions)
    assert len(calls) == 1
    assert calls[0][3] == 2.0


def test_limits_exception_is_fail_closed(monkeypatch):
    """3. _get_qty_limits бросает → place_take_profit не вызван, исключение не летит."""
    def _boom(sym):
        raise RuntimeError('instruments-info down')

    _patch_common(monkeypatch, _boom)
    calls = _spy_place_tp(monkeypatch)

    # auto_take_profit: лимиты неизвестны → ордер не формируем (не падаем).
    actions = auto_tp.auto_take_profit(_mkpos('BOOMTESTUSDT', 100), {})
    assert actions == [], f'без лимитов action быть не должно, got {actions}'

    # apply_auto_tp: тот же fail-closed на денежном пути — hand-made action тоже скипается.
    auto_tp.apply_auto_tp([('BOOMTESTUSDT', 0, 'Buy', 100.0, 110.0, 100.0)])

    assert calls == [], f'place_take_profit не должен вызываться, got {calls}'


def test_qty_below_real_step_not_sent(monkeypatch):
    """4. qty=5 при шаге/минимуме 10 → ордер не отправлен, TP_FAIL_COUNT не растёт."""
    _patch_common(monkeypatch, lambda sym: (10.0, 10.0))
    calls = _spy_place_tp(monkeypatch)

    auto_tp.TP_FAIL_COUNT.pop('STEPSKIPUSDT', None)
    auto_tp.apply_auto_tp([('STEPSKIPUSDT', 0, 'Buy', 5.0, 110.0, 5.0)])

    assert calls == [], f'усечение в 0 — ордер не отправляем, got {calls}'
    assert 'STEPSKIPUSDT' not in auto_tp.TP_FAIL_COUNT, 'TP_FAIL_COUNT не должен расти'


def test_qty_step_10_min_10_uncovered_100_three_orders(monkeypatch):
    """5. Регресс: uncovered=100, step=10/min=10 → три уровня, три ордера (как было)."""
    _patch_common(monkeypatch, lambda sym: (10.0, 10.0))
    calls = _spy_place_tp(monkeypatch)

    actions = auto_tp.auto_take_profit(_mkpos('BIGTESTUSDT', 100), {})

    # Сплит 0.40/0.35/0.25 с floor до шага 10: 40/30/20 — все ≥ min 10.
    assert len(actions) == 3, f'ожидались 3 уровня, got {actions}'
    assert all(a[3] >= 10.0 for a in actions), f'все куски ≥ min, got {[a[3] for a in actions]}'

    auto_tp.apply_auto_tp(actions)
    assert len(calls) == 3, f'ожидались 3 ордера, got {calls}'
