"""Порог imbalance для LONG в даунтренде (IMBALANCE_LONG_THRESHOLD_DOWNTREND=0.45).

В TRENDING_DOWN стакан sell-heavy по определению → порог 0.55 отсекал 100%
лонгов. Порог SHORT и fail-open при отсутствии данных не менялись.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_orderbook_regime_threshold.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

import pytest


@pytest.fixture()
def obf():
    return pytest.importorskip('bybit_ws.orderbook_filter')


def _imbalance(monkeypatch, obf, value):
    monkeypatch.setattr(obf, 'get_orderbook_imbalance', lambda symbol: value)


def test_long_passes_in_downtrend(obf, monkeypatch):
    """0.49 при мягком пороге 0.45 → LONG проходит."""
    _imbalance(monkeypatch, obf, 0.49)
    ok, reason = obf.should_enter_by_imbalance('BTCUSDT', 'Buy', 'TRENDING_DOWN')
    assert ok, reason


def test_long_blocked_without_regime(obf, monkeypatch):
    """0.49 без режима → старый порог 0.55 режет LONG."""
    _imbalance(monkeypatch, obf, 0.49)
    ok, reason = obf.should_enter_by_imbalance('BTCUSDT', 'Buy', '')
    assert not ok, reason


def test_long_blocked_still_in_downtrend(obf, monkeypatch):
    """0.40 < 0.45 → враждебный стакан режется и в даунтренде."""
    _imbalance(monkeypatch, obf, 0.40)
    ok, reason = obf.should_enter_by_imbalance('BTCUSDT', 'Buy', 'TRENDING_DOWN')
    assert not ok, reason


def test_short_threshold_unchanged(obf, monkeypatch):
    """SHORT порог остался 0.45: 0.60 → отказ."""
    _imbalance(monkeypatch, obf, 0.60)
    ok, reason = obf.should_enter_by_imbalance('BTCUSDT', 'Sell', '')
    assert not ok, reason


def test_no_data_fail_open(obf, monkeypatch):
    """API недоступен (None) → вход не блокируем."""
    _imbalance(monkeypatch, obf, None)
    ok, reason = obf.should_enter_by_imbalance('BTCUSDT', 'Buy', 'TRENDING_DOWN')
    assert ok, reason
