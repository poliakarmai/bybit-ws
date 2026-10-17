"""Tests for entry-price-drift detection (bybit_ws.journal.analyzer.check_entry_price_drift).

Regression guard for the 17.10.2026 fix: the old `check_chasing` compared entry
prices of trades separated by days/weeks (median gap 70.9h) and produced a false
[HIGH] "chasing_momentum". Pairs further apart than max_gap_hours must be skipped.

Run: cd /home/openclaw/bybit-ws && python3 -m pytest test_entry_price_drift.py -q
"""
import importlib.util
import sys

# Load analyzer.py directly by path. Importing `bybit_ws.journal` via sys.path is
# fragile here: other test modules put /home/openclaw first, where `bybit_ws` is a
# symlink to the repo root (no journal/ package). Direct file loading is immune to
# that ordering.
_ANALYZER_PATH = '/home/openclaw/bybit-ws/bybit_ws/journal/analyzer.py'
_spec = importlib.util.spec_from_file_location('_journal_analyzer_under_test', _ANALYZER_PATH)
assert _spec and _spec.loader, f'cannot load analyzer from {_ANALYZER_PATH}'
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

Trade = _mod.Trade
check_entry_price_drift = _mod.check_entry_price_drift


def _buys(prices, step_hours, symbol='AAAUSDT'):
    """Buys in chronological order, one every step_hours."""
    return [
        Trade(symbol=symbol, side='buy', quantity=1.0, price=p,
              timestamp=1_700_000_000 + i * step_hours * 3600)
        for i, p in enumerate(prices)
    ]


class TestGapFiltering:
    def test_pairs_further_than_max_gap_are_skipped(self):
        """Growth exists but entries are 90h apart -> nothing to evaluate."""
        trades = _buys([100, 101, 102, 104, 106, 108], step_hours=30)
        rep = check_entry_price_drift(trades, max_gap_hours=24.0)
        assert rep.name == 'entry_price_drift'
        assert rep.severity == 'low'
        assert 'оценивать нечего' in rep.evidence
        assert rep.metrics['buys_evaluated'] == 0

    def test_close_pairs_with_growth_are_counted(self):
        """Entries 3h apart, price +3.5% over 3 steps -> high drift."""
        trades = _buys([100, 101, 102, 103.5, 105, 106], step_hours=1)
        rep = check_entry_price_drift(trades, max_gap_hours=24.0)
        assert rep.metrics['buys_evaluated'] == 3
        assert rep.metrics['drift_ratio'] == 1.0
        assert rep.severity == 'high'

    def test_close_pairs_without_growth(self):
        """Entries close in time but flat/falling prices -> no drift."""
        trades = _buys([100, 100, 100, 100, 99, 98], step_hours=1)
        rep = check_entry_price_drift(trades, max_gap_hours=24.0)
        assert rep.metrics['drift_ratio'] == 0.0
        assert rep.severity == 'low'

    def test_custom_gap_threshold_widens_window(self):
        """Same 90h-apart series passes when max_gap_hours=100."""
        trades = _buys([100, 101, 102, 104, 106, 108], step_hours=30)
        rep = check_entry_price_drift(trades, max_gap_hours=100.0)
        assert rep.metrics['buys_evaluated'] == 3
        assert rep.metrics['max_gap_hours'] == 100.0

    def test_too_few_buys(self):
        rep = check_entry_price_drift(_buys([100, 101], step_hours=1))
        assert rep.severity == 'low'
        assert 'Слишком мало' in rep.evidence

    def test_empty_and_sell_only(self):
        assert check_entry_price_drift([]).name == 'entry_price_drift'
        sells = [Trade(symbol='AAAUSDT', side='sell', quantity=1.0, price=100,
                       timestamp=1_700_000_000 + i * 3600) for i in range(6)]
        assert 'Слишком мало' in check_entry_price_drift(sells).evidence

    def test_metric_renamed_from_chasing(self):
        """The misleading 'chasing_momentum' label must be gone."""
        rep = check_entry_price_drift(_buys([100, 101, 102, 104, 106, 108], step_hours=1))
        assert rep.name == 'entry_price_drift'
        assert 'chase_ratio' not in rep.metrics
        assert 'drift_ratio' in rep.metrics


class TestMixedGaps:
    def test_only_close_pairs_evaluated(self):
        """4 buys: 3 close (1h) then a distant one (200h). Only close pairs count."""
        base = 1_700_000_000
        trades = [
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=100, timestamp=base),
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=100.5, timestamp=base + 3600),
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=101, timestamp=base + 7200),
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=104, timestamp=base + 10800),
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=120, timestamp=base + 200 * 3600),
            Trade(symbol='AAAUSDT', side='buy', quantity=1.0, price=121, timestamp=base + 201 * 3600),
        ]
        rep = check_entry_price_drift(trades, max_gap_hours=24.0)
        # i=3 (close, +4%) counted; i=4,5 have gaps >24h -> skipped
        assert rep.metrics['buys_evaluated'] == 1
        assert rep.metrics['drift_ratio'] == 1.0


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
