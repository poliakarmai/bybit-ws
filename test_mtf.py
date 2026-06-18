"""Тесты для mtf_confirmation.py — Фаза 4.3.1

Проверяет:
- check_confluence() для LONG/SHORT с мок-данными
- _bb_signal() граничные случаи
- format_confluence() вывод
"""

import pytest
from bybit_ws.mtf_confirmation import (
    _bb_signal, check_confluence, format_confluence,
    CONFLUENCE_MIN_TFS, TF_LIST
)


class TestBBSignal:
    """Тесты определения сигнала на одном ТФ."""

    def test_long_signal_below_middle(self):
        """LONG: pos < 50 → сигнал есть."""
        bb = {'pos': 22.5, 'current': 100.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'LONG')
        assert result is not None
        assert result['signal'] is True
        assert result['pos'] == 22.5
        assert result['current'] == 100.0
        assert result['bb_lower'] == 90.0
        assert result['bb_upper'] == 120.0
        assert result['bb_middle'] == 105.0
        # distance: (100-90)/(120-90) = 10/30 = 0.333
        assert abs(result['distance_to_band'] - 0.333) < 0.01

    def test_long_no_signal_above_middle(self):
        """LONG: pos > 50 → сигнала нет."""
        bb = {'pos': 72.0, 'current': 115.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'LONG')
        assert result is not None
        assert result['signal'] is False
        assert result['pos'] == 72.0

    def test_short_signal_above_middle(self):
        """SHORT: pos > 50 → сигнал есть."""
        bb = {'pos': 85.0, 'current': 115.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'SHORT')
        assert result is not None
        assert result['signal'] is True
        assert result['pos'] == 85.0
        # distance: (120-115)/(120-90) = 5/30 = 0.167
        assert abs(result['distance_to_band'] - 0.167) < 0.01

    def test_short_no_signal_below_middle(self):
        """SHORT: pos < 50 → сигнала нет."""
        bb = {'pos': 22.5, 'current': 100.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'SHORT')
        assert result is not None
        assert result['signal'] is False

    def test_exact_middle_pos_50_long(self):
        """LONG: pos = 50 (ровно посередине) → сигнала нет (строго < 50)."""
        bb = {'pos': 50.0, 'current': 105.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'LONG')
        assert result is not None
        assert result['signal'] is False

    def test_exact_middle_pos_50_short(self):
        """SHORT: pos = 50 (ровно посередине) → сигнала нет (строго > 50)."""
        bb = {'pos': 50.0, 'current': 105.0, 'lower': 90.0, 'upper': 120.0, 'middle': 105.0}
        result = _bb_signal(bb, 'SHORT')
        assert result is not None
        assert result['signal'] is False

    def test_none_bb_returns_none(self):
        """None BB → None."""
        assert _bb_signal(None, 'LONG') is None
        assert _bb_signal(None, 'SHORT') is None

    def test_zero_bb_range(self):
        """BB range = 0 (цена не менялась) → не падает."""
        bb = {'pos': 50.0, 'current': 100.0, 'lower': 100.0, 'upper': 100.0, 'middle': 100.0}
        result = _bb_signal(bb, 'LONG')
        assert result is not None
        # distance_to_band должен быть разумным даже при нулевом диапазоне
        assert result['distance_to_band'] >= 0


class TestCheckConfluence:
    """Тесты check_confluence() с реальным API (D-ТФ минимум)."""

    def test_real_symbol_long_confluence(self):
        """Проверяем конфлюенс для реального символа (BTCUSDT — данные точно есть)."""
        result = check_confluence('BTCUSDT', 'LONG')
        # D-ТФ должен быть доступен всегда
        assert result is not None
        assert result['symbol'] == 'BTCUSDT'
        assert result['direction'] == 'LONG'
        assert 'D' in result['timeframes']
        assert result['timeframes']['D'] is not None
        # confluence должен быть 0..3
        assert 0 <= result['confluence'] <= 3
        # approved если >= CONFLUENCE_MIN_TFS
        assert result['approved'] == (result['confluence'] >= CONFLUENCE_MIN_TFS)
        # strength
        if result['confluence'] == 3:
            assert result['strength'] == 'strong'
        elif result['confluence'] == 2:
            assert result['strength'] == 'normal'
        else:
            assert result['strength'] == 'weak'

    def test_real_symbol_short_confluence(self):
        """Проверяем конфлюенс для SHORT (BTCUSDT)."""
        result = check_confluence('BTCUSDT', 'SHORT')
        assert result is not None
        assert result['direction'] == 'SHORT'
        assert 0 <= result['confluence'] <= 3
        assert result['approved'] == (result['confluence'] >= CONFLUENCE_MIN_TFS)

    def test_invalid_symbol_returns_none(self):
        """Несуществующий символ → D-ТФ недоступен → None."""
        result = check_confluence('ZZZUSDT123XYZ', 'LONG')
        assert result is None


class TestFormatConfluence:
    """Тесты форматирования."""

    def test_format_none(self):
        assert format_confluence(None) == '⛔ MTF: no data'

    def test_format_strong(self):
        conf = {
            'symbol': 'LINKUSDT',
            'direction': 'LONG',
            'timeframes': {
                'D': {'pos': 15.0, 'signal': True},
                'W': {'pos': 30.0, 'signal': True},
                'M': {'pos': 40.0, 'signal': True},
            },
            'confluence': 3,
            'confluence_tfs': ['D', 'W', 'M'],
            'approved': True,
            'strength': 'strong',
            'filter_reason': None,
        }
        result = format_confluence(conf)
        assert '🔥' in result
        assert 'LINKUSDT' in result
        assert 'LONG' in result
        assert '3/3' in result
        assert 'D+W+M' in result
        assert 'strong' in result

    def test_format_filtered(self):
        conf = {
            'symbol': 'ADAUSDT',
            'direction': 'LONG',
            'timeframes': {
                'D': {'pos': 80.0, 'signal': False},
                'W': {'pos': 20.0, 'signal': True},
                'M': {'pos': None},
            },
            'confluence': 0,
            'confluence_tfs': [],
            'approved': False,
            'strength': 'weak',
            'filter_reason': 'day=disagree(pos=80.0), month=no_data',
        }
        result = format_confluence(conf)
        assert '❌' in result
        assert 'ADAUSDT' in result
        assert '0/3' in result
        assert 'Filter' in result


class TestIntegration:
    """Интеграционные тесты: mtf_confirmation работает с реальным API."""

    def test_multiple_symbols_confluence(self):
        """Проверяем, что для нескольких топовых символов возвращаются валидные результаты."""
        symbols = ['LINKUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT']
        results = {}
        for sym in symbols:
            r = check_confluence(sym, 'LONG')
            if r is not None:
                results[sym] = r

        # Хотя бы для 50% символов должны быть данные
        assert len(results) >= len(symbols) / 2
        for sym, r in results.items():
            assert 0 <= r['confluence'] <= 3
            assert r['approved'] == (r['confluence'] >= 2)

    def test_confluence_values_are_consistent(self):
        """Для одного символа multiple вызовов должны давать консистентные результаты."""
        r1 = check_confluence('ETHUSDT', 'LONG')
        r2 = check_confluence('ETHUSDT', 'LONG')
        if r1 is not None and r2 is not None:
            # confluence может слегка плавать из-за цены, но не должен сильно скакать
            assert abs(r1['confluence'] - r2['confluence']) <= 1
