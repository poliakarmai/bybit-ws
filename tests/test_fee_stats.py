"""Тест замера входов (fee_stats) + гейт на post-only входы (этап 4 аудита).

Гейт: если кто-то вернёт входные лимитки на GTC, вход снова сможет исполниться
taker-ом (0.055% вместо 0.02%) — тест это ловит по исходникам.

Запуск:
    cd /home/openclaw/bybit-ws && python3 -m pytest tests/test_fee_stats.py -q
"""
import os
import sys

sys.path.insert(0, '/home/openclaw')
sys.path.insert(0, '/home/openclaw/bybit-ws')

from bybit_ws import fee_stats as fs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_summarize_basics():
    orders = [
        {'orderType': 'Limit', 'orderStatus': 'Filled', 'timeInForce': 'PostOnly',
         'reduceOnly': False, 'cumExecFee': '0.02'},
        {'orderType': 'Limit', 'orderStatus': 'Cancelled', 'timeInForce': 'PostOnly',
         'reduceOnly': False, 'cumExecFee': '0'},
        {'orderType': 'Limit', 'orderStatus': 'Filled', 'reduceOnly': True,   # TP/SL — не вход
         'cumExecFee': '0.05'},
        {'orderType': 'Limit', 'orderStatus': 'Filled', 'stopOrderType': 'StopLoss',
         'reduceOnly': False, 'cumExecFee': '0.05'},
        {'orderType': 'Market', 'orderStatus': 'Filled', 'reduceOnly': False,
         'cumExecFee': '0.07'},
    ]
    executions = [
        {'closedSize': '0', 'isMaker': True, 'execFee': '0.01'},   # открытие maker
        {'closedSize': '0', 'isMaker': False, 'execFee': '0.02'},  # открытие taker
        {'closedSize': '1', 'isMaker': True, 'execFee': '0.01'},   # закрытие — не считаем
    ]
    s = fs.summarize(orders, executions)
    assert s['entry_orders'] == 3, s          # 2 лимитных + 1 market; TP/SL отброшены
    assert s['limit_entries'] == 2, s
    assert s['limit_filled'] == 1 and s['limit_cancelled'] == 1, s
    assert s['fill_rate'] == 50.0, s
    assert round(s['limit_entry_fee'], 2) == 0.02, s   # только входные, без TP/SL
    assert s['opening_executions'] == 2, s
    assert s['maker_executions'] == 1 and s['maker_share'] == 50.0, s
    assert round(s['opening_fee'], 2) == 0.03, s


def test_entry_limits_are_post_only():
    """Входные лимитки авто-ветки — только PostOnly (закрытия остаются IOC)."""
    for fname in ('auto_entry.py', 'auto_short.py'):
        src = open(os.path.join(ROOT, fname)).read()
        assert "'timeInForce': 'PostOnly'" in src, f'{fname}: входной ордер не PostOnly'
        assert "'timeInForce': 'GTC'" not in src, f'{fname}: входной ордер снова GTC'
    assert "'timeInForce': 'IOC'" in open(os.path.join(ROOT, 'auto_short.py')).read(), \
        'auto_short: закрытие потеряло IOC'


if __name__ == '__main__':
    test_summarize_basics()
    test_entry_limits_are_post_only()
    print('OK: fee_stats.summarize + post-only гейт')