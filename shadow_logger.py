"""Shadow logger — фиксирует BB-кандидатов + L2 orderbook imbalance для post-mortem анализа.

По вердикту Петра (18.09.2026): НЕ симулирует SL/TP, НЕ влияет на торговлю, НЕ кормит self-learn.
Просто пишет сигнал в момент возникновения + imbalance стакана, чтобы через 48ч понять,
почему шорты входят и сразу стопятся (токсичный стакан / фитиль / микроструктура).

Вывод: ~/.local/share/bybit-ws/shadow_candidates.jsonl (append-only).
Дальше по данным — rule-based Execution Gate (хардкод-фильтр), НЕ ML.
"""
import json
import os
import time

from .api import bybit
from .alerts import log_event

DATA_DIR = os.path.expanduser("~/.local/share/bybit-ws")
LOG_FILE = os.path.join(DATA_DIR, "shadow_candidates.jsonl")

_ob_cache = {}      # symbol -> (ts, imbalance)
_OB_TTL = 300       # кеш orderbook imbalance 5 мин (не спамим Bybit)


def get_orderbook_imbalance(symbol, depth=25):
    """imbalance = (bid_vol − ask_vol) / (bid_vol + ask_vol). +1 = чисто bid, −1 = чисто ask.

    Для SHORT-входа: сильный ask (imbalance < 0) = продавцы давят = вход по тренду.
    Сильный bid (imbalance > 0) = покупатели давят = вход ПРОТИВ стакана = риск мгновенного SL.
    """
    now = time.time()
    cached = _ob_cache.get(symbol)
    if cached and now - cached[0] < _OB_TTL:
        return cached[1]
    try:
        data = bybit('GET', f'/v5/market/orderbook?category=linear&symbol={symbol}&limit={depth}')
        if not data or data.get('retCode') != 0:
            return None
        bids = data['result'].get('b', [])
        asks = data['result'].get('a', [])
        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        if bid_vol + ask_vol <= 0:
            return None
        imb = round((bid_vol - ask_vol) / (bid_vol + ask_vol), 4)
        _ob_cache[symbol] = (now, imb)
        return imb
    except Exception:
        return None


def log_candidate(symbol, side, entry_price, bb_pct=None, rsi=None, score=None,
                  funding=None, extra=None):
    """Записать кандидата в JSONL (append-only)."""
    rec = {
        'ts': time.time(),
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'bb_pct': bb_pct,
        'rsi': rsi,
        'score': score,
        'funding': funding,
        'ob_imbalance': get_orderbook_imbalance(symbol),
        'extra': extra or {},
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception as e:
        log_event(f'⚠️ shadow_logger: {e}')
