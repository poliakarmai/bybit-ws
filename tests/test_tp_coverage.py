"""TASK G: покрытие TP считаем суммарно, а не по уровням BB.

Симптом (живой лог 18.10, каждые ~5 мин):
    🔎 TPDBG XLMUSDT: qty=167.0 qty_str='167' price=0.233639... idx=1
    ❌ TP ошибка XLMUSDT: orderQty will be truncated to zero.   (Bybit 110017)

qty=167 валидна (qtyStep=1), а 110017 — это «reduce-only объём урезан до нуля»,
то есть свободного объёма позиции нет. Позиция XLMUSDT 421 была уже закрыта
ордерами 85+168+168=421, но BB-ветка считала покрытие ПО УРОВНЯМ: на уровне
upper видела 85 из «нужных» 252 и просила добавить 167.

Проверяем на моках, без живых API и без боевого state.db.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_tp_coverage.py -q
"""
import os
import sys

# Не гадим в боевой events.log.
os.environ['BYBIT_LOG_SILENT'] = '1'

sys.path.insert(0, '/home/openclaw')

from bybit_ws import auto_tp  # noqa: E402

# BB из живого прогона 18.10 19:18 (cur ниже middle → LONG-ветка активна).
BB = {'lower': 0.1677498558425005, 'middle': 0.20070000000000002,
      'upper': 0.23365014415749955, 'cur': 0.1744, 'bb_pos': 10.09}


def _mkpos(sym='XLMUSDT', size=421.0, entry=0.18335202, side='Buy', idx=1):
    return {sym: {'size': size, 'entry': entry, 'side': side,
                  'positionIdx': idx, 'stopLoss': None}}


def _orders(*pairs):
    """Существующие открытые TP в форме снапшота (kind=TP, status=New)."""
    return [{'symbol': 'XLMUSDT', 'kind': 'TP', 'status': 'New',
             'qty': q, 'price': pr} for q, pr in pairs]


def _patch(monkeypatch, bb=BB, limits=(1.0, 1.0)):
    """Изолировать auto_tp: BB задаём, сеть и self-learn отключаем."""
    monkeypatch.setattr(auto_tp, 'get_bb_data', lambda *a, **k: bb)
    monkeypatch.setattr(auto_tp, '_get_atr_split', lambda *a, **k: (0.4, 0.6))
    monkeypatch.setattr(auto_tp, '_get_atr_value', lambda *a, **k: 5.0)
    monkeypatch.setattr(auto_tp, '_get_regime_tp_levels',
                        lambda: (auto_tp.ATR_TP_LEVELS, 'NEUTRAL'))
    monkeypatch.setattr(auto_tp, 'is_manual_position', lambda *a, **k: False)
    monkeypatch.setattr(auto_tp, 'is_auto_sltp', lambda *a, **k: False)
    monkeypatch.setattr(auto_tp, '_get_qty_limits', lambda *a, **k: limits)
    auto_tp.TP_PERM_SKIP.clear()


# ── Боевой случай XLMUSDT: 421 закрыто ордерами 85+168+168 ────────────────
def test_fully_covered_by_three_tps_places_nothing(monkeypatch):
    _patch(monkeypatch)
    acts = auto_tp.auto_take_profit(
        _mkpos(), _orders((85.0, 0.2334), (168.0, 0.2009), (168.0, 0.2056)))
    assert acts == [], f'позиция уже покрыта, а бот просит ещё: {acts}'


def test_single_tp_equal_to_position_places_nothing(monkeypatch):
    _patch(monkeypatch)
    assert auto_tp.auto_take_profit(_mkpos(), _orders((421.0, 0.2334))) == []


# ── Регресс: гейт не должен глушить частично покрытую позицию ─────────────
def test_partial_coverage_still_places(monkeypatch):
    _patch(monkeypatch)
    acts = auto_tp.auto_take_profit(_mkpos(), _orders((85.0, 0.2334)))
    qty = sorted(a[3] for a in acts)
    assert qty == [167.0, 168.0], f'ожидали уровни mid=168 и far=167, получили {acts}'
    assert all(a[0] == 'XLMUSDT' and a[2] == 'Buy' for a in acts)


def test_no_existing_tp_unchanged(monkeypatch):
    """Без покрытия поведение прежнее — иначе гейт сломал бы основной путь."""
    _patch(monkeypatch)
    qty = sorted(a[3] for a in auto_tp.auto_take_profit(_mkpos(), []))
    assert qty == [168.0, 252.0], qty


# ── Экономия: полностью покрытый символ не должен дёргать API ─────────────
def test_fully_covered_skips_api_calls(monkeypatch):
    calls = []
    _patch(monkeypatch)
    monkeypatch.setattr(auto_tp, 'get_bb_data',
                        lambda *a, **k: calls.append('bb') or BB)
    monkeypatch.setattr(auto_tp, '_get_qty_limits',
                        lambda *a, **k: calls.append('limits') or (1.0, 1.0))
    acts = auto_tp.auto_take_profit(
        _mkpos(), _orders((85.0, 0.2334), (168.0, 0.2009), (168.0, 0.2056)))
    assert acts == []
    assert calls == [], f'гейт стоит после запросов к API: {calls}'
