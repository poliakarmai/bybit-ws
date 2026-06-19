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
    POST /reset-token     — сбросить RPC-токен (генерирует новый UUID)

Все ответы содержат api_version: "v1".
При ошибке: {"error": "...", "detail": "...", "api_version": "v1", "status": код}.
"""

import json
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import defaultdict
from statistics import mean, stdev
import urllib.request

from .api import bybit as _bybit_api
from .api import fetch_positions as _fetch_positions
from .state_db import db as _db
from .alerts import add_alert

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
HOME = Path.home()

# Глобальное состояние (обновляется main-потоком)
rpc_state = {
    "alive": False,
    "started_at": time.time(),
    "cycle_count": 0,
    "last_cycle": 0.0,
    "cycle_duration": 0.0,
    "paused": False,
}

# BB-кеш: {symbol: {data, ts}}. Дневные свечи — валидны 24ч.
_BB_CACHE = {}
_BB_CACHE_LOCK = threading.Lock()
_BB_CACHE_FILE = DATA_DIR / "bb_cache.json"


def _load_bb_cache():
    global _BB_CACHE
    try:
        if _BB_CACHE_FILE.exists():
            _BB_CACHE = json.loads(_BB_CACHE_FILE.read_text())
    except Exception:
        _BB_CACHE = {}
_BB_CACHE_LOCK = threading.Lock()


def _save_bb_cache():
    try:
        _BB_CACHE_FILE.write_text(json.dumps(_BB_CACHE))
    except Exception:
        pass


def _get_bb_for_symbol(symbol: str, interval: str = 'D') -> dict | None:
    """Вычисляет BB(20,2) и RSI(14) для символа. Кеш 24ч, keyed по interval."""
    now = time.time()
    cache_key = f"{symbol}|{interval}"
    cached = _BB_CACHE.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 86400:  # 24h
        return cached["data"]

    try:
        url = (
            f"https://api.bybit.com/v5/market/kline?"
            f"category=linear&symbol={symbol}&interval={interval}&limit=50"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "bybit-ws-rpc/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        if raw.get("retCode") != 0:
            return None

        klines = raw["result"]["list"]
        klines.reverse()  # старые → новые
        closes = [float(k[4]) for k in klines]
        if len(closes) < 20:
            return None

        sma_20 = mean(closes[-20:])
        std_20 = stdev(closes[-20:]) if len(closes[-20:]) > 1 else 0.0
        upper = sma_20 + 2 * std_20
        lower = sma_20 - 2 * std_20
        bb_pct = (closes[-1] - lower) / (upper - lower) * 100 if upper != lower else 50.0

        # RSI(14)
        rsi_w = closes[-15:]
        gains = sum(max(rsi_w[j] - rsi_w[j-1], 0) for j in range(1, len(rsi_w)))
        losses = sum(max(rsi_w[j-1] - rsi_w[j], 0) for j in range(1, len(rsi_w)))
        rsi = 100 - (100 / (1 + gains / max(losses, 0.0001)))

        # down_days: количество дней снижения подряд
        down_days = 0
        for j in range(len(closes)-1, 0, -1):
            if closes[j] < closes[j-1]:
                down_days += 1
            else:
                break

        data = {
            "bb_sma": round(sma_20, 8),
            "bb_upper": round(upper, 8),
            "bb_lower": round(lower, 8),
            "bb_pct": round(bb_pct, 1),
            "bb_rsi": round(rsi, 0),
            "bb_down_days": down_days,
        }
        with _BB_CACHE_LOCK:
            _BB_CACHE[cache_key] = {"data": data, "ts": now}
        return data
    except Exception:
        return None


def _enrich_positions_with_bb(positions: list[dict]) -> list[dict]:
    """Добавляет BB% и RSI к каждой позиции."""
    _load_bb_cache()
    modified = False
    for p in positions:
        sym = p.get("symbol", "")
        if not sym:
            continue
        bb = _get_bb_for_symbol(sym)
        if bb:
            p["bb_pct"] = bb["bb_pct"]
            p["bb_rsi"] = bb["bb_rsi"]
            p["bb_sma"] = bb["bb_sma"]
            p["bb_upper"] = bb["bb_upper"]
            p["bb_lower"] = bb["bb_lower"]
            modified = True
    if modified:
        _save_bb_cache()
    return positions

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


# ── RPC error codes (MONITOR.md §5) ─────────────────────────
ERROR_CODES = {
    400: 'bad_request',
    401: 'unauthorized',
    402: 'insufficient_margin',
    404: 'symbol_not_found',
    409: 'position_exists',
    422: 'invalid_qty',
    429: 'rate_limit',
    500: 'internal_error',
}

def _error(handler, error: str, detail: str = "", status: int = 400, error_code: str = None):
    """Стандартный error-ответ с кодом ошибки.

    Args:
        handler: HTTP handler
        error: краткое описание
        detail: расширенная информация
        status: HTTP-статус
        error_code: код из ERROR_CODES (авто по status если не указан)
    """
    if error_code is None:
        error_code = ERROR_CODES.get(status, 'unknown_error')
    return _json_response(handler, {
        "error": error,
        "error_code": error_code,
        "detail": detail,
        "status": status,
    }, status)


def _api_call(method, path, body=None) -> dict:
    """Выполнить запрос к Bybit API через нативный модуль api.
    
    Замена subprocess(BYBIT_CLI) на прямые вызовы requests.
    """
    if body is not None and isinstance(body, str):
        body = json.loads(body)
    return _bybit_api(method, path, body)


def _get_position(symbol: str) -> dict | None:
    """Получить информацию о позиции по символу."""
    data = _bybit_api('GET',
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


def _get_auth_token() -> str:
    """Получить RPC токен. Автогенерация при первом запуске (SQLite)."""
    try:
        from .config import Config
        cfg = Config()
        token = cfg.rpc.get('auth_token', '')
        if token and not token.startswith('${'):
            return token
    except Exception:
        pass
    # Автогенерация: сохраняем в state.db/kv_store
    try:
        from .state_db import db
        token = db.get_kv('rpc_auth_token')
        if not token:
            import uuid
            token = str(uuid.uuid4())
            db.set_kv('rpc_auth_token', token)
        return token
    except Exception:
        import uuid
        return str(uuid.uuid4())


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


class RPCHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # тихий режим

    def _check_auth(self) -> bool:
        """Проверить Bearer-токен (обязателен всегда)."""
        token = _get_auth_token()
        if not token:
            return False
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

        path = self.path.rstrip("/") or "/"
        path = path.split("?")[0]  # strip query string

        # Публичные эндпоинты — без авторизации
        if path in ("/health", "/rpc/paths"):
            if path == "/health":
                return self._handle_health()
            if path == "/rpc/paths":
                return self._handle_paths()

        if not self._check_auth():
            return _error(self, 'Unauthorized', 'Invalid or missing Bearer token', 401)

        # Алиасы (короткие пути)
        if path == "/health":
            return self._handle_health()
        if path == "/positions":
            return self._handle_positions()
        if path == "/orders":
            return self._handle_orders()
        if path == "/metrics":
            return self._handle_metrics()
        if path == "/risk":
            return self._handle_risk()
        if path == "/balance":
            return self._handle_balance()
        if path == "/metrics":
            return self._handle_metrics_prometheus()
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
        elif path == "/rpc/risk":
            self._handle_risk()
        elif path == "/rpc/signals":
            self._handle_signals()
        elif path == "/rpc/config":
            self._handle_get_config()
        elif path == "/rpc/paths":
            self._handle_paths()
        elif path == "/rpc/ab_test_report":
            self._handle_ab_test_report()
        elif path == "/rpc/ml_toggle":
            self._handle_ml_toggle()
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
        elif path == "/reset-token":
            self._handle_reset_token(body)
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
                "/rpc/health", "/rpc/trades", "/rpc/alerts", "/rpc/metrics", "/rpc/risk",
                "/rpc/signals", "/rpc/config", "/rpc/paths", "/rpc/ab_test_report",
                "/health", "/positions", "/orders", "/metrics", "/risk", "/signals", "/config",
                "POST /scan", "POST /enter", "POST /close", "POST /reset-token",
                "POST /reload-config", "POST /pause", "POST /resume", "POST /logs",
            ]
        })

    def _handle_paths(self):
        """GET /rpc/paths — все пути установки bybit-ws для внешних агентов."""
        import os
        paths = {
            "state_db": os.path.expanduser("~/.local/share/bybit-ws/state.db"),
            "events_log": os.path.expanduser("~/.local/share/bybit-ws/events.log"),
            "alerts_log": os.path.expanduser("~/.local/share/bybit-ws/alerts.log"),
            "rpc_port": 8766,
            "rpc_host": "127.0.0.1",
            "repo": os.path.expanduser("~/bybit-ws"),
            "install_dir": os.path.expanduser("~/.local/lib/bybit_ws"),
            "config_file": os.path.expanduser("~/.config/bybit-ws/config.yaml"),
            "service": "bybit-ws",
            "sync_command": "cp ~/bybit-ws/{file}.py ~/.local/lib/bybit_ws/",
            "restart_command": "systemctl --user restart bybit-ws",
            "venv": os.path.expanduser("~/bybit-ws/.venv"),
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(paths, indent=2).encode())

    def _handle_ab_test_report(self):
        """GET /rpc/ab_test_report — отчёт A/B-тестирования ML Gate."""
        try:
            from .ab_test import get_report as _ab_report
            report = _ab_report()
            _json_response(self, report)
        except Exception as e:
            _error(self, 'AB test error', str(e), 500)

    def _handle_ml_toggle(self):
        """GET /rpc/ml_toggle — статус ML-конвейера (включён/выключен).
        POST /rpc/ml_toggle?enable=0|1 — переключить (требует авторизации)."""
        import os
        if self.command == 'POST':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            enable = body.get('enable', None)
            # Также поддержка query-параметров
            if enable is None:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                enable = qs.get('enable', [None])[0]
            if enable is not None:
                new_val = '1' if str(enable) in ('1', 'true', 'True') else '0'
                os.environ['BYBIT_ML_ENABLED'] = new_val
                _json_response(self, {
                    'ml_enabled': new_val == '1',
                    'note': 'Перезапустите bybit-ws-async для применения',
                    'restart_cmd': 'systemctl --user restart bybit-ws-async'
                })
                return
        # GET — показать текущий статус
        from .auto_entry import ML_ENABLED as _ml
        _json_response(self, {'ml_enabled': _ml, 'env_var': 'BYBIT_ML_ENABLED'})

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
        """Все данные одним запросом — для дашборда (из SQLite SSOT)."""
        # Позиции из SQLite (SSOT), алерты/метрики из JSON (backup)
        positions_db = _db.get_positions()
        positions_raw = positions_db if positions_db else _load_json(DATA_DIR / "positions.json")
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
        positions_db = _db.get_positions()
        positions = positions_db if positions_db else _load_json(DATA_DIR / "positions.json")
        result = []
        if isinstance(positions, dict):
            for sym, p in positions.items():
                if isinstance(p, dict):
                    p = dict(p)
                    p["symbol"] = sym
                result.append(p)
        result = _enrich_positions_with_bb(result)
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

    def _handle_metrics_prometheus(self):
        """GET /metrics — Prometheus-совместимый текстовый формат."""
        lines = []
        # Позиции
        positions = _db.get_positions() or _load_json(DATA_DIR / "positions.json")
        if isinstance(positions, dict):
            longs = sum(1 for p in positions.values() if p.get('side') == 'Buy')
            shorts = sum(1 for p in positions.values() if p.get('side') == 'Sell')
            total_upnl = sum(float(p.get('upnl', 0)) for p in positions.values())
            lines.append(f'# HELP bybit_ws_active_positions Current open positions')
            lines.append(f'# TYPE bybit_ws_active_positions gauge')
            lines.append(f'bybit_ws_active_positions{{side="long"}} {longs}')
            lines.append(f'bybit_ws_active_positions{{side="short"}} {shorts}')
            lines.append(f'# HELP bybit_ws_unrealized_pnl Unrealized PnL')
            lines.append(f'# TYPE bybit_ws_unrealized_pnl gauge')
            lines.append(f'bybit_ws_unrealized_pnl {total_upnl:.2f}')

        # Health
        lines.append(f'# HELP bybit_ws_uptime_seconds Monitor uptime')
        lines.append(f'# TYPE bybit_ws_uptime_seconds gauge')
        lines.append(f'bybit_ws_uptime_seconds {int(time.time() - rpc_state["started_at"])}')
        lines.append(f'# HELP bybit_ws_cycle_duration_seconds Last cycle duration')
        lines.append(f'# TYPE bybit_ws_cycle_duration_seconds gauge')
        lines.append(f'bybit_ws_cycle_duration_seconds {rpc_state["cycle_duration"]:.3f}')
        lines.append(f'# HELP bybit_ws_cycle_count Total cycles')
        lines.append(f'# TYPE bybit_ws_cycle_count counter')
        lines.append(f'bybit_ws_cycle_count {rpc_state["cycle_count"]}')

        # Daily PnL
        metrics = _load_json(DATA_DIR / "metrics.json")
        today_key = None
        import datetime
        for k in sorted(metrics.keys(), reverse=True):
            if k.startswith("20") and len(k) >= 8:
                try:
                    d = datetime.datetime.strptime(k[:10], "%Y-%m-%d")
                    if d.date() == datetime.date.today():
                        today_key = k
                        break
                except Exception:
                    pass
        daily_pnl = metrics.get(today_key, {}).get("pnl_total", 0) if today_key else 0
        lines.append(f'# HELP bybit_ws_daily_pnl Daily realized PnL')
        lines.append(f'# TYPE bybit_ws_daily_pnl gauge')
        lines.append(f'bybit_ws_daily_pnl {daily_pnl:.2f}')

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(('\n'.join(lines) + '\n').encode())

    def _handle_risk(self):
        """GET /rpc/risk — risk limits and current usage."""
        metrics = _load_json(DATA_DIR / "metrics.json")
        positions = _load_json(DATA_DIR / "positions.json")

        cfg = _load_json(DATA_DIR / "config.json") if os.path.exists(DATA_DIR / "config.json") else {}
        risk_cfg = cfg.get("risk", {})
        max_daily_loss = risk_cfg.get("max_daily_loss", 50)
        max_total_margin = risk_cfg.get("max_total_margin", 300)

        last_trade_date = None
        for date_key, entry in sorted(metrics.items(), reverse=True):
            if date_key.startswith("20") and len(date_key) >= 8:
                try:
                    import datetime
                    d = datetime.datetime.strptime(date_key[:10], "%Y-%m-%d")
                    if d.date() == datetime.date.today():
                        last_trade_date = date_key
                        break
                except Exception:
                    pass

        daily_loss = metrics.get(last_trade_date, {}).get("pnl_total", 0) if last_trade_date else 0
        total_margin = sum(float(p.get("margin", 0) or p.get("positionIM", 0)) for p in positions.values())
        position_count = len(positions)

        blocked = daily_loss <= -max_daily_loss or total_margin >= max_total_margin
        reasons = []
        if daily_loss <= -max_daily_loss:
            reasons.append(f"daily_loss (${abs(daily_loss):.2f}) >= max_daily_loss (${max_daily_loss})")
        if total_margin >= max_total_margin:
            reasons.append(f"total_margin (${total_margin:.2f}) >= max_total_margin (${max_total_margin})")

        _json_response(self, {
            "blocked": blocked,
            "reasons": reasons,
            "daily_loss": round(daily_loss, 2),
            "max_daily_loss": max_daily_loss,
            "total_margin": round(total_margin, 2),
            "max_total_margin": max_total_margin,
            "position_count": position_count,
            "remaining_daily_loss": round(max_daily_loss - abs(daily_loss), 2),
            "remaining_margin": round(max_total_margin - total_margin, 2),
        })

    def _handle_signals(self):
        """GET /rpc/signals — сигналы из BB% позиций (мгновенно, без API)."""
        result = {"long": [], "short": []}

        try:
            positions_db = _db.get_positions()
            positions = positions_db if positions_db else _load_json(DATA_DIR / "positions.json")
            if not isinstance(positions, dict):
                positions = {}

            enriched = []
            for sym, p in positions.items():
                if not isinstance(p, dict):
                    continue
                p = dict(p)
                p["symbol"] = sym
                enriched.append(p)

            enriched = _enrich_positions_with_bb(enriched)

            for p in enriched:
                sym = p.get("symbol", "")
                side = p.get("side", "")
                bb_pct = p.get("bb_pct")
                rsi = p.get("bb_rsi")
                mark = p.get("mark")
                entry = p.get("entry")

                if bb_pct is None:
                    continue

                try:
                    bb_pct = float(bb_pct)
                    rsi = float(rsi) if rsi else None
                    mark = float(mark) if mark else 0
                    entry = float(entry) if entry else 0
                except (ValueError, TypeError):
                    continue

                signal = {
                    "symbol": sym,
                    "side": side,
                    "bb_pct": round(bb_pct, 1),
                    "rsi": round(rsi, 0) if rsi else None,
                    "mark": mark,
                    "entry": entry,
                }

                if side == "Buy" and bb_pct < 35:
                    score = round(max(0, (35 - bb_pct) * 0.3 + max(0, 40 - (rsi or 50)) * 0.1, 1))
                    signal["score"] = score
                    signal["signal"] = "LONG"
                    result["long"].append(signal)
                elif side == "Sell" and bb_pct > 65:
                    score = round(max(0, (bb_pct - 65) * 0.3 + max(0, (rsi or 50) - 60) * 0.1, 1))
                    signal["score"] = score
                    signal["signal"] = "SHORT"
                    result["short"].append(signal)

            result["long"].sort(key=lambda s: s["score"], reverse=True)
            result["short"].sort(key=lambda s: s["score"], reverse=True)

        except Exception as e:
            import logging
            logging.getLogger('bybit.rpc').warning(f'Signals error: {e}')

        _json_response(self, result)

    # ═══════════════════════════════════════════════════════════════
    # POST handlers
    # ═══════════════════════════════════════════════════════════════

    def _handle_scan(self, body: dict):
        """POST /scan — Bollinger Grid скан (WS-кеш + параллельный REST).

        Принимает: {"mode": "long|short", "limit": 5, "interval": "D"}
        Возвращает: топ-N сигналов за ~5-10 секунд (даже при холодном кеше).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        mode = body.get('mode', 'long')
        if mode not in ('long', 'short'):
            return _error(self, 'Invalid mode', f'mode must be long/short, got {mode}', 400)

        interval = body.get('interval', 'D')
        if interval not in ('D', 'W', '4h', '1h', '15m', '5m'):
            return _error(self, 'Invalid interval', f'interval={interval}', 400)

        try:
            limit = int(body.get('limit', 5))
        except (ValueError, TypeError):
            return _error(self, 'Invalid limit', 'limit must be an integer', 400)
        if limit < 1 or limit > 20:
            return _error(self, 'Invalid limit', 'limit must be 1-20', 400)

        try:
            from . import api
            from .ws_client import get_bb

            # Топ-50 тикеров по обороту (один REST-запрос)
            ticker_resp = api.bybit('GET', '/v5/market/tickers?category=linear')
            if ticker_resp.get('retCode') != 0:
                return _error(self, 'API error', ticker_resp.get('retMsg', '?'), 500)

            tickers = ticker_resp.get('result', {}).get('list', [])
            tickers.sort(key=lambda t: float(t.get('turnover24h', 0)), reverse=True)

            BLACKLIST = {'TRUMPUSDT', 'MELANIAUSDT', 'BONKUSDT', 'FLOKIUSDT', 'WIFUSDT'}

            # Фаза 1: WS-кеш (мгновенно)
            ws_hits = []
            rest_needed = []
            for t in tickers:
                sym = t['symbol']
                if not sym.endswith('USDT') or sym in BLACKLIST:
                    continue
                bb = get_bb(sym, interval)
                if bb is not None:
                    ws_hits.append((sym, bb, float(t.get('lastPrice', 0)), float(t.get('turnover24h', 0))))
                else:
                    rest_needed.append(sym)
                if len(ws_hits) + len(rest_needed) >= 50:
                    break

            # Фаза 2: параллельный REST для промахов (10 потоков)
            rest_results = {}
            if rest_needed:
                _load_bb_cache()
                lock = threading.Lock()
                def _fetch_one(sym):
                    bb = _get_bb_for_symbol(sym, interval)
                    with lock:
                        rest_results[sym] = bb
                    return sym

                with ThreadPoolExecutor(max_workers=10) as pool:
                    futures = {pool.submit(_fetch_one, s): s for s in rest_needed}
                    for _ in as_completed(futures, timeout=30):
                        pass  # результаты в rest_results

                _save_bb_cache()

            # Фаза 3: сборка
            candidates = []
            for sym, bb, mark, turnover in ws_hits:
                self._score_candidate(candidates, sym, bb, mark, mode, interval, turnover=turnover)
            ticker_map = {t["symbol"]: float(t.get("turnover24h", 0)) for t in tickers}
            for sym, bb in rest_results.items():
                if bb:
                    # Найти mark из tickers
                    mark = 0.0
                    for t in tickers:
                        if t['symbol'] == sym:
                            mark = float(t.get('lastPrice', 0))
                            break
                    self._score_candidate(candidates, sym, bb, mark, mode, interval, turnover=ticker_map.get(sym, 0))

            candidates.sort(key=lambda s: s['score'], reverse=True)
            _json_response(self, candidates[:limit])

        except Exception as e:
            import logging
            logging.getLogger('bybit.rpc').error(f'Scan error: {e}')
            return _error(self, 'Internal scan error', str(e)[:500], 500)

    def _score_candidate(self, candidates, sym, bb, mark, mode, interval, turnover=0.0):
        """Добавить кандидата в список если проходит фильтр BB%."""
        bb_pct = bb.get('bb_pct', 50)
        if mode == 'long' and bb_pct > 35:
            return
        if mode == 'short' and bb_pct < 65:
            return
        score = round(min(10.0, max(0, (35 - bb_pct) * 0.4) if mode == 'long'
                      else max(0, (bb_pct - 65) * 0.4)), 1)
        candidates.append({
            'symbol': sym,
            'side': 'Buy' if mode == 'long' else 'Sell',
            'score': score,
            'price': mark,
            'bb_pct': round(bb_pct, 1),
            'bb_pos': round(bb_pct, 1),
            'lower_bb': bb.get('bb_lower'),
            'upper_bb': bb.get('bb_upper'),
            'middle_bb': bb.get('bb_sma'),
            'down_days': bb.get('bb_down_days', 0),
            'turnover': turnover,
            'rsi': bb.get('bb_rsi'),
            'mode': mode.upper(),
            'interval': interval,
        })

    def _handle_enter(self, body: dict):
        """POST /enter — ручной вход в позицию.

        Принимает: {"symbol": "XRPUSDT", "side": "Buy|Sell", "qty": 10,
                      "sl": 0.50, "tp": 0.55, "confirm": true}
        Если confirm: true — исполнение. Если false — только превью.
        """
        # ── Валидация ──
        symbol = body.get('symbol', '').strip().upper()
        if not symbol or not symbol.endswith('USDT'):
            return _error(self, 'Invalid symbol', 'symbol must be like XRPUSDT', 400, 'invalid_symbol')

        side = body.get('side', '').strip()
        if side not in ('Buy', 'Sell'):
            return _error(self, 'Invalid side', "side must be 'Buy' (LONG) or 'Sell' (SHORT)", 400, 'invalid_side')

        try:
            qty = float(body.get('qty', 0))
        except (ValueError, TypeError):
            return _error(self, 'Invalid qty', 'qty must be a number', 422, 'invalid_qty')

        if qty <= 0:
            return _error(self, 'Invalid qty', 'qty must be positive', 422, 'invalid_qty')

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
                          f'{symbol} already has an open {existing["side"]} position of size {existing["size"]}',
                          409, 'position_exists')

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

        # ── Размещение ордера ──
        # Валидация qty
        if not isinstance(qty, (int, float)) or qty <= 0:
            return _error(self, 'Invalid qty', 'qty must be a positive number')
        try:
            qty_int = int(qty)
            qty_str = str(qty_int) if qty == qty_int else str(qty)
        except (ValueError, TypeError):
            qty_str = str(qty)

        order_type = body.get('order_type', 'Market')
        limit_price = body.get('price')
        
        if order_type == 'Limit' and limit_price:
            # Limit order
            order_result = _api_call('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': symbol,
                'side': side,
                'orderType': 'Limit',
                'qty': qty_str,
                'price': str(limit_price),
                'timeInForce': 'GTC',
                'positionIdx': 0,
            })
            # Retry with positionIdx=1 для hedge mode
            if order_result.get('retCode') != 0 and 'position idx' in order_result.get('retMsg', ''):
                order_result = _api_call('POST', '/v5/order/create', {
                    'category': 'linear',
                    'symbol': symbol,
                    'side': side,
                    'orderType': 'Limit',
                    'qty': qty_str,
                    'price': str(limit_price),
                    'timeInForce': 'GTC',
                    'positionIdx': 1,
                })
        else:
            # Market order
            order_result = _api_call('POST', '/v5/order/create', {
                'category': 'linear',
                'symbol': symbol,
                'side': side,
                'orderType': 'Market',
                'qty': qty_str,
                'timeInForce': 'IOC',
                'positionIdx': 0,
            })
            # Retry with positionIdx=1 если hedge mode
            if order_result.get('retCode') != 0 and 'position idx' in order_result.get('retMsg', ''):
                order_result = _api_call('POST', '/v5/order/create', {
                    'category': 'linear',
                    'symbol': symbol,
                    'side': side,
                    'orderType': 'Market',
                    'qty': qty_str,
                    'timeInForce': 'IOC',
                    'positionIdx': 1,
                })

        if order_result.get('retCode') != 0:
            err_msg = order_result.get('retMsg', 'Unknown error')
            status = 400
            error_code = 'order_failed'
            if 'margin' in err_msg.lower() or 'balance' in err_msg.lower() or 'insufficient' in err_msg.lower():
                status = 402
                error_code = 'insufficient_margin'
                err_msg = f'Insufficient margin: {err_msg}'
            elif '110001' in err_msg:
                status = 422
                error_code = 'invalid_qty'
            elif 'symbol' in err_msg.lower() or 'not found' in err_msg.lower():
                status = 404
                error_code = 'symbol_not_found'
            return _error(self, 'Order failed', err_msg, status, error_code)

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
            if pos is None:
                result['sl'] = {'price': _round_price(sl), 'status': 'failed',
                                'detail': 'позиция не появилась за 3с — SL не поставлен'}
            else:
                sl_side = 'Sell' if side == 'Buy' else 'Buy'
                # Use actual positionIdx (hedge-safe)
                actual_idx = pos.get('positionIdx', 0)
                sl_result = _api_call('POST', '/v5/position/trading-stop', {
                    'category': 'linear',
                    'symbol': symbol,
                    'positionIdx': actual_idx,
                    'stopLoss': str(_round_price(sl)),
                    'slTriggerBy': 'MarkPrice',
                })
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
                tp_result = _api_call('POST', '/v5/order/create', {
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

        # ── Алерт о входе ──
        direction = '📈 LONG' if side == 'Buy' else '📉 SHORT'
        add_alert('ENTRY', f'{direction} {symbol}: вход {qty} шт. по рынку, SL={_round_price(sl) if sl else "нет"}, TP={_round_price(tp) if tp else "нет"}')

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

        close_result = _api_call('POST', '/v5/order/create', {
            'category': 'linear',
            'symbol': symbol,
            'side': close_side,
            'orderType': 'Market',
            'qty': qty_str,
            'positionIdx': pos['positionIdx'],
            'timeInForce': 'IOC',
            'reduceOnly': True,
        })

        if close_result.get('retCode') != 0:
            return _error(self, 'Close failed',
                          close_result.get('retMsg', 'Unknown error'), 400)

        order_id = close_result.get('result', {}).get('orderId', 'unknown')

        # ── Алерт о закрытии ──
        pnl = round((pos['mark'] - pos['entry']) * pos['size'] * (1 if pos['side'] == 'Buy' else -1), 2)
        # -- Фаза 5.3: запись исхода в A/B тест --
        try:
            from .ab_test import record_outcome_for_symbol
            record_outcome_for_symbol(symbol, 'MANUAL', pos['mark'], pnl)
        except Exception:
            pass
        direction = '📈 LONG' if pos['side'] == 'Buy' else '📉 SHORT'
        entry_str = str(pos['entry'])
        mark_str = str(pos['mark'])
        add_alert('TP', f'{direction} {symbol}: закрыт, PnL=${pnl}, вход={entry_str}, выход={mark_str}')

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

    def _handle_reset_token(self, body: dict):
        """POST /reset-token — сбросить RPC-токен (генерирует новый UUID).

        MONITOR.md §7: генерация/сброс токена через UUID.
        Требует действующий токен для авторизации.
        """
        import uuid
        new_token = str(uuid.uuid4())
        try:
            from .state_db import db
            db.set_kv('rpc_auth_token', new_token)
            _json_response(self, {
                'status': 'ok',
                'message': 'Token reset successful. Update your Authorization header.',
                'new_token': new_token,
            })
        except Exception as e:
            _error(self, 'Token reset failed', str(e), 500)


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
