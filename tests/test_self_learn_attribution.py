"""Тесты кредита bandit-руки и отключения canary (аудит самообучения 26.10).

Проверяют три вещи:
1. record_arm_assignment/pop_arm_assignment — round trip и окно свежести.
2. on_trade_closed кредитует ИМЕННО ту руку, что была выбрана на входе,
   и не трогает posterior, если назначения нет (раньше кредитовалась
   случайная рука — повторный Thompson-сэмпл на закрытии).
3. CANARY_ENABLED=False: canary-параметры не применяются к live.
"""
import os
import pathlib
import time
import sys

os.environ["BYBIT_LOG_SILENT"] = "1"
sys.path.insert(0, "/home/openclaw")

import pytest

from bybit_ws.journal import self_learn as sl


@pytest.fixture()
def tmp_assignments(tmp_path, monkeypatch):
    path = tmp_path / "bandit_arm_assignments.jsonl"
    monkeypatch.setattr(sl, "ARM_ASSIGNMENTS", path)
    return path


def test_assignment_roundtrip(tmp_assignments):
    sl.record_arm_assignment("SOLUSDT", "buy", 1000.0, "RANGING", 3)
    sl.record_arm_assignment("BTCUSDT", "buy", 1000.0, "RANGING", 1)

    got = sl.pop_arm_assignment("SOLUSDT", "buy", 1200.0)
    assert got == ("RANGING", 3), got
    # запись забрана — повторно не выдаётся
    assert sl.pop_arm_assignment("SOLUSDT", "buy", 1200.0) is None
    # чужая запись осталась
    assert sl.pop_arm_assignment("BTCUSDT", "buy", 1200.0) == ("RANGING", 1)


def test_assignment_stale_and_wrong_side_ignored(tmp_assignments):
    sl.record_arm_assignment("SOLUSDT", "buy", 1000.0, "RANGING", 2)
    # закрытие позже окна в 72ч — не матчится
    assert sl.pop_arm_assignment("SOLUSDT", "buy", 1000.0 + 73 * 3600) is None
    sl.record_arm_assignment("SOLUSDT", "buy", 1000.0, "RANGING", 2)
    # другая сторона — не матчится
    assert sl.pop_arm_assignment("SOLUSDT", "sell", 1100.0) is None


class _FakeEnsemble:
    def __init__(self):
        self.updates = []
        self.sampled = 0

    def select_params(self, regime=None):
        self.sampled += 1
        return {"min_score": 30}, 0

    def update(self, regime, arm_idx, win):
        self.updates.append((regime, arm_idx, win))


def test_trade_closed_credits_recorded_arm(tmp_assignments, monkeypatch):
    fake = _FakeEnsemble()
    monkeypatch.setattr(sl, "load_ensemble", lambda: fake)
    monkeypatch.setattr(sl, "save_ensemble", lambda ens: None)
    monkeypatch.setattr(sl, "get_regime_drift_detector", lambda: type("D", (), {"update": lambda *a, **k: None})())
    monkeypatch.setattr(sl, "_load_symbol_profiles", lambda: {})
    monkeypatch.setattr(sl, "_save_symbol_profiles", lambda p: None)

    sl.record_arm_assignment("SOLUSDT", "buy", time.time() - 3600, "TRENDING_UP", 2)
    sl.on_trade_closed("SOLUSDT", "buy", 5.0, 3.0, "TP")

    assert fake.updates == [("TRENDING_UP", 2, True)], fake.updates
    # на закрытии рука НЕ пересэмплируется
    assert fake.sampled == 0


def test_trade_closed_without_assignment_is_noop(tmp_assignments, monkeypatch):
    fake = _FakeEnsemble()
    monkeypatch.setattr(sl, "load_ensemble", lambda: fake)
    monkeypatch.setattr(sl, "save_ensemble", lambda ens: None)
    monkeypatch.setattr(sl, "get_regime_drift_detector", lambda: type("D", (), {"update": lambda *a, **k: None})())
    monkeypatch.setattr(sl, "_load_symbol_profiles", lambda: {})
    monkeypatch.setattr(sl, "_save_symbol_profiles", lambda p: None)

    # SHORT и ручные входы руки не назначают — posterior не должен меняться
    sl.on_trade_closed("BTCUSDT", "sell", -3.0, 1.0, "SL")
    assert fake.updates == []
    assert fake.sampled == 0


def test_canary_disabled():
    assert sl.CANARY_ENABLED is False
    assert sl.should_use_canary() is False
    assert sl.get_canary_param("tp_mult", 1.0) == 1.0
