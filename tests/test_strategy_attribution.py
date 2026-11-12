"""Атрибуция механизма входа в trade_history.strategy (12.11.2026).

Было: `strategy='auto'` константой при импорте закрытых PnL с биржи — 203
auto-сделки за 30 дней под одним ярлыком, разрез по механизмам невозможен.
Стало: имя берётся из префикса orderLinkId, которым открыта позиция.
"""
import pytest

from bybit_ws.manual_positions import (
    AUTO_LINK_PREFIXES,
    STRATEGY_BY_PREFIX,
    strategy_by_link,
)

# Контракт один: два списка префиксов обязаны совпадать в ОБЕ стороны.
# Если кто-то добавит префикс только в один — тест падает здесь, а не в проде.
def test_prefix_tables_are_equal():
    assert set(STRATEGY_BY_PREFIX) == set(AUTO_LINK_PREFIXES)


@pytest.mark.parametrize('link,expected', [
    ('ae_btcusdt_1794488196', 'auto_entry'),
    ('as_ethusdt_1794488196', 'auto_short'),
    ('re_solusdt_1794488196', 'reentry'),
    ('dca_xrpusdt_1', 'dca'),
    ('bb_adausdt_1', 'scalp'),
    ('mr_dogeusdt_1', 'mean_revert'),
    ('fe_linkusdt_1', 'funding'),
    ('pd_qusdt_1', 'pump'),
])
def test_known_prefixes(link, expected):
    assert strategy_by_link(link) == expected


@pytest.mark.parametrize('link', ['', None, 'fff6079d-6a05-41dc-8310-f1e7c8023b99',
                                  'h1794488196', 'AE_UPPER'])
def test_unknown_links(link):
    """Нет нашего префикса — 'unknown', а не выдуманное имя.

    Регистр важен: Bybit отдаёт link как есть, 'AE_' наш код не ставит.
    """
    assert strategy_by_link(link) == 'unknown'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
