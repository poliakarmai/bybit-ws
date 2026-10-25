"""Аудит #14: suggest_qty не раздувает позицию до lot_step.

Симптом: margin=$1, 10x, price=$1000 → номинал $10, lot_step=0.1 → qty=0.01,
но возвращался max(qty, lot_step)=0.1 → номинал $100 (x10 раздувание).

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_suggest_qty.py -q
"""
import os
import sys

os.environ['BYBIT_LOG_SILENT'] = '1'
sys.path.insert(0, '/home/openclaw')

from bybit_ws.position_sizing import suggest_qty  # noqa: E402


def test_qty_above_lot_step_is_computed_normally():
    """(а) margin=$100, 10x, price=$1000 → номинал $1000 → qty=1.0."""
    assert suggest_qty(100, 1000, leverage=10, lot_step=0.1) == 1.0


def test_qty_below_lot_step_returns_zero():
    """(б) margin=$1, 10x, price=$1000 → qty=0.01 < 0.1 → 0, а не 0.1."""
    assert suggest_qty(1, 1000, leverage=10, lot_step=0.1) == 0


def test_zero_margin_returns_zero():
    """(в) margin=0 → 0."""
    assert suggest_qty(0, 1000, leverage=10, lot_step=0.1) == 0
