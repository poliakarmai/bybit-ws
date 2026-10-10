"""Tests for volatility_filter module.

Run: cd /home/openclaw/bybit-ws && python3 -m pytest test_volatility_filter.py -q
"""
import sys
from unittest.mock import patch

# Ensure bybit_ws package is importable (symlink bybit_ws -> . in parent dir)
sys.path.insert(0, '/home/openclaw')


class TestIsHighVolatilityDisabled:
    def test_returns_disabled_tuple(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value={'enabled': False}):
            from bybit_ws.volatility_filter import is_high_volatility
            blocked, ratio, reason = is_high_volatility('BTCUSDT')
            assert blocked is False
            assert ratio == 0.0
            assert reason == 'disabled'


class TestVolatilityScaleDisabled:
    def test_returns_one_when_disabled(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value={'enabled': False}):
            from bybit_ws.volatility_filter import volatility_scale
            assert volatility_scale('BTCUSDT') == 1.0


class TestIsHighVolatilityEnabled:
    CFG = {
        'enabled': True,
        'atr_ratio_threshold': 2.0,
        'baseline_days': 20,
        'min_scale': 0.5,
    }

    def test_blocks_when_ratio_above_threshold(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=1000.0), \
             patch('bybit_ws.volatility_filter._baseline_atr', return_value=400.0):
            from bybit_ws.volatility_filter import is_high_volatility
            blocked, ratio, reason = is_high_volatility('SOLUSDT')
            assert blocked is True
            assert ratio == 2.5
            assert 'BLOCKED' in reason

    def test_passes_when_ratio_below_threshold(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=600.0), \
             patch('bybit_ws.volatility_filter._baseline_atr', return_value=400.0):
            from bybit_ws.volatility_filter import is_high_volatility
            blocked, ratio, reason = is_high_volatility('SOLUSDT')
            assert blocked is False
            assert ratio == 1.5

    def test_fail_open_on_atr_unavailable(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=None):
            from bybit_ws.volatility_filter import is_high_volatility
            blocked, ratio, reason = is_high_volatility('SOLUSDT')
            assert blocked is False
            assert ratio == 0.0
            assert 'error' in reason.lower()

    def test_fail_open_on_exception(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', side_effect=Exception('network')):
            from bybit_ws.volatility_filter import is_high_volatility
            blocked, ratio, reason = is_high_volatility('SOLUSDT')
            assert blocked is False
            assert ratio == 0.0
            assert 'error' in reason.lower()


class TestVolatilityScaleEnabled:
    CFG = {
        'enabled': True,
        'atr_ratio_threshold': 2.0,
        'baseline_days': 20,
        'min_scale': 0.5,
    }

    def test_scale_below_one_when_ratio_above_one(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=800.0), \
             patch('bybit_ws.volatility_filter._baseline_atr', return_value=400.0):
            from bybit_ws.volatility_filter import volatility_scale
            scale = volatility_scale('SOLUSDT')
            assert scale == 0.5  # 1/2.0

    def test_scale_one_when_ratio_below_one(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=200.0), \
             patch('bybit_ws.volatility_filter._baseline_atr', return_value=400.0):
            from bybit_ws.volatility_filter import volatility_scale
            assert volatility_scale('SOLUSDT') == 1.0

    def test_scale_floored_at_min_scale(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', return_value=4000.0), \
             patch('bybit_ws.volatility_filter._baseline_atr', return_value=400.0):
            from bybit_ws.volatility_filter import volatility_scale
            # ratio=10 → 1/10=0.1, but min_scale=0.5
            assert volatility_scale('SOLUSDT') == 0.5

    def test_scale_returns_one_on_error(self):
        with patch('bybit_ws.volatility_filter._vf_cfg', return_value=self.CFG), \
             patch('bybit_ws.volatility_filter.fetch_atr', side_effect=Exception('fail')):
            from bybit_ws.volatility_filter import volatility_scale
            assert volatility_scale('SOLUSDT') == 1.0
