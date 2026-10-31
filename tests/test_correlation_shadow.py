"""Тесты для корреляционного shadow-режима в position_sizing.py."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bybit_ws import position_sizing as ps  # noqa: E402


def test_check_correlation_shadow_no_file():
    """Файл correlation.json отсутствует — функция должна вернуть True."""
    with patch.object(ps, "CORRELATION_FILE", "/nonexistent/correlation.json"):
        result = ps.check_correlation_shadow("BTCUSDT", 100.0)
        assert result is True


def test_check_correlation_shadow_stale_data():
    """Данные в correlation.json протухли — функция должна вернуть True."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"flagged": [["BTCUSDT", "ETHUSDT", 0.85]]}, f)
        temp_path = f.name

    try:
        # Сделать файл старым
        old_time = time.time() - (ps.CORRELATION_STALENESS + 100)
        os.utime(temp_path, (old_time, old_time))

        with patch.object(ps, "CORRELATION_FILE", temp_path):
            result = ps.check_correlation_shadow("BTCUSDT", 100.0)
            assert result is True
    finally:
        os.unlink(temp_path)


def test_check_correlation_shadow_no_flagged():
    """Флагged пары отсутствуют — функция должна вернуть True."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"flagged": []}, f)
        temp_path = f.name

    try:
        with patch.object(ps, "CORRELATION_FILE", temp_path):
            result = ps.check_correlation_shadow("BTCUSDT", 100.0)
            assert result is True
    finally:
        os.unlink(temp_path)


def test_check_correlation_shadow_symbol_not_in_cluster():
    """Символ не входит в коррелированный кластер — функция должна вернуть True."""
    flagged = [["BTCUSDT", "ETHUSDT", 0.85], ["SOLUSDT", "AVAXUSDT", 0.80]]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"flagged": flagged}, f)
        temp_path = f.name

    try:
        with patch.object(ps, "CORRELATION_FILE", temp_path):
            # LINKUSDT не в кластере
            result = ps.check_correlation_shadow("LINKUSDT", 100.0)
            assert result is True
    finally:
        os.unlink(temp_path)


def test_check_correlation_shadow_within_limit():
    """Корреляция в пределах лимита — функция должна вернуть True."""
    flagged = [["BTCUSDT", "ETHUSDT", 0.85]]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"flagged": flagged}, f)
        temp_path = f.name

    try:
        # Мокаем позиции: только BTCUSDT с маржой 50
        fake_positions = [
            {"symbol": "BTCUSDT", "margin": 50.0},
            {"symbol": "SOLUSDT", "margin": 100.0},  # не в кластере
        ]

        with patch.object(ps, "CORRELATION_FILE", temp_path), \
             patch("bybit_ws.api.fetch_positions", return_value=fake_positions), \
             patch.object(ps, "_p", return_value=40):  # limit 40%
            # Новая позиция ETHUSDT с маржой 50
            # Кластер BTCUSDT+ETHUSDT: 50 (BTC) + 50 (ETH new) = 100
            # Общий margin: 50 (BTC) + 100 (SOL) + 50 (ETH new) = 200
            # Лимит: 200 * 40% = 80
            # Кластер 100 > 80 → должен залогировать, но вернуть True
            result = ps.check_correlation_shadow("ETHUSDT", 50.0)
            assert result is True
    finally:
        os.unlink(temp_path)


def test_check_correlation_shadow_exceeds_limit():
    """Корреляция превышает лимит — функция должна залогировать, но вернуть True (shadow)."""
    flagged = [["BTCUSDT", "ETHUSDT", 0.85]]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"flagged": flagged}, f)
        temp_path = f.name

    try:
        # Мокаем позиции: только BTCUSDT с маржой 200
        fake_positions = [
            {"symbol": "BTCUSDT", "margin": 200.0},
        ]

        with patch.object(ps, "CORRELATION_FILE", temp_path), \
             patch("bybit_ws.api.fetch_positions", return_value=fake_positions), \
             patch.object(ps, "_p", return_value=40):  # limit 40%
            # Новая позиция ETHUSDT с маржой 100
            # Кластер BTCUSDT+ETHUSDT: 200 (BTC) + 100 (ETH new) = 300
            # Общий margin: 200 (BTC) + 100 (ETH new) = 300
            # Лимит: 300 * 40% = 120
            # Кластер 300 > 120 → должен залогировать предупреждение
            result = ps.check_correlation_shadow("ETHUSDT", 100.0)
            # Shadow-режим ВСЕГДА возвращает True
            assert result is True
    finally:
        os.unlink(temp_path)


def test_check_correlation_shadow_always_returns_true():
    """Критический тест: функция ВСЕГДА возвращает True (shadow-режим)."""
    # Даже при ошибке чтения файла
    with patch.object(ps, "CORRELATION_FILE", "/invalid/path.json"):
        result = ps.check_correlation_shadow("BTCUSDT", 100.0)
        assert result is True

    # Даже при JSON parse error
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{invalid json")
        temp_path = f.name

    try:
        with patch.object(ps, "CORRELATION_FILE", temp_path):
            result = ps.check_correlation_shadow("BTCUSDT", 100.0)
            assert result is True
    finally:
        os.unlink(temp_path)


def test_margin_for_strategy_wires_shadow(monkeypatch):
    """Обвязка: shadow обязан вызываться из единой точки сайзинга.

    Без неё функция — мёртвый код: логируется ноль, «shadow-режим» существует
    только на бумаге (поймано на приёмке 31.10).
    """
    calls = []
    monkeypatch.setattr(ps, 'check_correlation_shadow', lambda s, m: calls.append((s, m)))
    monkeypatch.setattr(ps, 'lock_reason', lambda *a, **k: None, raising=False)
    margin = ps.margin_for_strategy('long', score=5.5, symbol='ADAUSDT')
    assert margin > 0
    assert calls and calls[0][0] == 'ADAUSDT' and calls[0][1] == margin
