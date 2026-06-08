"""RPC-сервер bybit-ws — HTTP JSON API для дашборда и внешних потребителей.

Запускается как фоновый поток в main.py.
Порт: 8766 (рядом с дашбордом 8765).

Endpoints:
    GET /rpc/all         — все данные одним запросом (позиции, ордера, алерты, метрики, трейды)
    GET /rpc/positions   — открытые позиции
    GET /rpc/orders      — активные ордера
    GET /rpc/health      — статус монитора (alive, uptime, cycle_count)
    GET /rpc/trades      — трейд-лог (trades.jsonl)
    GET /rpc/alerts      — последние алерты
    GET /rpc/metrics     — метрики (daily)
"""

import json
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"

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


class RPCHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # тихий режим

    def do_GET(self):
        path = self.path.rstrip("/")

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

    def _handle_index(self):
        _json_response(self, {
            "service": "bybit-ws-rpc",
            "version": "1.0",
            "endpoints": [
                "/rpc/all", "/rpc/positions", "/rpc/orders",
                "/rpc/health", "/rpc/trades", "/rpc/alerts", "/rpc/metrics"
            ]
        })

    def _handle_all(self):
        """Все данные одним запросом — для дашборда."""
        positions_raw = _load_json(DATA_DIR / "positions.json")
        orders_raw = _load_json(DATA_DIR / "orders.json")
        metrics = _load_json(DATA_DIR / "metrics.json")

        # Включаем ключ (символ) в каждую позицию/ордер
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

        # Алерты (последние 20 строк из new_alerts.txt)
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

        # Трейды (последние 50)
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
        # Включаем ключ (символ) в каждую позицию
        result = []
        if isinstance(positions, dict):
            for sym, p in positions.items():
                if isinstance(p, dict):
                    p = dict(p)  # копия
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


def start_rpc_server(port=8766):
    """Запустить RPC-сервер в фоновом потоке."""
    server = HTTPServer(("0.0.0.0", port), RPCHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="bybit-rpc")
    thread.start()
    return server


def update_health(alive=True, cycle_count=0, cycle_duration=0.0):
    """Обновить глобальное состояние RPC (вызывается из main_loop)."""
    rpc_state["alive"] = alive
    rpc_state["cycle_count"] = cycle_count
    rpc_state["last_cycle"] = time.time()
    rpc_state["cycle_duration"] = cycle_duration
