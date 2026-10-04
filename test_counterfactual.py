"""Test counterfactual PnL logging for rejected Entry Judge signals."""
import os
import tempfile
import pytest
import state_db
from state_db import StateDB


def test_counterfactual_logged_on_rejection(monkeypatch):
    """When Entry Judge rejects, should_enter returns (False, ...) AND logs counterfactual."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        test_db = StateDB(db_path)
        monkeypatch.setattr(state_db, "db", test_db)

        import entry_judge
        monkeypatch.setattr(entry_judge, "JUDGE_ENABLED", True)
        monkeypatch.setattr(entry_judge, "judge_entry", lambda *a, **kw: {
            "verdict": "revise",
            "blocking_issues": ["test"],
            "confidence": 0.3,
        })

        can_enter, reason = entry_judge.should_enter(
            symbol="BTCUSDT", side="BUY", score=85,
            bb_pos=45.2, entry_price=50000.0,
        )

        assert can_enter is False
        assert "judge blocked" in reason
        assert "test" in reason

        counterfactuals = test_db.get_counterfactual()
        assert len(counterfactuals) == 1

        cf = counterfactuals[0]
        assert cf['symbol'] == "BTCUSDT"
        assert cf['side'] == "BUY"
        assert cf['score'] == 85
        assert cf['bb_pos'] == 45.2
        assert cf['entry_price'] == 50000.0
        assert cf['confidence'] == 0.3
        assert cf['issues'] == "test"
        assert cf['ts'] > 0

    finally:
        os.unlink(db_path)


def test_no_counterfactual_on_pass(monkeypatch):
    """When Entry Judge passes, should_enter returns (True, ...) AND no counterfactual logged."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        test_db = StateDB(db_path)
        monkeypatch.setattr(state_db, "db", test_db)

        import entry_judge
        monkeypatch.setattr(entry_judge, "JUDGE_ENABLED", True)
        monkeypatch.setattr(entry_judge, "judge_entry", lambda *a, **kw: {
            "verdict": "pass",
            "blocking_issues": [],
            "confidence": 0.9,
        })

        can_enter, reason = entry_judge.should_enter(
            symbol="ETHUSDT", side="SELL", score=92,
            bb_pos=78.5, entry_price=3000.0,
        )

        assert can_enter is True
        assert "judge pass" in reason

        counterfactuals = test_db.get_counterfactual()
        assert len(counterfactuals) == 0

    finally:
        os.unlink(db_path)
