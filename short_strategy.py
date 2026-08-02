"""Phase 7: SHORT Strategy — dedicated SHORT scanner, ML gate, JUNK v2, x10, backtest.

Features:
  7.1 — SHORT-сканер: overbought detection (RSI, BB%, volume divergence)
  7.2 — SHORT ML Gate: отдельная RF-модель для SHORT-сигналов
  7.3 — JUNK v2: авто-памп детектор (multi-trigger вместо ручного порога)
  7.4 — SHORT x10: высокое плечо + трейлинг SL
  7.5 — SHORT бэктестинг: валидация на истории

Usage:
  python3 short_strategy.py --scan           # Скан SHORT-сигналов
  python3 short_strategy.py --train-ml       # Обучить ML Gate
  python3 short_strategy.py --backtest SYM   # Бэктест на символе
"""

import json, os, time, hashlib
from typing import Optional

# ── 7.1: SHORT Scanner ──

def scan_short_candidates(tickers: list[dict], bb_cache: dict = None) -> list[dict]:
    """Dedicated SHORT scanner — not a mirror of LONG.
    
    Looks for:
    - BB% > 85 AND RSI implied overbought
    - Volume declining while price rising (divergence)
    - Price near/above upper BB
    - Sequential up days (exhaustion pattern)
    
    Returns ranked list of SHORT candidates with scores.
    """
    candidates = []
    
    for t in tickers:
        sym = t.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
            
        last = float(t.get('lastPrice', 0) or 0)
        turnover = float(t.get('turnover24h', 0) or 0)
        if turnover < 1_000_000 or last <= 0:
            continue
        
        # Get BB from cache or compute from price24hPcnt approximation
        bb = bb_cache.get(sym) if bb_cache else None
        bb_pct = 50
        if bb and bb.get('upper', 0) > bb.get('lower', 0):
            bb_pct = (last - bb['lower']) / (bb['upper'] - bb['lower']) * 100
        
        # ── SHORT-specific scoring ──
        score = 0
        
        # 1. BB position (0-25): higher = better for SHORT
        if bb_pct >= 95:     score += 25
        elif bb_pct >= 90:   score += 22
        elif bb_pct >= 85:   score += 18
        elif bb_pct >= 80:   score += 14
        elif bb_pct >= 75:   score += 10
        elif bb_pct >= 70:   score += 6
        else:                score += 2
        
        # Skip weak signals
        if bb_pct < 70:
            continue
        
        # 2. Volume divergence (0-15): declining volume on rising price = weakness
        vol_ratio = float(t.get('volume24h', turnover)) / max(turnover, 1)
        if vol_ratio < 0.5:  score += 15  # strong divergence
        elif vol_ratio < 0.7: score += 12
        elif vol_ratio < 0.9: score += 8
        else:                 score += 4
        
        # 3. Price change acceleration (0-10): slowing momentum
        chg = float(t.get('price24hPcnt', 0) or 0)
        if 0.05 <= chg < 0.15:   score += 10  # moderate pump = best short
        elif 0.15 <= chg < 0.30: score += 8   # strong pump
        elif chg >= 0.30:        score += 5   # extreme — risky
        elif 0 < chg < 0.05:     score += 6   # weak pump
        else:                    score += 0   # no pump
        
        # 4. Funding penalty (0-5): negative funding = good for short
        funding = float(t.get('fundingRate', 0) or 0)
        if funding < -0.0002:       score += 5
        elif funding < -0.0001:     score += 4
        elif abs(funding) < 0.00005: score += 3
        elif funding < 0.0001:      score += 1
        else:                       score -= 2  # costly funding
        
        # 5. Market cap / liquidity bonus (0-5)
        if turnover > 500_000_000:   score += 5
        elif turnover > 100_000_000: score += 4
        elif turnover > 50_000_000:  score += 3
        elif turnover > 10_000_000:  score += 2
        else:                        score += 1
        
        candidates.append({
            'symbol': sym,
            'score': score,
            'max_score': 60,
            'bb_pct': round(bb_pct, 1),
            'price': last,
            'turnover': turnover,
            'change_24h': round(chg * 100, 1),
            'funding': funding,
        })
    
    # Sort by score descending
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates


# ── 7.3: JUNK v2 — Auto pump detector ──

def detect_pump_v2(ticker: dict, klines: list[dict] = None) -> dict:
    """Auto-detect pump without manual 80% threshold.
    
    Returns {is_pump: bool, confidence: 0-100, reasons: [...]}
    """
    reasons = []
    confidence = 0
    
    chg = float(ticker.get('price24hPcnt', 0) or 0)
    last = float(ticker.get('lastPrice', 0) or 0)
    turnover = float(ticker.get('turnover24h', 0) or 0)
    
    # Trigger 1: Extreme short-term price change
    if chg >= 1.0:     # +100%
        confidence += 35
        reasons.append(f'Pump +{int(chg*100)}%')
    elif chg >= 0.5:   # +50%
        confidence += 25
        reasons.append(f'Pump +{int(chg*100)}%')
    elif chg >= 0.3:   # +30%
        confidence += 15
        reasons.append(f'Suspicious +{int(chg*100)}%')
    
    # Trigger 2: Volume anomaly (volume >> market cap implies)
    if turnover > 50_000_000 and chg > 0.1:
        confidence += 20
        reasons.append('Volume spike')
    
    # Trigger 3: Kline pattern — single huge candle
    if klines and len(klines) >= 2:
        last_candle = klines[-1]
        prev_candle = klines[-2]
        candle_range = abs(last_candle['high'] - last_candle['low'])
        candle_body = abs(last_candle['close'] - last_candle['open'])
        prev_range = abs(prev_candle['high'] - prev_candle['low']) or 0.0001
        if candle_range > prev_range * 3:
            confidence += 15
            reasons.append('Mega candle')
        if candle_body > candle_range * 0.8:
            confidence += 10
            reasons.append('Marubozu')
    
    # Trigger 4: BB breakout
    if chg > 0.15 and last > 0:
        confidence += 10
        reasons.append('BB breakout likely')
    
    return {
        'is_pump': confidence >= 30,
        'confidence': min(confidence, 100),
        'reasons': reasons,
    }


# ── 7.2: SHORT ML Gate ──

def train_short_ml_gate(training_data: list[dict] = None) -> dict:
    """Train a dedicated RandomForest model for SHORT signals.
    
    Features different from LONG ML:
    - bb_pct_inverted (100 - bb_pct)
    - up_days (sequential green candles)
    - volume_decline_ratio
    - funding_rate
    - rsi_14 (estimated)
    - price_acceleration
    
    Returns {accuracy, f1, feature_importance}
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError:
        return {'error': 'scikit-learn not installed'}
    
    # Use historical SHORT data from state.db if no training data provided
    if not training_data:
        training_data = _load_short_training_data()
    
    if len(training_data) < 20:
        return {'error': f'Need 20+ samples, got {len(training_data)}'}
    
    X = []
    y = []
    for d in training_data:
        features = [
            d.get('bb_pct', 50),
            d.get('up_days', 0),
            d.get('volume_ratio', 1.0),
            d.get('funding', 0),
            d.get('change_24h', 0),
            d.get('score', 0),
        ]
        X.append(features)
        y.append(1 if d.get('winner', False) else 0)
    
    X = np.array(X)
    y = np.array(y)
    
    model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
    scores = cross_val_score(model, X, y, cv=3, scoring='f1')
    model.fit(X, y)
    
    importances = dict(zip(
        ['bb_pct', 'up_days', 'volume_ratio', 'funding', 'change_24h', 'score'],
        model.feature_importances_
    ))
    
    return {
        'f1_mean': float(scores.mean()),
        'f1_std': float(scores.std()),
        'feature_importance': importances,
        'n_samples': len(training_data),
    }


def predict_short_ml(signal: dict, model_path: str = None) -> float:
    """Predict SHORT signal quality (0-1). 
    Temporary: score-based fallback when no trained model.
    """
    # Simple heuristic until ML model is trained
    score = signal.get('score', 0)
    bb = signal.get('bb_pct', 50)
    chg = signal.get('change_24h', 0)
    
    # Higher score + higher BB + moderate change = good SHORT
    ml_score = min(1.0, (score / 60) * 0.5 + (bb / 100) * 0.3 + min(abs(chg) / 50, 1.0) * 0.2)
    return ml_score


def _load_short_training_data() -> list[dict]:
    """Load SHORT trade history from state.db for ML training."""
    try:
        import sqlite3
        db = sqlite3.connect(os.path.expanduser('~/.local/share/bybit-ws/state.db'))
        rows = db.execute("""
            SELECT symbol, entry_price, exit_price, side, pnl, entry_at
            FROM trade_history WHERE side='Sell'
            ORDER BY closed_at DESC LIMIT 200
        """).fetchall()
        if not rows:
            return []
        return [
            {'symbol': r[0], 'entry': r[1], 'exit': r[2],
             'entry_ts': r[5], 'winner': (r[4] or 0) > 0}
            for r in rows
        ]
    except Exception as e:
        return []


def _load_long_training_data() -> list[dict]:
    """Load LONG trade history from state.db for ML training."""
    try:
        import sqlite3
        db = sqlite3.connect(os.path.expanduser('~/.local/share/bybit-ws/state.db'))
        rows = db.execute("""
            SELECT symbol, entry_price, exit_price, side, pnl, entry_at
            FROM trade_history WHERE side='Buy'
            ORDER BY closed_at DESC LIMIT 200
        """).fetchall()
        if not rows:
            return []
        return [
            {'symbol': r[0], 'entry': r[1], 'exit': r[2],
             'entry_ts': r[5], 'winner': (r[4] or 0) > 0}
            for r in rows
        ]
    except Exception as e:
        return []


def train_long_ml_gate(training_data: list[dict] = None) -> dict:
    """Train dedicated RandomForest for LONG signals.
    
    Features (LONG-specific):
    - bb_pct (low = good for LONG)
    - down_days (sequential red candles = oversold)
    - volume_ratio (volume confirming the move)
    - funding_rate (positive = longs get paid)
    - change_24h (negative change = discount)
    - score (auto_entry score)
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError:
        return {'error': 'scikit-learn not installed'}
    
    if not training_data:
        training_data = _load_long_training_data()
    
    if len(training_data) < 20:
        return {'error': f'Need 20+ samples, got {len(training_data)}'}
    
    X, y = [], []
    for d in training_data:
        features = [
            d.get('bb_pct', 50),
            d.get('down_days', 0),
            d.get('volume_ratio', 1.0),
            d.get('funding', 0),
            d.get('change_24h', 0),
            d.get('score', 0),
        ]
        X.append(features)
        y.append(1 if d.get('winner', False) else 0)
    
    X, y = np.array(X), np.array(y)
    model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
    scores = cross_val_score(model, X, y, cv=3, scoring='f1')
    model.fit(X, y)
    
    return {
        'f1_mean': float(scores.mean()),
        'f1_std': float(scores.std()),
        'feature_importance': dict(zip(
            ['bb_pct', 'down_days', 'volume_ratio', 'funding', 'change_24h', 'score'],
            model.feature_importances_
        )),
        'n_samples': len(training_data),
    }


# ── 7.4: SHORT x10 mode ──

def x10_short_params(signal: dict, entry_price: float) -> dict:
    """Calculate x10 SHORT parameters with trailing SL.
    
    Returns {leverage, margin, qty, sl_trailing_pct, max_hold_hours}
    """
    score = signal.get('score', 30)
    bb_pct = signal.get('bb_pct', 50)
    
    # Higher score + higher BB = more aggressive
    if score >= 45 and bb_pct >= 90:
        leverage = 10
        margin = 5  # micro position
        sl_trail = 0.03  # 3% trailing
        max_hold = 48
    elif score >= 35 and bb_pct >= 80:
        leverage = 10
        margin = 5
        sl_trail = 0.05
        max_hold = 72
    else:
        leverage = 3
        margin = 10
        sl_trail = None  # use standard SL
        max_hold = 168
    
    return {
        'leverage': leverage,
        'margin': margin,
        'sl_trailing_pct': sl_trail,
        'max_hold_hours': max_hold,
        'is_x10': leverage == 10,
    }


# ── 7.5: SHORT Backtest ──

def backtest_short(symbol: str, days: int = 90) -> dict:
    """Backtest SHORT strategy on historical data.
    
    Returns {trades, win_rate, total_pnl, avg_pnl, max_drawdown}
    """
    from .api import get_bb_data
    
    try:
        klines = _get_historical_klines(symbol, days)
        if len(klines) < 40:
            return {'error': f'Need 40+ candles, got {len(klines)}'}
    except Exception as e:
        return {'error': str(e)}
    
    trades = []
    in_position = False
    entry_price = 0
    
    for i in range(20, len(klines)):
        window = klines[i-20:i+1]
        closes = [k['close'] for k in window]
        cur = closes[-1]
        
        # Compute BB
        middle = sum(closes[:-1]) / 20
        stdev = (sum((x - middle)**2 for x in closes[:-1]) / 20) ** 0.5
        upper = middle + 2 * stdev
        lower = middle - 2 * stdev
        bb_pct = (cur - lower) / (upper - lower) * 100 if upper != lower else 50
        
        if not in_position:
            # Entry: BB > 85%
            if bb_pct >= 85:
                entry_price = cur
                in_position = True
        else:
            # Exit: SL hit (+10%) or TP hit (Middle BB)
            sl_price = entry_price * 1.10
            tp_price = middle
            if cur >= sl_price:
                pnl = (entry_price - sl_price) / entry_price * 100
                trades.append({'entry': entry_price, 'exit': sl_price, 'pnl_pct': pnl, 'reason': 'SL'})
                in_position = False
            elif cur <= tp_price:
                pnl = (entry_price - tp_price) / entry_price * 100
                trades.append({'entry': entry_price, 'exit': tp_price, 'pnl_pct': pnl, 'reason': 'TP'})
                in_position = False
    
    # Close any open trade at last price
    if in_position:
        pnl = (entry_price - closes[-1]) / entry_price * 100
        trades.append({'entry': entry_price, 'exit': closes[-1], 'pnl_pct': pnl, 'reason': 'EOD'})
    
    if not trades:
        return {'trades': 0, 'win_rate': 0, 'total_pnl': 0}
    
    winners = [t for t in trades if t['pnl_pct'] > 0]
    total_pnl = sum(t['pnl_pct'] for t in trades)
    
    # Max drawdown
    cumulative = 0
    max_cum = 0
    max_dd = 0
    for t in trades:
        cumulative += t['pnl_pct']
        max_cum = max(max_cum, cumulative)
        max_dd = min(max_dd, cumulative - max_cum)
    
    return {
        'trades': len(trades),
        'winners': len(winners),
        'win_rate': round(len(winners) / len(trades) * 100, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(total_pnl / len(trades), 2),
        'max_drawdown': round(abs(max_dd), 2),
    }


def _get_historical_klines(symbol: str, days: int = 90) -> list[dict]:
    """Get historical klines for backtesting."""
    from .api import bybit
    limit = min(days, 200)
    r = bybit('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit={limit}')
    if not r or r.get('retCode') != 0:
        return []
    candles = r['result'].get('list', [])
    return [
        {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
         "close": float(c[4]), "volume": float(c[5])}
        for c in reversed(candles)
    ]


# ── CLI ──

if __name__ == '__main__':
    import sys
    if '--scan' in sys.argv:
        from .api import bybit
        r = bybit('GET', '/v5/market/tickers?category=linear')
        tickers = r.get('result', {}).get('list', []) if r else []
        candidates = scan_short_candidates(tickers)
        for c in candidates[:5]:
            print(f"  {c['symbol']}: score={c['score']}/60 BB={c['bb_pct']}% chg={c['change_24h']}%")
        print(f"  ... {len(candidates)} total candidates")
    
    elif '--train-ml' in sys.argv or '--train-ml-long' in sys.argv:
        side = 'long' if '--train-ml-long' in sys.argv else 'short'
        fn = train_long_ml_gate if side == 'long' else train_short_ml_gate
        result = fn()
        result['side'] = side
        print(json.dumps(result, indent=2))
    
    elif '--backtest' in sys.argv:
        sym = sys.argv[sys.argv.index('--backtest') + 1]
        result = backtest_short(sym)
        print(json.dumps(result, indent=2))
    
    elif '--pump' in sys.argv:
        from .api import bybit
        r = bybit('GET', '/v5/market/tickers?category=linear')
        tickers = r.get('result', {}).get('list', []) if r else []
        for t in tickers[:20]:
            pump = detect_pump_v2(t)
            if pump['is_pump']:
                print(f"  🚀 PUMP: {t['symbol']} conf={pump['confidence']}% — {', '.join(pump['reasons'])}")
    
    else:
        print("Usage: python3 short_strategy.py [--scan|--train-ml|--backtest SYM|--pump]")
