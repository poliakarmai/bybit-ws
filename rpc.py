"""RPC-сервер bybit-ws — HTTP JSON API для дашборда и внешних потребителей.

Запускается как фоновый поток в main.py.
Порт: 8766 (рядом с дашбордом 8765).
Версия API: v1 (доступна через /v1/* и корневые алиасы).

Endpoints:
    GET  /rpc/all         — все данные одним запросом (позиции, ордера, алерты, метрики, трейды)
    GET  /rpc/positions   — открытые позиции
    GET  /rpc/orders      — активные ордера
    GET  /rpc/health      — статус монитора (alive, uptime, cycle_count)
    GET  /rpc/trades      — трейд-лог (trades.jsonl)
    GET  /rpc/alerts      — последние алерты
    GET  /rpc/metrics     — метрики (daily)
    GET  /rpc/signals     — LONG + SHORT сигналы (скоринг, кандидаты)
    GET  /rpc/config      — текущая конфигурация (без секретов)
    GET  /health          — алиас на /rpc/health
    GET  /positions       — алиас на /rpc/positions
    GET  /orders          — алиас на /rpc/orders
    GET  /metrics         — алиас на /rpc/metrics
    GET  /signals         — алиас на /rpc/signals
    GET  /config          — алиас на /rpc/config
    POST /scan            — запустить GridSignal-сканер
    POST /enter           — ручной вход в позицию
    POST /close           — закрыть позицию

Все ответы содержат api_version: "v1".
При ошибке: {"error": "...", "detail": "...", "api_version": "v1", "status": код}.
"""

import json
import os
import time
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import defaultdict

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
    "paused": False,
}

# Rate limiting: per-IP token bucket
_rate_limit_store = defaultdict(lambda: {"tokens": 60, "last": time.time()})

API_VERSION = "v1"


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
    """Отправить JSON-ответ с api_version."""
    if isinstance(data, dict) and "api_version" not in data:
        data = {"api_version": API_VERSION, **data}
    body = json.dumps(data, ensure_ascii=False, default=str)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "http://localhost, http://127.0.0.1")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body.encode())


def _error(handler, error: str, detail: str = "", status: int = 400):
    """Стандартный error-ответ."""
    return _json_response(handler, {
        "error": error,
        "detail": detail,
        "status": status,
    }, status)


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


def _get_auth_token() -> str:
    """Получить RPC токен из конфига (если есть). Пустая строка = без auth."""
    try:
        from .config import Config
        cfg = Config()
        token = cfg.rpc.get('auth_token', '')
        # Если токен — env-var который не подставился, считаем пустым
        if token.startswith('${') or token == '':
            return ''
        return token
    except Exception:
        return ''


def _check_rate_limit(client_ip: str, max_per_min: int = 60) -> bool:
    """Проверить rate limit для IP. Возвращает True если запрос разрешён."""
    now = time.time()
    bucket = _rate_limit_store[client_ip]
    elapsed = now - bucket["last"]
    bucket["last"] = now
    # Восстановление токенов: 1 токен в секунду
    bucket["tokens"] = min(max_per_min, bucket["tokens"] + elapsed)
    if bucket["tokens"] >= 1:
        bucket["tokens"] -= 1
        return True
    return False


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

    def _check_auth(self) -> bool:
        """Проверить Bearer-токен (обязателен при bind=0.0.0.0)."""
        token = _get_auth_token()
        if not token:
            # Проверяем bind — если 0.0.0.0, отказ в обслуживании
            try:
                from .config import Config
                cfg = Config()
                bind = cfg.rpc.get('bind', '127.0.0.1')
                if bind == '0.0.0.0':
                    return False  # внешний доступ без токена запрещён
            except Exception as e:
                import logging
                logging.getLogger('bybit.rpc').warning(f'_check_auth config: {e}')
            return True  # localhost — ок
        auth = self.headers.get('Authorization', '')
        return auth == f'Bearer {token}'

    def _check_ip_rate(self) -> bool:
        """Проверить rate limit для IP клиента."""
        try:
            from .config import Config
            cfg = Config()
            max_per_min = cfg.rpc.get('rate_limit_per_min', 60)
        except Exception:
            max_per_min = 60
        client_ip = self.client_address[0]
        return _check_rate_limit(client_ip, max_per_min)

    # ── CORS preflight ──────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "http://localhost, http://127.0.0.1")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── GET ─────────────────────────────────────────────────────
    def do_GET(self):
        if not self._check_ip_rate():
            return _error(self, 'Rate limit exceeded', 'Too many requests', 429)
        if not self._check_auth():
            return _error(self, 'Unauthorized', 'Invalid or missing Bearer token', 401)

        path = self.path.rstrip("/") or "/"

        # Алиасы (короткие пути)
        if path == "/health":
            return self._handle_health()
        if path == "/positions":
            return self._handle_positions()
        if path == "/orders":
            return self._handle_orders()
        if path == "/metrics":
            return self._handle_metrics()
        if path == "/balance":
            return self._handle_balance()
        if path == "/signals":
            return self._handle_signals()
        if path == "/config":
            return self._handle_get_config()

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
        elif path == "/rpc/signals":
            self._handle_signals()
        elif path == "/rpc/config":
            self._handle_get_config()
        elif path == "/rpc" or path == "/":
            self._handle_index()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found","api_version":"v1"}')

    # ── POST ────────────────────────────────────────────────────
    def do_POST(self):
        if not self._check_ip_rate():
            return _error(self, 'Rate limit exceeded', 'Too many requests', 429)
        if not self._check_auth():
            return _error(self, 'Unauthorized', 'Invalid or missing Bearer token', 401)

        path = self.path.rstrip("/") or "/"

        # Читаем тело запроса
        content_length = int(self.headers.get('Content-Length', 0))
        body_raw = self.rfile.read(content_length) if content_length > 0 else b''

        try:
            body = json.loads(body_raw) if body_raw else {}
        except json.JSONDecodeError:
            return _error(self, 'Invalid JSON body', '', 400)

        if path == "/scan":
            self._handle_scan(body)
        elif path == "/enter":
            self._handle_enter(body)
        elif path == "/close":
            self._handle_close(body)
        elif path == "/reload-config":
            self._handle_reload_config()
        elif path == "/pause":
            self._handle_pause()
        elif path == "/resume":
            self._handle_resume()
        elif path == "/logs":
            self._handle_logs(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found","api_version":"v1"}')

    # ═══════════════════════════════════════════════════════════════
    # GET handlers
    # ═══════════════════════════════════════════════════════════════

    def _handle_index(self):
        _json_response(self, {
            "service": "bybit-ws-rpc",
            "api_version": API_VERSION,
            "endpoints": [
                "/rpc/all", "/rpc/positions", "/rpc/orders",
                "/rpc/health", "/rpc/trades", "/rpc/alerts", "/rpc/metrics",
                "/rpc/signals", "/rpc/config",
                "/health", "/positions", "/orders", "/metrics", "/signals", "/config",
                "POST /scan", "POST /enter", "POST /close",
                "POST /reload-config", "POST /pause", "POST /resume", "POST /logs",
            ]
        })

    def _handle_get_config(self):
        """GET /rpc/config — текущая конфигурация без секретов."""
        try:
            from .config import get_config
            cfg = get_config()
            # Удаляем секреты
            safe = dict(cfg)
            if 'api' in safe:
                safe['api'] = dict(safe['api'])
                safe['api'].pop('key', None)
                safe['api'].pop('secret', None)
            if 'rpc' in safe and 'auth_token' in safe['rpc']:
                safe['rpc'] = dict(safe['rpc'])
                safe['rpc']['auth_token'] = '***' if safe['rpc']['auth_token'] else ''
            _json_response(self, safe)
        except Exception as e:
            _error(self, 'Config read error', str(e), 500)

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
                        except Exception as e:
                            import logging
                            logging.getLogger('bybit.rpc').warning(f'trade parse: {e}')

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
                "paused": rpc_state["paused"],
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
            except Exception as e:
                import logging
                logging.getLogger('bybit.rpc').warning(f'health parse: {e}')

        _json_response(self, {
            "status": "alive" if alive else "stale",
            "alive": alive,
            "uptime": int(time.time() - rpc_state["started_at"]),
            "cycle_count": rpc_state["cycle_count"],
            "last_cycle": rpc_state["last_cycle"],
            "cycle_duration": rpc_state["cycle_duration"],
            "paused": rpc_state["paused"],
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
                        except Exception as e:
                            import logging
                            logging.getLogger('bybit.rpc').warning(f'trade parse: {e}')

        limit = 100
        if "?limit=" in self.path:
            try:
                limit = int(self.path.split("limit=")[1].split("&")[0])
            except Exception as e:
                import logging
                logging.getLogger('bybit.rpc').warning(f'limit parse: {e}')

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

    def _handle_signals(self):
        """GET /rpc/signals — LONG и SHORT сигналы."""
        result = {"long": [], "short": []}

        # SHORT сигналы через GridSignal сканер
        try:
            short_result = subprocess.run(
                ['python3', GRIDSIGNAL_SCANNER, '--mode', 'short', '--limit', '10'],
                capture_output=True, text=True, timeout=60
            )
            if short_result.returncode == 0:
                result["short"] = json.loads(short_result.stdout)
        except Exception as e:
            import logging
            logging.getLogger('bybit.rpc').warning(f'Short scanner failed: {e}')

        # LONG сигналы через auto_entry_scan
        try:
            from .auto_entry import auto_entry_scan
            positions = _load_json(DATA_DIR / "positions.json")
            if not isinstance(positions, dict):
                positions = {}
            long_entries = auto_entry_scan(positions)
            # Парсим сообщения в структурированные сигналы
            for msg in long_entries:
                # Формат: "📌 SYMUSDT score=X.X (Tier X) BB=X% ..."
                try:
                    parts = msg.split()
                    if len(parts) >= 3 and parts[0] == "📌":
                        sym = parts[1]
                        signal = {"symbol": sym, "raw": msg}
                        for p in parts[2:]:
                            if "=" in p:
                                k, v = p.split("=", 1)
                                signal[k] = v
                        result["long"].append(signal)
                except Exception as e:
                    import logging
                    logging.getLogger('bybit.rpc').warning(f'Signal parse error: {e}')
        except Exception as e:
            import logging
            logging.getLogger('bybit.rpc').warning(f'Long scanner failed: {e}')

        _json_response(self, result)

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
            return _error(self, 'Invalid mode', "mode must be 'long' or 'short'", 400)

        try:
            limit = int(body.get('limit', 5))
        except (ValueError, TypeError):
            return _error(self, 'Invalid limit', 'limit must be an integer', 400)

        if limit < 1 or limit > 20:
            return _error(self, 'Invalid limit', 'limit must be between 1 and 20', 400)

        try:
            result = subprocess.run(
                ['python3', GRIDSIGNAL_SCANNER,
                 '--mode', mode, '--limit', str(limit)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return _error(self, 'Scanner failed',
                              result.stderr.strip()[:500] or f'exit code {result.returncode}', 500)

            signals = json.loads(result.stdout)
            if not isinstance(signals, list):
                return _error(self, 'Scanner returned unexpected format',
                              str(signals)[:200], 500)

            return _json_response(self, {
                'mode': mode,
                'count': len(signals),
                'signals': signals,
            })

        except subprocess.TimeoutExpired:
            return _error(self, 'Scanner timed out',
                          'Scanner did not complete within 120 seconds', 504)
        except json.JSONDecodeError:
            return _error(self, 'Scanner output parse error',
                          result.stdout[:500] if 'result' in dir() else '', 500)
        except FileNotFoundError:
            return _error(self, 'Scanner script not found',
                          f'Expected at {GRIDSIGNAL_SCANNER}', 500)
        except Exception as e:
            return _error(self, 'Internal scanner error', str(e)[:500], 500)

    def _handle_enter(self, body: dict):
        """POST /enter — ручной вход в позицию.

        Принимает: {"symbol": "XRPUSDT", "side": "Buy|Sell", "qty": 10,
                      "sl": 0.50, "tp": 0.55, "confirm": true}
        Если confirm: true — исполнение. Если false — только превью.
        """
        # ── Валидация ──
        symbol = body.get('symbol', '').strip().upper()
        if not symbol or not symbol.endswith('USDT'):
            return _error(self, 'Invalid symbol', 'symbol must be like XRPUSDT', 400)

        side = body.get('side', '').strip()
        if side not in ('Buy', 'Sell'):
            return _error(self, 'Invalid side', "side must be 'Buy' (LONG) or 'Sell' (SHORT)", 400)

        try:
            qty = float(body.get('qty', 0))
        except (ValueError, TypeError):
            return _error(self, 'Invalid qty', 'qty must be a number', 400)

        if qty <= 0:
            return _error(self, 'Invalid qty', 'qty must be positive', 400)

        sl = body.get('sl')
        if sl is not None:
            try:
                sl = float(sl)
            except (ValueError, TypeError):
                return _error(self, 'Invalid sl', 'sl must be a number', 400)

        tp = body.get('tp')
        if tp is not None:
            try:
                tp = float(tp)
            except (ValueError, TypeError):
                return _error(self, 'Invalid tp', 'tp must be a number', 400)

        # ── Проверка существующей позиции ──
        existing = _get_position(symbol)
        if existing:
            return _error(self, 'Position already exists',
                          f'{symbol} already has an open {existing["side"]} position of size {existing["size"]}', 409)

        # ── Подтверждение (двухэтапный вход) ──
        confirm = body.get('confirm', False)
        if not confirm:
            # Превью-режим: возвращаем что БУДЕТ сделано
            preview = {
                'symbol': symbol,
                'side': side,
                'qty': qty,
                'sl': _round_price(sl) if sl else None,
                'tp': _round_price(tp) if tp else None,
                'confirm_required': True,
                'message': 'Send with confirm: true to execute',
            }
            return _json_response(self, preview)

        # ── Размещение рыночного ордера ──
        # Валидация qty: должно быть положительным числом
        if not isinstance(qty, (int, float)) or qty <= 0:
            return _error(self, 'Invalid qty', 'qty must be a positive number')
        try:
            qty_int = int(qty)
            qty_str = str(qty_int) if qty == qty_int else str(qty)
        except (ValueError, TypeError):
            qty_str = str(qty)

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

        # Retry with positionIdx=1 если hedge mode
        if order_result.get('retCode') != 0 and 'position idx' in order_result.get('retMsg', ''):
            order_body_dict = json.loads(order_body)
            order_body_dict['positionIdx'] = 1
            order_body = json.dumps(order_body_dict)
            order_result = _run_bybit('raw', 'POST', '/v5/order/create', order_body)

        if order_result.get('retCode') != 0:
            err_msg = order_result.get('retMsg', 'Unknown error')
            status = 400
            if 'margin' in err_msg.lower() or 'balance' in err_msg.lower() or 'insufficient' in err_msg.lower():
                status = 402
                err_msg = f'Insufficient margin: {err_msg}'
            elif '110001' in err_msg:
                status = 422
            elif 'symbol' in err_msg.lower() or 'not found' in err_msg.lower():
                status = 404
            return _error(self, 'Order failed', err_msg, status)

        order_id = order_result.get('result', {}).get('orderId', 'unknown')
        result = {
            'status': 'ok',
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'order_id': order_id,
        }

        # ── Ожидание появления позиции (polling) ──
        pos = None
        for _ in range(6):  # до 3 секунд
            time.sleep(0.5)
            pos = _get_position(symbol)
            if pos is not None:
                break

        # ── Размещение SL ──
        if sl is not None and sl > 0:
            sl_side = 'Sell' if side == 'Buy' else 'Buy'
            # Use actual positionIdx (hedge-safe)
            actual_idx = pos.get('positionIdx', 0) if pos else 0
            sl_body = json.dumps({
                'category': 'linear',
                'symbol': symbol,
                'side': sl_side,
                'positionIdx': actual_idx,
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
            return _error(self, 'Invalid symbol', 'symbol must be like XRPUSDT', 400)

        pos = _get_position(symbol)
        if not pos:
            return _error(self, 'No position',
                          f'No open position found for {symbol}', 404)

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
            return _error(self, 'Close failed',
                          close_result.get('retMsg', 'Unknown error'), 400)

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

    def _handle_reload_config(self):
        """POST /reload-config — перечитать config.yaml без рестарта."""
        try:
            from .config import reload_config
            reload_config()
            _json_response(self, {"status": "ok", "message": "config reloaded"})
        except Exception as e:
            _error(self, 'Config reload error', str(e), 500)

    def _handle_pause(self):
        """POST /pause — приостановить торговлю."""
        rpc_state["paused"] = True
        _json_response(self, {"status": "ok", "paused": True})

    def _handle_resume(self):
        """POST /resume — возобновить торговлю."""
        rpc_state["paused"] = False
        _json_response(self, {"status": "ok", "paused": False})

    def _handle_logs(self, body: dict):
        """GET /logs?lines=100 — последние строки events.log."""
        import os
        lines = int(body.get("lines", 100)) if body else 100
        log_path = os.path.expanduser("~/.local/share/bybit-ws/events.log")
        try:
            with open(log_path) as f:
                all_lines = f.readlines()
                last = all_lines[-lines:] if len(all_lines) > lines else all_lines
                _json_response(self, {"lines": len(last), "log": "".join(last)})
        except FileNotFoundError:
            _error(self, 'Log file not found', str(log_path), 404)


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
