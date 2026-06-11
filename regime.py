"""
Market regime detector for bybit-ws.

Uses Bybit API to get BTC and ETH 24h change, volume, and recent volatility (klines).
Classifies regime: TRENDING_UP, TRENDING_DOWN, CHOPPY, HIGH_VOL, LOW_VOL, NEUTRAL.
"""

import math
import json
import os
import time
from datetime import datetime

from .api import bybit

# Cache: avoid calling API too often
_regime_cache = {
    "regime": "UNKNOWN",
    "details": {},
    "ts": 0,
}
REGIME_CACHE_TTL = 120  # seconds — cache regime for 2 min
REGIME_FILE = os.path.join(os.path.expanduser("~"), ".local", "share", "bybit-ws", "regime.json")


def _fetch_klines(symbol: str, interval: str = "60", limit: int = 24) -> list[dict] | None:
    """Fetch klines for a symbol. Returns list of candles as dicts or None."""
    data = bybit(
        "GET",
        f"/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}",
    )
    if not data or data.get("retCode") != 0:
        return None
    result = data.get("result", {})
    raw_list = result.get("list", [])
    if not raw_list:
        return None
    # Each candle: [timestamp, open, high, low, close, volume, turnover]
    candles = []
    for c in raw_list:
        candles.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "turnover": float(c[6]),
        })
    # Reverse: API returns newest first, we want oldest first
    candles.reverse()
    return candles


def _fetch_tickers(symbols: list[str]) -> dict[str, dict] | None:
    """Fetch 24h ticker data for multiple symbols (one at a time — Bybit multi-symbol unreliable)."""
    tickers = {}
    for sym in symbols:
        data = bybit(
            "GET",
            f"/v5/market/tickers?category=linear&symbol={sym}",
        )
        if not data or data.get("retCode") != 0:
            continue
        result = data.get("result", {})
        for item in result.get("list", []):
            tickers[item["symbol"]] = {
                "price": float(item.get("lastPrice", 0)),
                "change_pct": float(item.get("price24hPcnt", 0)) * 100,  # already fraction, convert to %
                "volume_24h": float(item.get("volume24h", 0)),
                "turnover_24h": float(item.get("turnover24h", 0)),
                "high_24h": float(item.get("highPrice24h", 0)),
                "low_24h": float(item.get("lowPrice24h", 0)),
            }
    if not tickers:
        return None
    return tickers


def _calc_volatility(candles: list[dict]) -> float:
    """Calculate annualized volatility from a list of candles (log returns)."""
    if len(candles) < 3:
        return 0.0
    returns = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]["close"]
        curr = candles[i]["close"]
        if prev > 0:
            returns.append(math.log(curr / prev))
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    # Annualize: std * sqrt(periods_per_year)
    # For 1h candles: sqrt(365 * 24) = sqrt(8760) ≈ 93.6
    return std * math.sqrt(365 * 24)


def _calc_recent_volatility_pct(candles: list[dict]) -> float:
    """Calculate recent volatility as % of price range over the period."""
    if not candles:
        return 0.0
    closes = [c["close"] for c in candles]
    if not closes or closes[-1] == 0:
        return 0.0
    mean_price = sum(closes) / len(closes)
    if mean_price == 0:
        return 0.0
    # Standard deviation of closes as % of mean
    var = sum((c - mean_price) ** 2 for c in closes) / len(closes)
    return (math.sqrt(var) / mean_price) * 100


def _avg_volume_ratio(candles: list[dict]) -> float | None:
    """Calculate ratio of recent volume vs average. None if not enough data."""
    if len(candles) < 6:
        return None
    half = len(candles) // 2
    recent_vol = sum(c["volume"] for c in candles[-half:]) / half
    older_vol = sum(c["volume"] for c in candles[:half]) / half
    if older_vol == 0:
        return 1.0
    return recent_vol / older_vol


def check_regime(force: bool = False) -> dict:
    """
    Detect current market regime using BTC and ETH data.

    Returns dict with:
        regime: TRENDING_UP | TRENDING_DOWN | CHOPPY | HIGH_VOL | LOW_VOL | NEUTRAL | UNKNOWN
        confidence: 0-100
        details: dict with BTC/ETH metrics
    """
    global _regime_cache
    now = time.time()

    # Return cache if fresh enough
    if not force and (now - _regime_cache["ts"]) < REGIME_CACHE_TTL:
        if _regime_cache["regime"] != "UNKNOWN":
            return _regime_cache

    try:
        # Fetch BTC and ETH klines (1h candles, 24 candles = 24h)
        btc_klines = _fetch_klines("BTCUSDT", interval="60", limit=24)
        eth_klines = _fetch_klines("ETHUSDT", interval="60", limit=24)

        # Fetch 24h ticker data
        tickers = _fetch_tickers(["BTCUSDT", "ETHUSDT"])

        btc_change = 0.0
        eth_change = 0.0
        btc_vol_24h = 0.0
        eth_vol_24h = 0.0

        if tickers:
            btc_t = tickers.get("BTCUSDT", {})
            eth_t = tickers.get("ETHUSDT", {})
            btc_change = btc_t.get("change_pct", 0)
            eth_change = eth_t.get("change_pct", 0)
            btc_vol_24h = btc_t.get("turnover_24h", 0)
            eth_vol_24h = eth_t.get("turnover_24h", 0)

        # Calculate volatilities
        btc_vol = 0.0
        eth_vol = 0.0
        if btc_klines:
            btc_vol = _calc_recent_volatility_pct(btc_klines)
        if eth_klines:
            eth_vol = _calc_recent_volatility_pct(eth_klines)

        avg_vol = (btc_vol + eth_vol) / 2 if (btc_vol + eth_vol) > 0 else 0.0

        # Volume ratio (recent surge?)
        btc_vol_ratio = _avg_volume_ratio(btc_klines) if btc_klines else None
        eth_vol_ratio = _avg_volume_ratio(eth_klines) if eth_klines else None

        # ── Classification ──
        regime = "NEUTRAL"
        confidence = 50

        # Sharp trending detection
        trend_aligned = False
        if btc_change > 2.0 and eth_change > 1.5:
            regime = "TRENDING_UP"
            confidence = min(90, int(40 + abs(btc_change) * 6))
            trend_aligned = True
        elif btc_change < -2.0 and eth_change < -1.5:
            regime = "TRENDING_DOWN"
            confidence = min(90, int(40 + abs(btc_change) * 6))
            trend_aligned = True

        # Volatility-based refinement
        if avg_vol > 4.0:
            if not trend_aligned:
                regime = "HIGH_VOL"
            confidence = min(95, confidence + 15)
        elif avg_vol < 1.2 and abs(btc_change) < 1.5 and abs(eth_change) < 1.5:
            regime = "LOW_VOL"
            confidence = min(90, int(70 - avg_vol * 15))

        # Choppy: high vol but divergent directions or tight range w/ volume surge
        if not trend_aligned and avg_vol > 2.5:
            # Check if BTC and ETH diverge
            if (btc_change > 0 and eth_change < 0) or (btc_change < 0 and eth_change > 0):
                regime = "CHOPPY"
                confidence = min(85, int(40 + avg_vol * 8))
            # Check for volume surge without clear direction
            elif btc_vol_ratio and btc_vol_ratio > 1.5 and abs(btc_change) < 1.5:
                regime = "CHOPPY"
                confidence = 60

        # If trending but vol is low, lower confidence
        if trend_aligned and avg_vol < 1.5:
            confidence = max(40, confidence - 20)

        details = {
            "btc_change_pct": round(btc_change, 2),
            "eth_change_pct": round(eth_change, 2),
            "btc_volatility_pct": round(btc_vol, 2),
            "eth_volatility_pct": round(eth_vol, 2),
            "avg_volatility_pct": round(avg_vol, 2),
            "btc_turnover_24h": btc_vol_24h,
            "eth_turnover_24h": eth_vol_24h,
            "btc_vol_ratio": round(btc_vol_ratio, 2) if btc_vol_ratio else None,
            "timestamp": datetime.now().isoformat(),
        }

        result = {
            "regime": regime,
            "confidence": confidence,
            "details": details,
        }
        _regime_cache = {**result, "ts": now}

        # Persist to file for dashboard
        _save_regime_file(result)

        return result

    except Exception as e:
        # On failure, return cached or UNKNOWN
        if _regime_cache["regime"] != "UNKNOWN":
            return _regime_cache
        return {
            "regime": "UNKNOWN",
            "confidence": 0,
            "details": {"error": str(e)},
        }


def _save_regime_file(result: dict) -> None:
    """Persist regime to JSON file for dashboard consumption."""
    try:
        os.makedirs(os.path.dirname(REGIME_FILE), exist_ok=True)
        payload = {
            "regime": result["regime"],
            "confidence": result["confidence"],
            "details": result["details"],
        }
        with open(REGIME_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        log_event(f'⚠️ regime: {e}')


def get_cached_regime() -> dict:
    """Get the most recent regime from cache or file (no API call)."""
    global _regime_cache
    if _regime_cache["regime"] != "UNKNOWN":
        return {
            "regime": _regime_cache["regime"],
            "confidence": _regime_cache["confidence"],
            "details": _regime_cache.get("details", {}),
        }
    # Try loading from file
    if os.path.exists(REGIME_FILE):
        try:
            with open(REGIME_FILE) as f:
                return json.load(f)
        except Exception as e:
            log_event(f'⚠️ regime: {e}')
    return {"regime": "UNKNOWN", "confidence": 0, "details": {}}


# ── Regime visualization helpers ──

REGIME_COLORS = {
    "TRENDING_UP": "#4caf50",      # green
    "TRENDING_DOWN": "#f44336",    # red
    "CHOPPY": "#ff9800",           # orange
    "HIGH_VOL": "#e91e63",         # pink
    "LOW_VOL": "#2196f3",          # blue
    "NEUTRAL": "#9e9e9e",          # grey
    "UNKNOWN": "#607d8b",          # blue-grey
}

REGIME_EMOJI = {
    "TRENDING_UP": "📈",
    "TRENDING_DOWN": "📉",
    "CHOPPY": "🌊",
    "HIGH_VOL": "🔥",
    "LOW_VOL": "😴",
    "NEUTRAL": "➖",
    "UNKNOWN": "❓",
}

REGIME_LABELS = {
    "TRENDING_UP": "Trending Up",
    "TRENDING_DOWN": "Trending Down",
    "CHOPPY": "Choppy",
    "HIGH_VOL": "High Volatility",
    "LOW_VOL": "Low Volatility",
    "NEUTRAL": "Neutral",
    "UNKNOWN": "Unknown",
}
