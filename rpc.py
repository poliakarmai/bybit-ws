"""RPC-сервер bybit-ws — HTTP JSON API для дашборда и внешних потребителей.

Запускается как фоновый поток в main.py.
Порт: 8766 (рядом с дашбордом 8765).

Endpoints:
    GET  /rpc/all         — все данные одним запросом (позиции, ордера, алерты, метрики, трейды)
    GET  /rpc/positions   — открытые позиции
    GET  /rpc/orders      — активные ордера
    GET  /rpc/health      — статус монитора (alive, uptime, cycle_count)
    GET  /rpc/trades      — трейд-лог (trades.jsonl)
    GET  /rpc/alerts      — последние алерты
    GET  /rpc/metrics     — метрики (daily)
    GET  /health          — алиас на /rpc/health
    GET  /positions       — алиас на /rpc/positions
    GET  /orders          — алиас на /rpc/orders
    GET  /metrics         — алиас на /rpc/metrics
    POST /scan            — запустить GridSignal-сканер
    POST /enter           — ручной вход в позицию
    POST /close           — закрыть позицию
"""

import json
import os
import time
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
HOME = Path.home()
BYBIT_CLI = str(HOME / ".local" / "bin" / "bybit")
GRIDSIGNAL_SCANNER = str(HOME / ".local" / "bin" / "gridsignal_scanner.py")

# Глобальное состояние (обновляется main-потоком)
rpc_state = {
    "alive": False,
    "started_at": time.time(),
    "cycle_count": 0,
    "last_cycle": 0.0,
    "cycle_duration": 0.0,
}


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _json_response(handler, data, status=200):
    body = json.dumps(data, ensure_ascii=False, default=str)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body.encode())


def _run_bybit(*args, timeout=30) -> dict:
    """Выполнить bybit CLI и вернуть JSON."""
    try:
        result = subprocess.run(
            [BYBIT_CLI, *args],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return {'retCode': -1, 'retMsg': result.stderr.strip()[:200] or f'exit {result.returncode}'}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {'retCode': -1, 'retMsg': f'JSON parse error: {result.stdout[:200]}'}
    except subprocess.TimeoutExpired:
        return {'retCode': -1, 'retMsg': 'bybit CLI timeout'}
    except FileNotFoundError:
        return {'retCode': -1, 'retMsg': 'bybit CLI not found'}
    except Exception as e:
        return {'retCode': -1, 'retMsg': str(e)[:200]}


def _tick_size(price: float) -> float:
    """Определить шаг цены для округления."""
    if price < 1:
        return 0.0001
    elif price < 10:
        return 0.001
    elif price < 100:
        return 0.01
    elif price < 1000:
        return 0.1
    else:
        return 1.0


def _round_price(price: float) -> float:
    """Округлить цену до правильного тика."""
    tick = _tick_size(price)
    return round(round(price / tick) * tick, 8)


def _get_position(symbol: str) -> dict | None:
    """Получить информацию о позиции по символу."""
    data = _run_bybit('raw', 'GET',
                      f'/v5/position/list?category=linear&symbol={symbol}')
    if data.get('retCode') != 0:
        return None
    for p in data.get('result', {}).get('list', []):
        if float(p.get('size', 0)) > 0:
            return {
                'symbol': p['symbol'],
                'side': p['side'],
                'size': float(p['size']),
                'entry': float(p['avgPrice']),
                'mark': float(p['markPrice']),
                'positionIdx': int(p.get('positionIdx', 0)),
                'leverage': float(p.get('leverage', '1')),
            }
    return None


class RPCHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # тихий режим

    # ── CORS preflight ──────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ─────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.rstrip("/") or "/"

        # Алиасы (короткие пути из DESIGN.md)
        if path == "/health":
            return self._handle_health()
        if path == "/positions":
            return self._handle_positions()
        if path == "/orders":
            return self._handle_orders()
        if path == "/metrics":
            return self._handle_metrics()

        if path == "/rpc/all":
            self._handle_all()
        elif path == "/rpc/positions":
            self._handle_positions()
        elif path == "/rpc/orders":
            self._handle_orders()
        elif path == "/rpc/health":
            self._handle_health()
        elif path == "/rpc/trades":
            self._handle_trades()
        elif path == "/rpc/alerts":
            self._handle_alerts()
        elif path == "/rpc/metrics":
            self._handle_metrics()
        elif path == "/rpc" or path == "/":
            self._handle_index()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')

    # ── POST ────────────────────────────────────────────────────
    def do_POST(self):
        path = self.path.rstrip("/") or "/"

        # Читаем тело запроса
        content_length = int(self.headers.get('Content-Length', 0))
        body_raw = self.rfile.read(content_length) if content_length > 0 else b''

        try:
            body = json.loads(body_raw) if body_raw else {}
        except json.JSONDecodeError:
            return _json_response(self, {'error': 'Invalid JSON body'}, 400)

        if path == "/scan":
            self._handle_scan(body)
        elif path == "/enter":
            self._handle_enter(body)
        elif path == "/close":
            self._handle_close(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')

    # ═══════════════════════════════════════════════════════════════
    # GET handlers
    # ═══════════════════════════════════════════════════════════════

    def _handle_index(self):
        _json_response(self, {
            "service": "bybit-ws-rpc",
            "version": "2.0",
            "endpoints": [
                "/rpc/all", "/rpc/positions", "/rpc/orders",
                "/rpc/health", "/rpc/trades", "/rpc/alerts", "/rpc/metrics",
                "/health", "/positions", "/orders", "/metrics",
                "POST /scan", "POST /enter", "POST /close",
            ]
        })

    def _handle_all(self):
        """Все данные одним запросом — для дашборда."""
        positions_raw = _load_json(DATA_DIR / "positions.json")
        orders_raw = _load_json(DATA_DIR / "orders.json")
        metrics = _load_json(DATA_DIR / "metrics.json")

        positions = []
        if isinstance(positions_raw, dict):
            for sym, p in positions_raw.items():
                if isinstance(p, dict):
                    p = dict(p)
                    p["symbol"] = sym
                positions.append(p)

        orders = []
        if isinstance(orders_raw, dict):
            for oid, o in orders_raw.items():
                if isinstance(o, dict):
                    o = dict(o)
                    if "symbol" not in o:
                        o["symbol"] = oid.split("_")[0] if "_" in oid else "???"
                orders.append(o)

        alerts = []
        af = DATA_DIR / "new_alerts.txt"
        if af.exists():
            for line in _read_file(af).split("\n"):
                line = line.strip()
                if line:
                    atype = "info"
                    if any(c in line for c in ["🛑", "⚠️", "🚀", "💸"]):
                        atype = "stop"
                    elif any(c in line for c in ["📌", "📈", "📉"]):
                        atype = "entry"
                    elif "🎯" in line:
                        atype = "tp"
                    alerts.append({"type": atype, "msg": line})

        trades = []
        tf = DATA_DIR / "trades.jsonl"
        if tf.exists():
            with open(tf) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except Exception:
                            pass
            trades = trades[-50:]

        _json_response(self, {
            "positions": positions,
            "orders": orders,
            "alerts": alerts,
            "metrics": metrics,
            "trades": trades,
            "monitor": {
                "alive": rpc_state["alive"],
                "uptime": int(time.time() - rpc_state["started_at"]),
                "cycle_count": rpc_state["cycle_count"],
            },
        })

    def _handle_positions(self):
        positions = _load_json(DATA_DIR / "positions.json")
        result = []
        if isinstance(positions, dict):
            for sym, p in positions.items():
                if isinstance(p, dict):
                    p = dict(p)
                    p["symbol"] = sym
                result.append(p)
        _json_response(self, result)

    def _handle_orders(self):
        orders_raw = _load_json(DATA_DIR / "orders.json")
        result = []
        if isinstance(orders_raw, dict):
            for oid, o in orders_raw.items():
                if isinstance(o, dict):
                    o = dict(o)
                    if "symbol" not in o:
                        o["symbol"] = oid.split("_")[0] if "_" in oid else "???"
                result.append(o)
        _json_response(self, result)

    def _handle_health(self):
        alive = False
        hf = DATA_DIR / "health.txt"
        if hf.exists():
            try:
                alive = time.time() - float(_read_file(hf)) < 180
            except Exception:
                pass

        _json_response(self, {
            "status": "alive" if alive else "stale",
            "alive": alive,
            "uptime": int(time.time() - rpc_state["started_at"]),
            "cycle_count": rpc_state["cycle_count"],
            "last_cycle": rpc_state["last_cycle"],
            "cycle_duration": rpc_state["cycle_duration"],
        })

    def _handle_trades(self):
        trades = []
        tf = DATA_DIR / "trades.jsonl"
        if tf.exists():
            with open(tf) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except Exception:
                            pass

        limit = 100
        if "?limit=" in self.path:
            try:
                limit = int(self.path.split("limit=")[1].split("&")[0])
            except Exception:
                pass

        _json_response(self, trades[-limit:])

    def _handle_alerts(self):
        alerts = []
        af = DATA_DIR / "new_alerts.txt"
        if af.exists():
            for line in _read_file(af).split("\n"):
                line = line.strip()
                if line:
                    alerts.append({"msg": line})
        _json_response(self, alerts[-30:])

    def _handle_metrics(self):
        metrics = _load_json(DATA_DIR / "metrics.json")
        _json_response(self, metrics)

    # ═══════════════════════════════════════════════════════════════
    # POST handlers
    # ═══════════════════════════════════════════════════════════════

    def _handle_scan(self, body: dict):
        """POST /scan — запустить GridSignal-сканер.

        Принимает: {"mode": "long|short", "limit": 5}
        Возвращает: список сигналов с метриками.
        """
        mode = body.get('mode', 'long')
        if mode not in ('long', 'short'):
            return _json_response(self, {
                'error': 'Invalid mode',
                'detail': "mode must be 'long' or 'short'",
                'received': mode,
            }, 400)

        try:
            limit = int(body.get('limit', 5))
        except (ValueError, TypeError):
            return _json_response(self, {
                'error': 'Invalid limit',
                'detail': 'limit must be an integer',
            }, 400)

        if limit < 1 or limit > 20:
            return _json_response(self, {
                'error': 'Invalid limit',
                'detail': 'limit must be between 1 and 20',
            }, 400)

        try:
            result = subprocess.run(
                ['python3', GRIDSIGNAL_SCANNER,
                 '--mode', mode, '--limit', str(limit)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return _json_response(self, {
                    'error': 'Scanner failed',
                    'detail': result.stderr.strip()[:500] or f'exit code {result.returncode}',
                }, 500)

            signals = json.loads(result.stdout)
            if not isinstance(signals, list):
                return _json_response(self, {
                    'error': 'Scanner returned unexpected format',
                    'detail': str(signals)[:200],
                }, 500)

            return _json_response(self, {
                'mode': mode,
                'count': len(signals),
                'signals': signals,
            })

        except subprocess.TimeoutExpired:
            return _json_response(self, {
                'error': 'Scanner timed out',
                'detail': 'Scanner did not complete within 120 seconds',
            }, 504)
        except json.JSONDecodeError:
            return _json_response(self, {
                'error': 'Scanner output parse error',
                'detail': result.stdout[:500] if 'result' in dir() else '',
            }, 500)
        except FileNotFoundError:
            return _json_response(self, {
                'error': 'Scanner script not found',
                'detail': f'Expected at {GRIDSIGNAL_SCANNER}',
            }, 500)
        except Exception as e:
            return _json_response(self, {
                'error': 'Internal scanner error',
                'detail': str(e)[:500],
            }, 500)

    def _handle_enter(self, body: dict):
        """POST /enter — ручной вход в позицию.

        Принимает: {"symbol": "XRPUSDT", "side": "Buy|Sell", "qty": 10,
                      "sl": 0.50, "tp": 0.55}
        """
        # ── Валидация ──
        symbol = body.get('symbol', '').strip().upper()
        if not symbol or not symbol.endswith('USDT'):
            return _json_response(self, {
                'error': 'Invalid symbol',
                'detail': 'symbol must be like XRPUSDT',
            }, 400)

        side = body.get('side', '').strip()
        if side not in ('Buy', 'Sell'):
            return _json_response(self, {
                'error': 'Invalid side',
                'detail': "side must be 'Buy' (LONG) or 'Sell' (SHORT)",
            }, 400)

        try:
            qty = float(body.get('qty', 0))
        except (ValueError, TypeError):
            return _json_response(self, {
                'error': 'Invalid qty',
                'detail': 'qty must be a number',
            }, 400)

        if qty <= 0:
            return _json_response(self, {
                'error': 'Invalid qty',
                'detail': 'qty must be positive',
            }, 400)

        sl = body.get('sl')
        if sl is not None:
            try:
                sl = float(sl)
            except (ValueError, TypeError):
                return _json_response(self, {
                    'error': 'Invalid sl',
                    'detail': 'sl must be a number',
                }, 400)

        tp = body.get('tp')
        if tp is not None:
            try:
                tp = float(tp)
            except (ValueError, TypeError):
                return _json_response(self, {
                    'error': 'Invalid tp',
                    'detail': 'tp must be a number',
                }, 400)

        # ── Проверка существующей позиции ──
        existing = _get_position(symbol)
        if existing:
            return _json_response(self, {
                'error': 'Position already exists',
                'detail': f'{symbol} already has an open {existing["side"]} position of size {existing["size"]}',
                'existing': existing,
            }, 409)

        # ── Размещение рыночного ордера ──
        qty_str = str(int(qty)) if qty == int(qty) else str(qty)

        order_body = json.dumps({
            'category': 'linear',
            'symbol': symbol,
            'side': side,
            'orderType': 'Market',
            'qty': qty_str,
            'timeInForce': 'IOC',
            'positionIdx': 0,
        })

        order_result = _run_bybit('raw', 'POST', '/v5/order/create', order_body)

        if order_result.get('retCode') != 0:
            err_msg = order_result.get('retMsg', 'Unknown error')
            # Определяем статус по ошибке
            status = 400
            if 'margin' in err_msg.lower() or 'balance' in err_msg.lower() or 'insufficient' in err_msg.lower():
                status = 402  # Payment Required для маржи
                err_msg = f'Insufficient margin: {err_msg}'
            elif '110001' in err_msg:
                status = 422
            elif 'symbol' in err_msg.lower() or 'not found' in err_msg.lower():
                status = 404
            return _json_response(self, {
                'error': 'Order failed',
                'detail': err_msg,
                'bybit_code': order_result.get('retCode'),
            }, status)

        order_id = order_result.get('result', {}).get('orderId', 'unknown')
        result = {
            'status': 'ok',
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'order_id': order_id,
        }

        # ── Пауза чтобы позиция появилась ──
        time.sleep(0.5)

        # ── Размещение SL ──
        if sl is not None and sl > 0:
            sl_side = 'Sell' if side == 'Buy' else 'Buy'
            sl_body = json.dumps({
                'category': 'linear',
                'symbol': symbol,
                'side': sl_side,
                'positionIdx': 0,
                'orderType': 'Market',
                'qty': qty_str,
                'stopLoss': str(_round_price(sl)),
                'slTriggerBy': 'MarkPrice',
            })
            sl_result = _run_bybit('raw', 'POST', '/v5/position/trading-stop', sl_body)
            if sl_result.get('retCode') == 0:
                result['sl'] = {'price': _round_price(sl), 'status': 'placed'}
            else:
                result['sl'] = {
                    'price': _round_price(sl), 'status': 'failed',
                    'detail': sl_result.get('retMsg', '?')
                }

        # ── Размещение TP ──
        if tp is not None and tp > 0:
            tp_side = 'Sell' if side == 'Buy' else 'Buy'
            # Проверка что TP в правильную сторону
            if (side == 'Buy' and tp > sl) or (side == 'Sell' and tp < sl) or sl is None:
                tp_body = json.dumps({
                    'category': 'linear',
                    'symbol': symbol,
                    'side': tp_side,
                    'orderType': 'Limit',
                    'qty': qty_str,
                    'price': str(_round_price(tp)),
                    'positionIdx': 0,
                    'timeInForce': 'GTC',
                    'reduceOnly': True,
                })
                tp_result = _run_bybit('raw', 'POST', '/v5/order/create', tp_body)
                if tp_result.get('retCode') == 0:
                    result['tp'] = {'price': _round_price(tp), 'status': 'placed'}
                else:
                    result['tp'] = {
                        'price': _round_price(tp), 'status': 'failed',
                        'detail': tp_result.get('retMsg', '?')
                    }
            else:
                result['tp'] = {
                    'price': _round_price(tp), 'status': 'skipped',
                    'detail': 'TP price is on wrong side of SL — would be redundant'
                }

        return _json_response(self, result, 200)

    def _handle_close(self, body: dict):
        """POST /close — закрыть позицию рынком.

        Принимает: {"symbol": "XRPUSDT"}
        """
        symbol = body.get('symbol', '').strip().upper()
        if not symbol or not symbol.endswith('USDT'):
            return _json_response(self, {
                'error': 'Invalid symbol',
                'detail': 'symbol must be like XRPUSDT',
            }, 400)

        # ── Получить позицию ──
        pos = _get_position(symbol)
        if not pos:
            return _json_response(self, {
                'error': 'No position',
                'detail': f'No open position found for {symbol}',
            }, 404)

        # ── Закрывающий ордер (противоположная сторона) ──
        close_side = 'Sell' if pos['side'] == 'Buy' else 'Buy'
        qty_str = str(int(pos['size'])) if pos['size'] == int(pos['size']) else str(pos['size'])

        close_body = json.dumps({
            'category': 'linear',
            'symbol': symbol,
            'side': close_side,
            'orderType': 'Market',
            'qty': qty_str,
            'positionIdx': pos['positionIdx'],
            'timeInForce': 'IOC',
            'reduceOnly': True,
        })

        close_result = _run_bybit('raw', 'POST', '/v5/order/create', close_body)

        if close_result.get('retCode') != 0:
            return _json_response(self, {
                'error': 'Close failed',
                'detail': close_result.get('retMsg', 'Unknown error'),
                'bybit_code': close_result.get('retCode'),
            }, 400)

        order_id = close_result.get('result', {}).get('orderId', 'unknown')
        return _json_response(self, {
            'status': 'ok',
            'symbol': symbol,
            'closed_side': pos['side'],
            'size': pos['size'],
            'entry': pos['entry'],
            'mark': pos['mark'],
            'close_side': close_side,
            'order_id': order_id,
        })


def start_rpc_server(port=8766, bind='127.0.0.1'):
    """Запустить RPC-сервер в фоновом потоке."""
    server = HTTPServer((bind, port), RPCHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="bybit-rpc")
    thread.start()
    return server


def update_health(alive=True, cycle_count=0, cycle_duration=0.0):
    """Обновить глобальное состояние RPC (вызывается из main_loop)."""
    rpc_state["alive"] = alive
    rpc_state["cycle_count"] = cycle_count
    rpc_state["last_cycle"] = time.time()
    rpc_state["cycle_duration"] = cycle_duration
