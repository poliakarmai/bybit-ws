"""Unified Exchange API — Phase 6.5.

Abstracts Bybit, Binance, OKX behind a single interface.
Feature flag: BYBIT_EXCHANGE=bybit|binance|okx (default: bybit)

Architecture:
  ExchangeAdapter (ABC)
    ├── BybitAdapter   — wraps existing api.py
    ├── BinanceAdapter — Binance Futures REST API
    └── OKXAdapter     — OKX Futures REST API

Used by: main_async.py, auto_entry.py, auto_short.py, auto_sl.py, auto_tp.py
"""

import os, json, time, hmac, hashlib
from abc import ABC, abstractmethod
from typing import Optional

import httpx

# ── Feature flag ──
EXCHANGE = os.environ.get('BYBIT_EXCHANGE', 'bybit').lower()

# ── Base Adapter ──

class ExchangeAdapter(ABC):
    """Unified interface for any futures exchange."""

    name: str = "unknown"

    @abstractmethod
    def get_tickers(self) -> list[dict]:
        """All linear futures tickers with 24h data."""
        ...

    @abstractmethod
    def get_klines(self, symbol: str, interval: str = "D", limit: int = 10) -> list[dict]:
        """OHLCV candles. Returns list of {open, high, low, close, volume}."""
        ...

    @abstractmethod
    def get_positions(self) -> dict[str, dict]:
        """Open positions: {symbol: {entry, mark, side, size, leverage, sl, tp}}."""
        ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", price: float = None,
                    reduce_only: bool = False) -> dict:
        """Place order. Returns {order_id, status}."""
        ...

    @abstractmethod
    def set_trading_stop(self, symbol: str, side: str, sl: float = None, tp: float = None) -> dict:
        """Set SL/TP for position."""
        ...

    @abstractmethod
    def get_balance(self) -> dict:
        """Account balance {total, available, unrealized_pnl}."""
        ...

    @abstractmethod
    def get_bb(self, symbol: str, interval: str = "D", period: int = 20, std: float = 2.0) -> dict:
        """Bollinger Bands: {upper, middle, lower, bb_pos, bb_width}."""
        ...


# ── Utility: compute BB from klines ──

def _compute_bb(closes: list[float], period: int = 20, std: float = 2.0) -> dict:
    """Compute Bollinger Bands from close prices. Returns {upper, middle, lower, bb_pos, bb_width}."""
    if len(closes) < period:
        return {}
    window = closes[-period:]
    middle = sum(window) / len(window)
    variance = sum((x - middle) ** 2 for x in window) / len(window)
    stdev = variance ** 0.5
    upper = middle + std * stdev
    lower = middle - std * stdev
    last = closes[-1]
    bb_pos = (last - lower) / (upper - lower) * 100 if upper != lower else 50
    bb_width = (upper - lower) / middle * 100 if middle > 0 else 0
    return {"upper": upper, "middle": middle, "lower": lower, "bb_pos": bb_pos, "bb_width": bb_width}


# ═══════════════════════════════════════════
# Bybit Adapter
# ═══════════════════════════════════════════

class BybitAdapter(ExchangeAdapter):
    """Wraps existing bybit-ws api.py. Production adapter."""

    name = "bybit"

    def _import_bybit(self):
        """Import bybit helper — works both as package and standalone."""
        try:
            from .api import bybit as b, fetch_positions as fp
        except ImportError:
            from api import bybit as b, fetch_positions as fp
        return b, fp

    def get_tickers(self) -> list[dict]:
        b, _ = self._import_bybit()
        r = b('GET', '/v5/market/tickers?category=linear')
        return r.get('result', {}).get('list', []) if r else []

    def get_klines(self, symbol: str, interval: str = "D", limit: int = 10) -> list[dict]:
        b, _ = self._import_bybit()
        r = b('GET', f'/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}')
        if not r or r.get('retCode') != 0:
            return []
        candles = r['result'].get('list', [])
        # Bybit returns [ts, open, high, low, close, volume, turnover] — newest first
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(candles)  # oldest first
        ]

    def get_positions(self) -> dict[str, dict]:
        _, fp = self._import_bybit()
        return fp()

    def place_order(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", price: float = None,
                    reduce_only: bool = False) -> dict:
        b, _ = self._import_bybit()
        body = {
            'category': 'linear', 'symbol': symbol, 'side': side,
            'orderType': order_type, 'qty': str(qty),
            'timeInForce': 'GTC' if order_type == 'Limit' else 'IOC',
        }
        if price and order_type == 'Limit':
            body['price'] = str(price)
        if reduce_only:
            body['reduceOnly'] = True
        r = b('POST', '/v5/order/create', body)
        return {'order_id': r.get('result', {}).get('orderId'), 'status': 'ok' if r and r.get('retCode') == 0 else 'error'}

    def set_trading_stop(self, symbol: str, side: str, sl: float = None, tp: float = None) -> dict:
        b, fp = self._import_bybit()
        pos = fp().get(symbol, {})
        body = {'category': 'linear', 'symbol': symbol, 'positionIdx': pos.get('positionIdx', 0)}
        if sl:
            body['stopLoss'] = str(sl)
            body['slTriggerBy'] = 'MarkPrice'
        if tp:
            body['takeProfit'] = str(tp)
            body['tpTriggerBy'] = 'MarkPrice'
        r = b('POST', '/v5/position/trading-stop', body)
        return {'status': 'ok' if r and r.get('retCode') == 0 else 'error'}

    def get_balance(self) -> dict:
        b, _ = self._import_bybit()
        r = b('GET', '/v5/account/wallet-balance?accountType=UNIFIED')
        if not r or r.get('retCode') != 0:
            return {}
        coins = r['result'].get('list', [{}])[0].get('coin', [])
        usdt = next((c for c in coins if c['coin'] == 'USDT'), {})
        return {
            'total': float(usdt.get('walletBalance', 0)),
            'available': float(usdt.get('availableToWithdraw', 0)),
            'unrealized_pnl': float(usdt.get('unrealisedPnl', 0)),
        }

    def get_bb(self, symbol: str, interval: str = "D", period: int = 20, std: float = 2.0) -> dict:
        klines = self.get_klines(symbol, interval, limit=period + 5)
        if not klines:
            return {}
        closes = [k['close'] for k in klines]
        return _compute_bb(closes, period, std)


# ═══════════════════════════════════════════
# Binance Adapter
# ═══════════════════════════════════════════

class BinanceAdapter(ExchangeAdapter):
    """Binance Futures REST API (USDⓈ-M)."""

    name = "binance"
    BASE_URL = "https://fapi.binance.com"

    def __init__(self):
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=15)
        self._key = os.environ.get('BINANCE_API_KEY', '')
        self._secret = os.environ.get('BINANCE_API_SECRET', '')

    def _sign(self, params: dict) -> dict:
        if not self._key:
            return params
        params['timestamp'] = int(time.time() * 1000)
        query = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
        params['signature'] = hmac.new(
            self._secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return params

    def _get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        params = params or {}
        if signed:
            params = self._sign(params)
        headers = {'X-MBX-APIKEY': self._key} if signed and self._key else {}
        r = self._client.get(path, params=params, headers=headers)
        return r.json() if r.status_code == 200 else {}

    def get_tickers(self) -> list[dict]:
        data = self._get('/fapi/v1/ticker/24hr')
        if not isinstance(data, list):
            return []
        return [
            {'symbol': t['symbol'], 'lastPrice': t['lastPrice'],
             'turnover24h': t.get('quoteVolume', '0'),
             'price24hPcnt': str((float(t['lastPrice']) / float(t['prevClosePrice']) - 1) if float(t.get('prevClosePrice', 1)) > 0 else 0),
             'fundingRate': '0'}
            for t in data if t['symbol'].endswith('USDT')
        ]

    def get_klines(self, symbol: str, interval: str = "D", limit: int = 10) -> list[dict]:
        binance_interval = {'D': '1d', 'W': '1w', '4h': '4h', '1h': '1h', '15m': '15m', '5m': '5m'}.get(interval, '1d')
        data = self._get('/fapi/v1/klines', {'symbol': symbol, 'interval': binance_interval, 'limit': limit})
        if not isinstance(data, list):
            return []
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "volume": float(c[5])}
            for c in data
        ]

    def get_positions(self) -> dict[str, dict]:
        if not self._key:
            return {}
        data = self._get('/fapi/v2/positionRisk', signed=True)
        if not isinstance(data, list):
            return {}
        result = {}
        for p in data:
            qty = abs(float(p.get('positionAmt', 0)))
            if qty <= 0:
                continue
            entry = float(p.get('entryPrice', 0))
            mark = float(p.get('markPrice', 0))
            side = 'Buy' if float(p.get('positionAmt', 0)) > 0 else 'Sell'
            result[p['symbol']] = {
                'symbol': p['symbol'], 'entry': entry, 'mark': mark,
                'side': side, 'size': qty,
                'leverage': float(p.get('leverage', 0)),
                'stopLoss': p.get('stopLoss', ''),
                'takeProfit': p.get('takeProfit', ''),
                'positionIdx': 0,
            }
        return result

    def place_order(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", price: float = None,
                    reduce_only: bool = False) -> dict:
        if not self._key:
            return {'status': 'error', 'reason': 'no API key'}
        params = {
            'symbol': symbol,
            'side': 'BUY' if side == 'Buy' else 'SELL',
            'type': order_type.upper(),
            'quantity': str(qty),
        }
        if order_type == 'Limit' and price:
            params['price'] = str(price)
            params['timeInForce'] = 'GTC'
        if reduce_only:
            params['reduceOnly'] = 'true'
        r = self._client.post('/fapi/v1/order', data=self._sign(params),
                              headers={'X-MBX-APIKEY': self._key})
        data = r.json() if r.status_code == 200 else {}
        return {'order_id': data.get('orderId'), 'status': 'ok' if data.get('orderId') else 'error'}

    def set_trading_stop(self, symbol: str, side: str, sl: float = None, tp: float = None) -> dict:
        if not self._key:
            return {'status': 'error'}
        result = {'status': 'ok'}
        pos_side = 'LONG' if side == 'Buy' else 'SHORT'
        if sl:
            sl_side = 'SELL' if side == 'Buy' else 'BUY'
            params = self._sign({
                'symbol': symbol, 'side': sl_side, 'type': 'STOP_MARKET',
                'stopPrice': str(sl), 'closePosition': 'true',
                'positionSide': pos_side,
            })
            r = self._client.post('/fapi/v1/order', data=params, headers={'X-MBX-APIKEY': self._key})
            if r.status_code != 200:
                result['status'] = 'error'
        return result

    def get_balance(self) -> dict:
        if not self._key:
            return {}
        data = self._get('/fapi/v2/balance', signed=True)
        if not isinstance(data, list):
            return {}
        usdt = next((c for c in data if c['asset'] == 'USDT'), {})
        return {
            'total': float(usdt.get('balance', 0)),
            'available': float(usdt.get('availableBalance', 0)),
            'unrealized_pnl': float(usdt.get('crossUnPnl', 0)),
        }

    def get_bb(self, symbol: str, interval: str = "D", period: int = 20, std: float = 2.0) -> dict:
        klines = self.get_klines(symbol, interval, limit=period + 5)
        if not klines:
            return {}
        closes = [k['close'] for k in klines]
        return _compute_bb(closes, period, std)


# ═══════════════════════════════════════════
# OKX Adapter
# ═══════════════════════════════════════════

class OKXAdapter(ExchangeAdapter):
    """OKX Futures REST API."""

    name = "okx"
    BASE_URL = "https://www.okx.com"

    def __init__(self):
        self._client = httpx.Client(base_url=self.BASE_URL, timeout=15)
        self._key = os.environ.get('OKX_API_KEY', '')
        self._secret = os.environ.get('OKX_API_SECRET', '')
        self._passphrase = os.environ.get('OKX_PASSPHRASE', '')

    def _sign(self, method: str, path: str, body: str = '') -> dict:
        if not self._key:
            return {}
        ts = str(int(time.time()))
        sign_str = ts + method + path + body
        sign = hmac.new(self._secret.encode(), sign_str.encode(), hashlib.sha256).digest()
        import base64
        sign = base64.b64encode(sign).decode()
        return {
            'OK-ACCESS-KEY': self._key,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': self._passphrase,
            'Content-Type': 'application/json',
        }

    def get_tickers(self) -> list[dict]:
        r = self._client.get('/api/v5/market/tickers?instType=SWAP')
        data = r.json() if r.status_code == 200 else {}
        tickers = data.get('data', [])
        return [
            {'symbol': t['instId'].replace('-USDT-SWAP', 'USDT'),
             'lastPrice': t['last'], 'turnover24h': t.get('volCcy24h', '0'),
             'price24hPcnt': str(float(t.get('open24h', 1)) and float(t['last']) / float(t['open24h']) - 1),
             'fundingRate': t.get('fundingRate', '0')}
            for t in tickers if 'USDT-SWAP' in t.get('instId', '')
        ]

    def get_klines(self, symbol: str, interval: str = "D", limit: int = 10) -> list[dict]:
        okx_interval = {'D': '1D', 'W': '1W', '4h': '4H', '1h': '1H', '15m': '15m', '5m': '5m'}.get(interval, '1D')
        okx_symbol = symbol.replace('USDT', '-USDT-SWAP')
        r = self._client.get(f'/api/v5/market/candles?instId={okx_symbol}&bar={okx_interval}&limit={limit}')
        data = r.json() if r.status_code == 200 else {}
        candles = data.get('data', [])
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "volume": float(c[5])}
            for c in reversed(candles)  # OKX returns newest first
        ]

    def get_positions(self) -> dict[str, dict]:
        if not self._key:
            return {}
        r = self._client.get('/api/v5/account/positions?instType=SWAP',
                              headers=self._sign('GET', '/api/v5/account/positions?instType=SWAP'))
        data = r.json() if r.status_code == 200 else {}
        result = {}
        for p in data.get('data', []):
            qty = abs(float(p.get('pos', 0)))
            if qty <= 0:
                continue
            symbol = p['instId'].replace('-USDT-SWAP', 'USDT')
            result[symbol] = {
                'symbol': symbol, 'entry': float(p.get('avgPx', 0)),
                'mark': float(p.get('markPx', 0)),
                'side': 'Buy' if p.get('posSide') == 'long' else 'Sell',
                'size': qty, 'leverage': float(p.get('lever', 0)),
                'stopLoss': '', 'takeProfit': '', 'positionIdx': 0,
            }
        return result

    def place_order(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", price: float = None,
                    reduce_only: bool = False) -> dict:
        if not self._key:
            return {'status': 'error', 'reason': 'no API key'}
        okx_symbol = symbol.replace('USDT', '-USDT-SWAP')
        okx_side = 'buy' if side == 'Buy' else 'sell'
        body = {
            'instId': okx_symbol, 'tdMode': 'cross',
            'side': okx_side, 'ordType': order_type.lower(),
            'sz': str(qty),
        }
        if price and order_type == 'Limit':
            body['px'] = str(price)
        r = self._client.post('/api/v5/trade/order', json=body,
                               headers=self._sign('POST', '/api/v5/trade/order', json.dumps(body)))
        data = r.json() if r.status_code == 200 else {}
        return {'order_id': data.get('data', [{}])[0].get('ordId'), 'status': 'ok' if data.get('code') == '0' else 'error'}

    def set_trading_stop(self, symbol: str, side: str, sl: float = None, tp: float = None) -> dict:
        if not self._key:
            return {'status': 'error'}
        okx_symbol = symbol.replace('USDT', '-USDT-SWAP')
        okx_side = 'long' if side == 'Buy' else 'short'
        body = {'instId': okx_symbol, 'tdMode': 'cross', 'posSide': okx_side}
        if sl:
            body['slTriggerPx'] = str(sl)
            body['slOrdPx'] = str(sl * 0.99 if side == 'Buy' else sl * 1.01)
        if tp:
            body['tpTriggerPx'] = str(tp)
            body['tpOrdPx'] = str(tp)
        r = self._client.post('/api/v5/trade/order-algo', json=body,
                               headers=self._sign('POST', '/api/v5/trade/order-algo', json.dumps(body)))
        data = r.json() if r.status_code == 200 else {}
        return {'status': 'ok' if data.get('code') == '0' else 'error'}

    def get_balance(self) -> dict:
        if not self._key:
            return {}
        r = self._client.get('/api/v5/account/balance',
                              headers=self._sign('GET', '/api/v5/account/balance'))
        data = r.json() if r.status_code == 200 else {}
        details = data.get('data', [{}])[0].get('details', [])
        usdt = next((c for c in details if c['ccy'] == 'USDT'), {})
        return {
            'total': float(usdt.get('eq', 0)),
            'available': float(usdt.get('availBal', 0)),
            'unrealized_pnl': float(usdt.get('upl', 0)),
        }

    def get_bb(self, symbol: str, interval: str = "D", period: int = 20, std: float = 2.0) -> dict:
        klines = self.get_klines(symbol, interval, limit=period + 5)
        if not klines:
            return {}
        closes = [k['close'] for k in klines]
        return _compute_bb(closes, period, std)


# ═══════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════

_adapters = {
    'bybit': BybitAdapter,
    'binance': BinanceAdapter,
    'okx': OKXAdapter,
}

def get_exchange(name: str = None) -> ExchangeAdapter:
    """Get exchange adapter by name. Default from BYBIT_EXCHANGE env var."""
    name = name or EXCHANGE
    if name not in _adapters:
        raise ValueError(f"Unknown exchange: {name}. Use: {list(_adapters.keys())}")
    return _adapters[name]()


# Global instance (lazy)
_exchange: Optional[ExchangeAdapter] = None

def exchange() -> ExchangeAdapter:
    """Get the configured exchange adapter (singleton)."""
    global _exchange
    if _exchange is None:
        _exchange = get_exchange()
    return _exchange
