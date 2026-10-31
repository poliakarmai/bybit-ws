"""Атрибуция входов: бот или человек — по ордеру, а когда истории нет, по сигналам.

Главное правило: наши авто-входы помечены префиксом orderLinkId. Пустой link —
вход из приложения (там наша метка физически не появляется). createType для
различения НЕ годится: Bybit отдаёт CreateByUser и нашим API-ордерам.
"""

import pytest

import bybit_ws.manual_positions as mp


def test_auto_prefixes_are_auto():
    for link in ('ae_solusdt_1', 'as_xrpusdt_1', 're_dotusdt_9', 'dca_solusdt_2',
                 'bb_arbusdt_1', 'mr_tonusdt_1', 'fe_linkusdt_1', 'pd_jctusdt_123'):
        assert mp.origin_by_link(link) == 'auto', link


def test_empty_and_foreign_links_are_manual():
    assert mp.origin_by_link('') == 'manual'
    assert mp.origin_by_link(None) == 'manual'
    assert mp.origin_by_link('hbc23839586804556bf41') == 'manual'   # api.py default: /enter
    assert mp.origin_by_link('man_btcusdt_1') == 'manual'


def test_empty_link_is_manual_fact(monkeypatch):
    monkeypatch.setattr(mp, 'manual_signals', lambda *a, **k: [])
    manual, why = mp.decide_manual('DOTUSDT', 'Buy', 1_793_000_000, 75, link='')
    assert manual and 'приложени' in why[0]


def test_auto_link_wins_over_signals(monkeypatch):
    monkeypatch.setattr(mp, 'manual_signals', lambda *a, **k: ['нет анализа на входе (entry_diag пуст)'])
    manual, _ = mp.decide_manual('JCTUSDT', 'Sell', 1_793_000_000, 10, link='pd_jctusdt_1')
    assert manual is False


def test_lone_missing_analysis_is_not_enough(monkeypatch):
    """У скальпа diag тоже может не быть — одного признака мало."""
    monkeypatch.setattr(mp, 'manual_signals', lambda *a, **k: ['нет анализа на входе (entry_diag пуст)'])
    manual, _ = mp.decide_manual('BIRBUSDT', 'Sell', 1_793_000_000, 10, link='h123')
    assert manual is False


def test_off_grid_leverage_plus_signal_is_manual(monkeypatch):
    """Твои маркеры: плечо вне {3,10} и нет следов анализа/судьи."""
    monkeypatch.setattr(mp, 'manual_signals', lambda *a, **k: [
        'нет анализа на входе (entry_diag пуст)',
        'плечо 75x вне авто-набора',
    ])
    manual, why = mp.decide_manual('DOTUSDT', 'Sell', 1_793_000_000, 75, link='h123')
    assert manual and len(why) == 2


def test_real_signals_on_auto_leverage(monkeypatch):
    """Сигналы считаются по-настоящему: пустой diag и нет судьи — два признака,
    но плечо 10x авто, значит вердикт «авто»."""
    monkeypatch.setattr(mp, '_entry_diag', lambda sym, side: {})
    monkeypatch.setattr(mp, 'judge_seen', lambda *a, **k: False)
    manual, why = mp.decide_manual('JCTUSDT', 'Sell', 1_793_000_000, 10, link='h123')
    assert manual is False
    assert len(why) == 2
