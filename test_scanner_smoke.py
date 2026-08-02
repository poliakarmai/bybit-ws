#!/usr/bin/env python3
"""Smoke-тесты GridSignal Scanner v4.1 — RSI, BB, edge cases (без API)."""
import sys, os, math, random

sys.path.insert(0, os.path.expanduser('~/.local/bin'))
from gridsignal_scanner import calc_rsi, calc_bb, score_coin, score_short, score_scalp

FAILS = 0
CHECKS = 0

def check(name, condition, detail=''):
    global CHECKS, FAILS
    CHECKS += 1
    ok = bool(condition)
    status = '✅' if ok else '❌'
    if not ok:
        FAILS += 1
        print(f'  {status} {name} {detail}')
    else:
        print(f'  {status} {name}')

# ── Test data helpers ──
def make_candles(prices):
    """prices: list of close prices, generate full candle arrays."""
    return [[str(i), str(p*0.99), str(p*1.02), str(p*0.98), str(p), '1000', '10000'] for i, p in enumerate(prices)]

# ═══ RSI ═══
print('\n📊 RSI')

# Uptrend: prices climb from 90 to 120 over 30 days
uptrend_prices = [90 + i for i in range(30)]
rsi_up = calc_rsi(make_candles(uptrend_prices), 14)
check('Uptrend RSI > 65', rsi_up is not None and rsi_up > 65, f'(got {rsi_up:.1f})')
print(f'    RSI = {rsi_up:.1f}')

# Downtrend: prices drop 120→90
downtrend_prices = [120 - i for i in range(30)]
rsi_down = calc_rsi(make_candles(downtrend_prices), 14)
check('Downtrend RSI < 35', rsi_down is not None and rsi_down < 35, f'(got {rsi_down:.1f})')
print(f'    RSI = {rsi_down:.1f}')

# Sideways
random.seed(42)
sideways_prices = [100 + random.uniform(-0.3, 0.3) for _ in range(30)]
rsi_side = calc_rsi(make_candles(sideways_prices), 14)
check('Sideways RSI 35-65', rsi_side is not None and 35 < rsi_side < 65, f'(got {rsi_side:.1f})')
print(f'    RSI = {rsi_side:.1f}')

# Not enough data
short_candles = make_candles([100]*10)
check('Short data → None', calc_rsi(short_candles, 14) is None)

# ═══ BB ═══
print('\n📊 Bollinger Bands')

# Normal candles: oscillating around 10
prices = [10 + math.sin(i/3)*2 for i in range(30)]
bb = calc_bb(make_candles(prices))
check('BB returns dict', bb is not None)
if bb:
    check('lower < middle < upper', bb['lower'] < bb['middle'] < bb['upper'],
          f'(L={bb["lower"]:.2f} M={bb["middle"]:.2f} U={bb["upper"]:.2f})')
    check('pos 0-100', 0 <= bb['pos'] <= 100, f'(got {bb["pos"]:.1f})')
    check('width > 0', bb['width'] > 0)
    print(f'    Lower={bb["lower"]:.2f} Middle={bb["middle"]:.2f} Upper={bb["upper"]:.2f} Pos={bb["pos"]:.1f}% Width={bb["width"]:.1f}%')

# Flat candles (all same price)
flat_bb = calc_bb(make_candles([100]*30))
check('Flat market: width near 0', flat_bb is not None and flat_bb['width'] < 5,
      f'(width={flat_bb["width"]:.1f}%' if flat_bb else 'None')

# ═══ Edge cases ═══
print('\n📊 Edge cases')

# turnover24h = None (API returns null)
bad_ticker = {'symbol': 'TESTUSDT', 'lastPrice': '10.5', 'price24hPcnt': '0.05',
              'turnover24h': None, 'fundingRate': '0.0001'}
s = score_coin('TESTUSDT', bad_ticker)
check('None turnover → no crash', s is None or isinstance(s, dict))

# Scalp with flat BB
if flat_bb:
    flat_ticker = {'symbol': 'FLATUSDT', 'lastPrice': '100', 'price24hPcnt': '0',
                   'turnover24h': '1000000', 'fundingRate': '0.0001'}
    s2 = score_scalp('FLATUSDT', flat_ticker)
    check('Scalp flat market → no crash', s2 is None or isinstance(s, dict))

# Missing keys in ticker
bad_ticker2 = {'symbol': 'BADSYMBOL'}
s3 = score_coin('BADSYMBOL', bad_ticker2)
check('Missing keys → no crash', s3 is None or isinstance(s3, dict))

# score_short basic
ok_ticker = {'symbol': 'OKUSDT', 'lastPrice': '15.0', 'price24hPcnt': '0.10',
             'turnover24h': '50000000', 'fundingRate': '0.0005'}
s4 = score_short('OKUSDT', ok_ticker)
check('score_short returns dict or None', s4 is None or (isinstance(s4, dict) and 'score' in s4))

# ═══ Result ═══
print(f'\n{"="*40}')
print(f'{"✅ ALL {CHECKS} PASS" if FAILS == 0 else f"❌ {FAILS}/{CHECKS} FAILED"}')
sys.exit(0 if FAILS == 0 else 1)
