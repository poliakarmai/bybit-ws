"""
Correlation risk matrix for bybit-ws.
Calculates 24h price correlation between all symbols currently in positions
using hourly klines from Bybit API. Flags pairs with >0.8 correlation as
concentration risk.
"""

import json
import math
import os
import time
from datetime import datetime

from . import DATA_DIR
from .api import bybit

CORRELATION_SNAPSHOT = os.path.join(DATA_DIR, 'correlation.json')
CORRELATION_THRESHOLD = 0.80
MIN_CANDLES = 12  # minimum overlapping candles for valid correlation


def fetch_klines(symbol, interval='60', limit=24):
    """Fetch kline close prices for a symbol.

    Returns list of float close prices (chronological order), or None on failure.
    """
    path = f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}'
    data = bybit('GET', path)
    if not data or data.get('retCode') != 0:
        return None
    try:
        candles = data['result']['list']
        if not candles:
            return None
        # API returns newest-first; reverse to chronological, extract close (index 4)
        closes = [float(c[4]) for c in reversed(candles)]
        return closes
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def pearson_r(x, y):
    """Calculate Pearson correlation coefficient between two equal-length lists."""
    n = len(x)
    if n != len(y) or n < 2:
        return 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    sum_x2 = sum(xi * xi for xi in x)
    sum_y2 = sum(yi * yi for yi in y)

    numerator = n * sum_xy - sum_x * sum_y
    denom_x = n * sum_x2 - sum_x * sum_x
    denom_y = n * sum_y2 - sum_y * sum_y
    denominator = math.sqrt(denom_x * denom_y)

    if denominator == 0:
        return 0.0

    return numerator / denominator


def price_returns(prices):
    """Convert price series to log returns for correlation."""
    if len(prices) < 2:
        return []
    return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]


def check_correlation(positions):
    """Calculate 24h price correlations between all position symbols.

    Args:
        positions: dict of {symbol: position_data} from fetch_positions()

    Returns:
        dict with:
            - 'pairs': list of (sym1, sym2, correlation) for all computed pairs
            - 'flagged': list of (sym1, sym2, correlation) for pairs > threshold
            - 'messages': list of warning strings for the main loop
            - 'timestamp': ISO timestamp of computation
            - 'position_count': number of symbols analyzed
    """
    if not positions or len(positions) < 2:
        return {
            'pairs': [],
            'flagged': [],
            'messages': [],
            'timestamp': datetime.now().isoformat(),
            'position_count': len(positions) if positions else 0,
        }

    symbols = list(positions.keys())

    # Fetch klines for all symbols
    prices = {}
    for sym in symbols:
        closes = fetch_klines(sym, interval='60', limit=24)
        if closes and len(closes) >= MIN_CANDLES:
            prices[sym] = closes

    if len(prices) < 2:
        return {
            'pairs': [],
            'flagged': [],
            'messages': [],
            'timestamp': datetime.now().isoformat(),
            'position_count': len(positions),
            'symbols_fetched': len(prices),
        }

    # Compute correlations for all pairs
    syms = sorted(prices.keys())
    pairs = []
    flagged = []
    messages = []

    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            s1, s2 = syms[i], syms[j]
            # Use the shorter of the two series
            p1 = prices[s1]
            p2 = prices[s2]
            # Ensure same length (truncate to shorter)
            min_len = min(len(p1), len(p2))
            r1 = price_returns(p1[:min_len])
            r2 = price_returns(p2[:min_len])

            if len(r1) < 2:
                continue

            corr = pearson_r(r1, r2)
            # Clamp to [-1, 1] to avoid floating-point noise
            corr = max(-1.0, min(1.0, corr))

            pairs.append((s1, s2, round(corr, 4)))

            if abs(corr) > CORRELATION_THRESHOLD:
                flagged.append((s1, s2, round(corr, 4)))
                direction = '📈📈' if corr > 0 else '📈📉'
                messages.append(
                    f'⚠️ Корреляция {direction} {s1}↔{s2}: r={corr:+.3f} '
                    f'(>±{CORRELATION_THRESHOLD}) — концентрационный риск'
                )

    # Save snapshot for dashboard
    result = {
        'pairs': pairs,
        'flagged': flagged,
        'messages': messages,
        'timestamp': datetime.now().isoformat(),
        'position_count': len(positions),
        'symbols_fetched': len(prices),
        'threshold': CORRELATION_THRESHOLD,
    }

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CORRELATION_SNAPSHOT, 'w') as f:
            json.dump(result, f, indent=2, default=str)
    except (IOError, OSError):
        pass

    return result


def load_correlation_snapshot():
    """Load the last correlation snapshot from disk. Returns dict or None."""
    if not os.path.exists(CORRELATION_SNAPSHOT):
        return None
    try:
        with open(CORRELATION_SNAPSHOT) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None
