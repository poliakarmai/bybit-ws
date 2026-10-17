"""Tests for cluster risk accounting (stage 1 — shadow mode).

Run: cd /home/openclaw/bybit-ws && python3 -m pytest test_cluster_risk.py -q
"""
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

# Ensure bybit_ws package is importable (symlink bybit_ws -> . in parent dir)
sys.path.insert(0, '/home/openclaw')


class _StubCfg:
    """Minimal config stub exposing a `.risk` dict."""
    def __init__(self, risk):
        self.risk = risk


def _cfg(mode='shadow', **over):
    risk = {
        'max_daily_loss': 50,
        'cluster_risk_mode': mode,
        'cluster_corr_threshold': 0.70,
        'cluster_risk_pct': 0.50,
        'cluster_snapshot_max_age_sec': 900,
        'banned_symbols': [],
        'max_long_positions': 12,
    }
    risk.update(over)
    return _StubCfg(risk)


def _snap(pairs, age_sec=0):
    ts = (datetime.now() - timedelta(seconds=age_sec)).isoformat()
    return {'timestamp': ts, 'pairs': pairs}


# Two members, each risking $100 (10 * |100-90|). budget = 50 * 0.5 = 25.
OVER_POSITIONS = {
    'BBBUSDT': {'side': 'Buy', 'size': 10, 'entry': 100, 'stopLoss': 90, 'margin': 30},
    'CCCUSDT': {'side': 'Buy', 'size': 10, 'entry': 100, 'stopLoss': 90, 'margin': 30},
}
OVER_PAIRS = [['AAAUSDT', 'BBBUSDT', 0.90], ['AAAUSDT', 'CCCUSDT', 0.85]]


class TestOverBudgetEnforce:
    def test_blocks_with_used_and_budget_in_reason(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(OVER_PAIRS)):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is False
            assert 'Cluster risk' in reason
            assert '200.00' in reason   # used
            assert '25.00' in reason    # budget


class TestOverBudgetShadow:
    def test_shadow_does_not_block_but_reports_multiplier(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(OVER_PAIRS)):
            from bybit_ws.risk_manager import check_cluster_risk, cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert ok is True
            r = cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['size_multiplier'] < 1.0
            assert r['size_multiplier'] == 25.0 / 200.0


class TestUnderBudget:
    def test_allows_with_empty_reason(self):
        positions = {
            'BBBUSDT': {'side': 'Buy', 'size': 1, 'entry': 100, 'stopLoss': 90, 'margin': 30},
        }
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap([['AAAUSDT', 'BBBUSDT', 0.90]])):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, reason = check_cluster_risk(positions, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert ok is True
            assert reason == ''


class TestUncorrelated:
    def test_low_corr_not_clustered(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap([['AAAUSDT', 'BBBUSDT', 0.30]])):
            from bybit_ws.risk_manager import cluster_risk
            r = cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['members'] == []


class TestOppositeSide:
    def test_short_not_clustered_with_long(self):
        positions = {
            'BBBUSDT': {'side': 'Sell', 'size': 10, 'entry': 100, 'stopLoss': 90, 'margin': 30},
        }
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap([['AAAUSDT', 'BBBUSDT', 0.90]])):
            from bybit_ws.risk_manager import cluster_risk
            r = cluster_risk(positions, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['members'] == []


class TestNegativeCorrelation:
    def test_negative_corr_not_clustered(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap([['AAAUSDT', 'BBBUSDT', -0.90]])):
            from bybit_ws.risk_manager import cluster_risk
            r = cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['members'] == []


class TestStaleSnapshot:
    def test_stale_fails_open_and_flags(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(OVER_PAIRS, age_sec=3600)):
            from bybit_ws.risk_manager import check_cluster_risk, cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is True
            assert 'stale' in reason.lower()
            r = cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert r['stale'] is True


class TestMissingSnapshot:
    def test_none_snapshot_fails_open(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot', return_value=None):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is True


class TestSnapshotException:
    def test_exception_fails_open_no_crash(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   side_effect=Exception('io boom')):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is True
            assert 'error' in reason.lower()


class TestModeOff:
    def test_off_mode_returns_off(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(OVER_PAIRS)):
            from bybit_ws.risk_manager import check_cluster_risk, cluster_risk
            ok, reason = check_cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('off'))
            assert ok is True
            r = cluster_risk(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('off'))
            assert r['mode'] == 'off'


class TestPositionRiskUsd:
    def test_with_sl_uses_size_times_distance(self):
        from bybit_ws.risk_manager import _position_risk_usd
        pos = {'size': 10, 'entry': 100, 'stopLoss': 90, 'margin': 30}
        assert _position_risk_usd(pos) == 100.0

    def test_without_sl_uses_margin_fallback(self):
        from bybit_ws.risk_manager import _position_risk_usd
        pos = {'size': 10, 'entry': 100, 'margin': 30}
        assert _position_risk_usd(pos) == 15.0


class TestPortfolioSlice:
    """Portfolio risk = all same-side positions, correlation-independent.

    Regression guard for the measured hole: 7 correlated LONGs risking $44.73
    while any single correlation cluster is only ~$15.
    """

    POSITIONS = {
        'BBBUSDT': {'side': 'Buy', 'size': 1, 'entry': 100, 'stopLoss': 90, 'margin': 30},
        'CCCUSDT': {'side': 'Buy', 'size': 1, 'entry': 100, 'stopLoss': 90, 'margin': 30},
        'DDDUSDT': {'side': 'Buy', 'size': 1, 'entry': 100, 'stopLoss': 90, 'margin': 30},
    }
    # new symbol correlates with nothing above threshold -> cluster empty, portfolio $30 > $25
    LOW_PAIRS = [['AAAUSDT', 'BBBUSDT', 0.30]]

    def test_portfolio_over_budget_flags_even_without_cluster(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(self.LOW_PAIRS)):
            from bybit_ws.risk_manager import cluster_risk
            r = cluster_risk(self.POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['members'] == []
            assert r['portfolio_used_usd'] == 30.0
            assert r['over'] is True
            assert 'portfolio' in r['reason']

    def test_portfolio_blocks_in_enforce(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(self.LOW_PAIRS)):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, reason = check_cluster_risk(self.POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is False
            assert 'portfolio' in reason

    def test_portfolio_shadow_does_not_block(self):
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(self.LOW_PAIRS)):
            from bybit_ws.risk_manager import check_cluster_risk
            ok, _ = check_cluster_risk(self.POSITIONS, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert ok is True

    def test_portfolio_ignores_opposite_side(self):
        positions = dict(self.POSITIONS)
        positions['DDDUSDT'] = {'side': 'Sell', 'size': 1, 'entry': 100, 'stopLoss': 90, 'margin': 30}
        with patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(self.LOW_PAIRS)):
            from bybit_ws.risk_manager import cluster_risk
            r = cluster_risk(positions, 'AAAUSDT', 'Buy', _cfg('shadow'))
            assert r['portfolio_used_usd'] == 20.0   # only the two Buy positions
            assert r['over'] is False


class TestIntegrationWithCheck:
    def test_check_returns_cluster_reason_in_enforce(self):
        with patch('bybit_ws.risk_manager.check_circuit_breaker', return_value=(False, '')), \
             patch('bybit_ws.risk_manager.get_dynamic_max_positions',
                   return_value={'max_positions': 12, 'volatility_level': 'normal'}), \
             patch('bybit_ws.risk_manager.get_daily_pnl', return_value={'total_pnl': 0.0}), \
             patch('bybit_ws.risk_manager.check_new_entry_correlation', return_value=(True, '')), \
             patch('bybit_ws.risk_manager._load_correlation_snapshot',
                   return_value=_snap(OVER_PAIRS)):
            from bybit_ws.risk_manager import check
            ok, reason = check(OVER_POSITIONS, 'AAAUSDT', 'Buy', _cfg('enforce'))
            assert ok is False
            assert 'Cluster risk' in reason
