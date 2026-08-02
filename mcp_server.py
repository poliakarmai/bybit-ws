#!/usr/bin/env python3
"""Hermes MCP Server — exposes trading + VPN tools to any MCP-compatible agent.

Stdio transport: run this as a subprocess, MCP clients discover tools automatically.

Tools:
  - scan_market(mode, interval, limit) → GridSignal scanner
  - get_positions() → текущие позиции + PnL
  - get_metrics() → дневные метрики
  - vpn_status() → VPN health + трафик
  - get_candidates(mode, min_score) → топ-кандидаты для входа
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── RPC helpers ──────────────────────────────────────────────────────────────

RPC_PORT = 8766
RPC_BASE = f"http://127.0.0.1:{RPC_PORT}"

# Auth token: get from state.db KV store (same as rpc.py _get_auth_token)
STATE_DB_PATH = os.path.expanduser('~/.local/share/bybit-ws/state.db')

def _get_rpc_token() -> str:
    """Read RPC auth token from SQLite KV store. Auto-gen if missing."""
    import sqlite3
    import uuid
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        row = conn.execute("SELECT value FROM kv_store WHERE key='rpc_auth_token'").fetchone()
        if row:
            return row[0]
        # Auto-generate (shouldn't happen — rpc.py does this at startup)
        token = str(uuid.uuid4())
        conn.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES ('rpc_auth_token', ?)", (token,))
        conn.commit()
        return token
    except Exception:
        return str(uuid.uuid4())


def _rpc(endpoint: str) -> dict:
    """Call bybit-ws RPC endpoint with Bearer auth (GET)."""
    try:
        token = _get_rpc_token()
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", "10",
             "-H", f"Authorization: Bearer {token}",
             f"{RPC_BASE}/{endpoint}"],
            capture_output=True, text=True, timeout=12,
        )
        return json.loads(r.stdout) if r.stdout else {}
    except Exception as e:
        return {"error": str(e)}


def _rpc_post(endpoint: str, body: dict) -> dict:
    """Call bybit-ws RPC with POST + JSON body."""
    try:
        token = _get_rpc_token()
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "5", "--max-time", "15",
             "-X", "POST",
             "-H", f"Authorization: Bearer {token}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(body),
             f"{RPC_BASE}/{endpoint}"],
            capture_output=True, text=True, timeout=20,
        )
        return json.loads(r.stdout) if r.stdout else {}
    except Exception as e:
        return {"error": str(e)}


def _scanner(mode: str = "long", interval: str = "D", limit: int = 10) -> list:
    """Run gridSignal scanner. Extracts JSON from potentially noisy stdout."""
    try:
        scanner = os.path.expanduser("~/.local/bin/gridsignal_scanner.py")
        r = subprocess.run(
            ["python3", scanner, "--mode", mode, "--tf", interval, "--limit", str(limit)],
            capture_output=True, text=True, timeout=90,
        )
        stdout = r.stdout
        if not stdout:
            return []
        # Strip diagnostic lines (WARNING, ⚠️) — extract only JSON
        # JSON starts with '[' or '{'
        for marker in ('[', '{'):
            idx = stdout.find(marker)
            if idx >= 0:
                try:
                    return json.loads(stdout[idx:])
                except json.JSONDecodeError:
                    pass
        # Last resort: try raw
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return [{"error": "JSON parse failed", "stdout_preview": stdout[:200]}]
    except Exception as e:
        return [{"error": str(e)}]


def _vpn_watch() -> dict:
    """Read vpn-watch status."""
    try:
        path = "/opt/vpn-core/conf/vpn-watch-status.json"
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {"error": "vpn-watch data unavailable"}


def _vpn_clients() -> list:
    """List VPN clients from Xray config."""
    try:
        with open("/opt/vpn-core/conf/config.json") as f:
            cfg = json.load(f)
        return [
            {"email": c.get("email", "?"), "uuid": c["id"][:16] + "..."}
            for c in cfg["inbounds"][0]["settings"]["clients"]
        ]
    except Exception as e:
        return [{"error": str(e)}]


# ── Format helpers ───────────────────────────────────────────────────────────

def _fmt_positions(positions: list) -> str:
    lines = []
    total = 0
    for p in positions:
        pnl = float(p.get("upnl", 0))
        total += pnl
        side = p.get("side", "?")
        emoji = "🟢" if pnl > 0 else "🔴"
        lines.append(
            f"{emoji} {p['symbol']:12s} {side:4s} {p.get('leverage',1)}x  "
            f"entry=${float(p['entry']):.4f}  mark=${float(p['mark']):.4f}  "
            f"PnL=${pnl:+.2f}  SL=${p.get('stopLoss','?')}"
        )
    lines.append(f"\nTotal unrealized PnL: ${total:+.2f}")
    return "\n".join(lines)


def _fmt_candidates(data: list, mode: str) -> str:
    if not data:
        return "No candidates found."
    lines = [f"Top {mode.upper()} candidates:"]
    for s in data[:10]:
        entry = s.get("lower_bb", 0) * 0.95
        lines.append(
            f"  {s['symbol']:12s} Score={s.get('score',0):.1f}  Tier={s.get('tier','?')}  "
            f"BB={s.get('bb_pos',0):.0f}%  RSI={s.get('rsi',0):.0f}  "
            f"${s.get('price',0):.4f} → entry ${entry:.4f}"
        )
    return "\n".join(lines)


# ── MCP Server ───────────────────────────────────────────────────────────────

app = Server("hermes-tools")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="scan_market",
            description="Scan market for Bollinger Grid signals (LONG/SHORT candidates with scores)",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["long", "short"],
                        "description": "Scan mode: long or short",
                        "default": "long",
                    },
                    "interval": {
                        "type": "string",
                        "enum": ["D", "W", "4h", "1h", "15m", "5m"],
                        "description": "Candle interval",
                        "default": "D",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="get_positions",
            description="Get current trading positions with unrealized PnL, stop-losses, and leverage",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_metrics",
            description="Get daily trading metrics: SL/TP counts, entries, auto-entry stats",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="vpn_status",
            description="Get VPN health: service status, traffic rates, connected clients",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_risk_status",
            description="Get risk limits and current usage: daily PnL vs max_daily_loss, margin vs max_total_margin, entry blocked status",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_journal",
            description="Get trading journal analysis: FIFO-matched roundtrips, win rate, profit/loss ratio, disposition effect, overtrading, chasing, anchoring biases, and alerts",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="place_entry",
            description="Place a market or limit order to enter a LONG or SHORT position with SL/TP",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Trading pair, e.g. LINKUSDT",
                    },
                    "side": {
                        "type": "string",
                        "enum": ["Buy", "Sell"],
                        "description": "Buy=LONG, Sell=SHORT",
                    },
                    "qty": {
                        "type": "number",
                        "description": "Quantity in base units (e.g. 14 LINK)",
                    },
                    "sl": {
                        "type": "number",
                        "description": "Stop-loss price (optional)",
                    },
                    "tp": {
                        "type": "number",
                        "description": "Take-profit price (optional)",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["Market", "Limit"],
                        "description": "Order type: Market (default) or Limit (requires price)",
                        "default": "Market",
                    },
                    "price": {
                        "type": "number",
                        "description": "Limit price (required for Limit orders)",
                    },
                },
                "required": ["symbol", "side", "qty"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "scan_market":
        mode = arguments.get("mode", "long")
        interval = arguments.get("interval", "D")
        limit = arguments.get("limit", 10)
        data = _scanner(mode, interval, limit)
        text = _fmt_candidates(data, mode)

    elif name == "get_positions":
        data = _rpc("rpc/positions")
        if isinstance(data, list):
            text = _fmt_positions(data)
        else:
            text = f"Error: {data.get('error', 'no data')}"

    elif name == "get_metrics":
        data = _rpc("rpc/metrics")
        if data:
            today_key = __import__('datetime').datetime.now().strftime("%Y-%m-%d")
            today = data.get(today_key)
            # Fallback: take most recent date key
            if not today:
                return [TextContent(type="text", text="No metrics for today yet. First trade will create today's entry.")]
            if today:
                tp_coins = today.get('tp_coins', [])
                sl_coins = today.get('sl_coins', [])
                entry_coins = today.get('entry_coins', [])
                tp_names = ", ".join(tp_coins) if tp_coins else "—"
                sl_names = ", ".join(sl_coins) if sl_coins else "—"
                entry_names = ", ".join(entry_coins) if entry_coins else "—"
                text = (
                    f"Today's metrics:\n"
                    f"  TP: {today.get('tp_real',0)}  SL: {today.get('sl_real',0)}\n"
                    f"  TP coins: {tp_names}\n"
                    f"  SL coins: {sl_names}\n"
                    f"  Entries: {today.get('entry',0)}  Auto-filled: {today.get('auto_entry_filled',0)}\n"
                    f"  Entry coins: {entry_names}\n"
                    f"  Auto-entry PnL: ${today.get('auto_entry_pnl',0):.2f}"
                )
            else:
                text = f"Metrics raw: {json.dumps(data, indent=2)[:500]}"
        else:
            text = "Metrics unavailable."

    elif name == "vpn_status":
        status = _vpn_watch()
        clients = _vpn_clients()
        text = (
            f"VPN Status: {'✅' if status.get('traffic_active') else '❌'}\n"
            f"  Service: {status.get('service','?')}  Port: {status.get('port_status','?')}\n"
            f"  Traffic: ↓{status.get('rx_fmt','?')}  ↑{status.get('tx_fmt','?')}\n"
            f"  Errors: {status.get('xray_errors',0)}\n"
            f"  Clients: {len(clients)}"
        )

    elif name == "get_risk_status":
        data = _rpc("rpc/risk")
        if data and "error" not in data:
            status_icon = "🛑 BLOCKED" if data.get("blocked") else "🟢 OK"
            reasons = "\n".join(f"  • {r}" for r in data.get("reasons", [])) or "  (none)"
            text = (
                f"Risk Status: {status_icon}\n"
                f"  Daily PnL: ${data.get('daily_loss', 0):.2f} / -${data.get('max_daily_loss', 50)}\n"
                f"  Margin: ${data.get('total_margin', 0):.2f} / ${data.get('max_total_margin', 500)}\n"
                f"  Positions: {data.get('position_count', 0)}\n"
                f"  Remaining: ${data.get('remaining_daily_loss', 0):.2f} loss / ${data.get('remaining_margin', 0):.2f} margin\n"
                f"  Block reasons:\n{reasons}"
            )
        else:
            text = f"Risk status unavailable: {data.get('error', 'no data')}"

    elif name == "get_journal":
        data = _rpc("rpc/analyze_history")
        if data and "profile" in data:
            p = data["profile"]
            biases = data.get("biases", [])
            alerts = data.get("alerts", [])
            bias_lines = "\n".join(
                f"  [{b['severity'].upper():5}] {b['name']}: {b['evidence'][:100]}"
                for b in biases
            )
            alert_lines = "\n".join(f"  • {a}" for a in alerts) if alerts else "  (none)"
            text = (
                f"📊 Торговый дневник ({p['total_roundtrips']} round-trips):\n"
                f"  Total PnL: ${p['total_pnl']:.2f}  Win rate: {p['win_rate']:.0%}\n"
                f"  P/L ratio: {p['profit_loss_ratio']}  Avg hold: {p['avg_hold_hours']}h\n"
                f"  Max drawdown: ${p['max_drawdown']:.2f}\n"
                f"BIAS-диагностика:\n{bias_lines}\n"
                f"ALERTS:\n{alert_lines}"
            )
        else:
            text = f"Journal unavailable: {data.get('error', 'no data')}"

    elif name == "place_entry":
        symbol = arguments.get("symbol", "").upper()
        side = arguments.get("side", "Buy")
        qty = arguments.get("qty", 0)
        sl = arguments.get("sl")
        tp = arguments.get("tp")
        order_type = arguments.get("order_type", "Market")
        price = arguments.get("price")
        
        # ATR-сайзинг: если qty не указан — рассчитать автоматически
        if qty <= 0:
            try:
                from bybit_ws.position_sizing import atr_margin, suggest_qty
                sizing = atr_margin(symbol, score=5.5, side=side)
                if sizing.get('margin', 0) > 0:
                    # Получаем текущую цену
                    ticker = _rpc("rpc/all")
                    mark_price = price
                    if not mark_price and ticker and 'positions' not in ticker:
                        mark_price = None
                    if not mark_price:
                        # fetch from API
                        from bybit_ws.api import bybit as _api
                        t = _api('GET', f'/v5/market/tickers?category=linear&symbol={symbol}')
                        if t and t.get('retCode') == 0:
                            items = t['result'].get('list', [])
                            if items:
                                mark_price = float(items[0].get('lastPrice', 0))
                    
                    if mark_price and mark_price > 0:
                        qty = suggest_qty(sizing['margin'], mark_price)
                        log_msg = (f'ATR-сайзинг {symbol}: ATR=${sizing["atr"]:.4f}, '
                                   f'стоп-дист=${sizing["stop_distance"]:.4f}, '
                                   f'маржа=${sizing["margin"]:.1f} → qty={qty}')
                        _rpc("rpc/all")  # placeholder, log to events
                    else:
                        qty = 0
            except Exception as e:
                qty = 0
        
        body = {"symbol": symbol, "side": side, "qty": qty, "confirm": True}
        
        # Auto-SL/TP из BB если не заданы явно
        if not sl or not tp:
            try:
                from bybit_ws.api import get_bb_data as _bb
                bb = _bb(symbol, interval='D')
                if bb:
                    if side == "Buy":
                        # LONG: SL ниже Lower BB на 7%, TP на Middle BB
                        if not sl:
                            sl = round(bb["lower"] * 0.93, 8)
                        if not tp:
                            tp = round(bb["sma"], 8)
                    else:
                        # SHORT: SL выше Upper BB на 7%, TP на Middle BB
                        if not sl:
                            sl = round(bb["upper"] * 1.07, 8)
                        if not tp:
                            tp = round(bb["sma"], 8)
            except Exception:
                pass  # нет BB — без SL/TP
        
        if sl:
            body["sl"] = sl
        if tp:
            body["tp"] = tp
        if order_type == "Limit" and price:
            body["order_type"] = "Limit"
            body["price"] = price
        
        data = _rpc_post("enter", body)
        if data.get("status") == "ok":
            text = (
                f"✅ Entry: {data['symbol']} {data['side']} {data['qty']}шт ({order_type}"
            )
            if price:
                text += f" @ ${price}"
            text += (
                f")\\n"
                f"   Order ID: {data.get('order_id', '?')}\\n"
            )
            if data.get("sl"):
                text += f"   SL: ${data['sl']['price']} ({data['sl']['status']})\\n"
            if data.get("tp"):
                text += f"   TP: ${data['tp']['price']} ({data['tp']['status']})"
        else:
            text = f"❌ Entry failed: {data.get('detail', data.get('error', '?'))}"

    else:
        text = f"Unknown tool: {name}"

    return [TextContent(type="text", text=text)]


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
