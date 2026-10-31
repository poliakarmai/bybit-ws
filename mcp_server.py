#!/usr/bin/env python3
"""Hermes MCP Server — exposes trading + VPN tools to any MCP-compatible agent.

Stdio transport: run this as a subprocess, MCP clients discover tools automatically.

Tools:
  - scan_market(mode, interval, limit) → GridSignal scanner
  - get_positions() → текущие позиции + PnL
  - get_metrics() → дневные метрики
  - vpn_status() → VPN health + трафик
  - get_candidates(mode, min_score) → топ-кандидаты для входа
  - backtest_run(symbol, days, interval, params) → исследовательский прогон backtest_runner
  - experiment_list(limit) → последние эксперименты из experiments.db
  - validation_report(run_id) → DSR/PBO/permutation из артефакта прогона
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from bybit_ws.alerts import log_event

EXPERIMENTS_DIR = Path.home() / '.local' / 'share' / 'bybit-ws' / 'experiments'
EXPERIMENTS_DB = EXPERIMENTS_DIR / 'experiments.db'

# ponytail: два одновременных бэктеста конкурируют за SQLite и кэш свечей —
# single-flight лок ограничивает потолок одним прогоном в момент времени
_BACKTEST_LOCK = threading.Lock()

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
        sl = p.get("stop_loss", p.get("stopLoss")) or "—"
        lines.append(
            f"{emoji} {p['symbol']:12s} {side:4s} {p.get('leverage',1)}x  "
            f"entry=${float(p['entry']):.4f}  mark=${float(p['mark']):.4f}  "
            f"PnL=${pnl:+.2f}  SL=${sl}"
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
            name="backtest_run",
            description="Run reproducible backtest via bybit_ws.experiments.backtest_runner. Пишет только в experiments.db (боевой state.db не трогает), 180s timeout, single-flight. Returns metrics: trades, win_rate, pnl, total_fees, equity_hash, git_commit",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Trading pair, e.g. SOLUSDT",
                    },
                    "days": {
                        "type": "integer",
                        "description": "History depth (days)",
                        "default": 30,
                    },
                    "interval": {
                        "type": "string",
                        "description": "Candle interval (D, 240, 60, ...)",
                        "default": "240",
                    },
                    "params": {
                        "type": "object",
                        "description": "Optional param overrides (JSON object)",
                    },
                },
                "required": ["symbol"],
            },
        ),
        Tool(
            name="experiment_list",
            description="List recent experiments from experiments.db (read-only). Returns experiment id, name, last run metrics",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max experiments to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="validation_report",
            description="Get DSR/PBO/permutation validation from a saved run artifact or DB. Returns honest 'no data' if missing",
            inputSchema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "integer",
                        "description": "Run ID from experiment_list or backtest_run output",
                    },
                },
                "required": ["run_id"],
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
                    "leverage": {
                        "type": "integer",
                        "description": "Leverage 1-100. Omitted → from strategy config (strategy.long/short.leverage). Actual leverage is read back after entry; mismatch is reported",
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

    elif name == "backtest_run":
        if not _BACKTEST_LOCK.acquire(blocking=False):
            text = "⏳ Бэктест уже запущен, подожди завершения"
        else:
            try:
                symbol = arguments.get("symbol", "").upper()
                days = int(arguments.get("days", 30))
                interval = str(arguments.get("interval", "240"))
                params_arg = arguments.get("params")
                out_fd, out_path = tempfile.mkstemp(suffix='.json', prefix='bt_')
                os.close(out_fd)
                cmd = [
                    sys.executable, "-m", "bybit_ws.experiments.backtest_runner",
                    "--symbol", symbol, "--days", str(days), "--interval", interval,
                    "--out", out_path, "--json"   # пишет только в experiments.db
                ]
                params_path = None
                if params_arg:
                    pf, params_path = tempfile.mkstemp(suffix='.json', prefix='params_')
                    with os.fdopen(pf, 'w') as pfile:
                        json.dump(params_arg, pfile)
                    cmd.extend(["--params", params_path])
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                    if r.returncode != 0:
                        text = f"❌ Backtest error:\n{r.stderr or r.stdout or 'unknown error'}"
                    elif not os.path.exists(out_path):
                        text = "❌ Backtest completed but no artifact found"
                    else:
                        with open(out_path) as af:
                            artifact = json.load(af)
                        metrics = artifact.get("metrics", {})
                        manifest = artifact.get("inputs_manifest", {})
                        text = (
                            f"✅ Backtest {symbol} {days}d {interval}:\n"
                            f"  Trades: {metrics.get('n_trades', 0)} "
                            f"({metrics.get('wins', 0)}W/{metrics.get('losses', 0)}L)\n"
                            f"  Win Rate: {metrics.get('win_rate', 0):.2f}%\n"
                            f"  PnL: ${metrics.get('total_pnl', 0):+.2f} "
                            f"({metrics.get('total_pnl_pct', 0):+.2f}%)\n"
                            f"  Total Fees: ${metrics.get('total_fees', 0):.2f}\n"
                            f"  Equity Hash: {artifact.get('equity_hash', 'n/a')[:16]}…\n"
                            f"  Git Commit: {manifest.get('git_commit', 'n/a') or 'n/a'}"
                        )
                except subprocess.TimeoutExpired:
                    text = f"⏱️ Backtest timeout (>180s) for {symbol}"
                except Exception as e:
                    text = f"❌ Backtest exception: {e}"
                finally:
                    for p in (out_path, params_path):
                        if p and os.path.exists(p):
                            os.unlink(p)
            finally:
                _BACKTEST_LOCK.release()

    elif name == "experiment_list":
        limit = int(arguments.get("limit", 10))
        if not EXPERIMENTS_DB.exists():
            text = (f"📋 БД экспериментов отсутствует: {EXPERIMENTS_DB}\n"
                    f"   Запусти backtest_run чтобы создать первый эксперимент.")
        else:
            try:
                conn = sqlite3.connect(
                    f"file:{EXPERIMENTS_DB}?mode=ro", uri=True, timeout=5)
                try:
                    rows = conn.execute("""
                        SELECT e.id, e.name, e.hypothesis,
                               r.id as run_id, r.metrics_json, r.created_at
                        FROM experiment e
                        LEFT JOIN run r ON r.experiment_id = e.id
                        ORDER BY e.id DESC, r.id DESC
                        LIMIT ?
                    """, (limit,)).fetchall()
                finally:
                    conn.close()
                if not rows:
                    text = "📋 Экспериментов пока нет"
                else:
                    lines = [f"📋 Последние {len(rows)} эксперимент(ов):"]
                    for row in rows:
                        exp_id, name, hyp, run_id, metrics_json, created = row
                        metrics = json.loads(metrics_json) if metrics_json else {}
                        pnl = metrics.get('total_pnl', 0)
                        wr = metrics.get('win_rate', 0)
                        n_trades = metrics.get('n_trades', 0)
                        lines.append(
                            f"  [{exp_id}] {name} — run {run_id or '—'}: "
                            f"{n_trades} trades, WR {wr:.1f}%, PnL ${pnl:+.2f}"
                        )
                    text = "\n".join(lines)
            except sqlite3.OperationalError as e:
                text = f"⚠️ Ошибка чтения БД (возможно заблокирована): {e}"
            except Exception as e:
                text = f"❌ Ошибка: {e}"

    elif name == "validation_report":
        run_id = int(arguments.get("run_id", 0))
        runs_dir = EXPERIMENTS_DIR / "runs"
        artifact_path = runs_dir / f"run_{run_id}.json"
        artifact = None
        if artifact_path.exists():
            try:
                with open(artifact_path) as f:
                    artifact = json.load(f)
            except Exception:
                pass
        if artifact is None and EXPERIMENTS_DB.exists():
            try:
                conn = sqlite3.connect(
                    f"file:{EXPERIMENTS_DB}?mode=ro", uri=True, timeout=5)
                try:
                    row = conn.execute(
                        "SELECT inputs_manifest, metrics_json, verdict "
                        "FROM run WHERE id=?", (run_id,)
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    manifest, metrics_json, verdict = row
                    artifact = {
                        "inputs_manifest": json.loads(manifest) if manifest else {},
                        "metrics": json.loads(metrics_json) if metrics_json else {},
                        "verdict": verdict,
                    }
            except Exception:
                pass
        if artifact is None:
            text = (f"❓ Нет данных для run_id={run_id}\n"
                    f"   Проверь experiment_list для списка доступных прогонов.")
        else:
            metrics = artifact.get("metrics", {})
            manifest = artifact.get("inputs_manifest", {})
            verdict = artifact.get("verdict", "—")
            text = (
                f"🔬 Validation Report (run {run_id}):\n"
                f"  Trades: {metrics.get('n_trades', 0)} "
                f"({metrics.get('wins', 0)}W/{metrics.get('losses', 0)}L)\n"
                f"  Win Rate: {metrics.get('win_rate', 0):.2f}%\n"
                f"  Profit Factor: {metrics.get('profit_factor', 0) or 'n/a'}\n"
                f"  PnL: ${metrics.get('total_pnl', 0):+.2f} "
                f"({metrics.get('total_pnl_pct', 0):+.2f}%)\n"
                f"  Max DD: {metrics.get('max_drawdown_pct', 0):.2f}%\n"
                f"  Sharpe: {metrics.get('sharpe_ratio', 0):.2f}\n"
                f"  Git Commit: {manifest.get('git_commit', 'n/a') or 'n/a'}\n"
                f"  Verdict: {verdict or '—'}\n"
                f"  (полный DSR/PBO/permutation: python3 -m bybit_ws.experiments.validation)"
            )

    elif name == "place_entry":
        symbol = arguments.get("symbol", "").upper()
        side = arguments.get("side", "Buy")
        qty = arguments.get("qty", 0)
        sl = arguments.get("sl")
        tp = arguments.get("tp")
        order_type = arguments.get("order_type", "Market")
        price = arguments.get("price")
        leverage = arguments.get("leverage")
        if leverage is None:
            # Дефолт — плечо стратегии из конфига (Buy → long, Sell → short)
            try:
                from bybit_ws.config import Config
                _strat = Config().strategy.long if side == "Buy" else Config().strategy.short
                leverage = int(_strat.get("leverage", 3))
            except Exception:
                leverage = None
        
        # ATR-сайзинг: если qty не указан — рассчитать автоматически.
        # Цена нужна ДО расчёта маржи: с price+leverage сайзинг риск-постоянный
        # (margin = риск / (стоп% × плечо)), иначе позиция гуляет по риску в 3+ раза.
        if qty <= 0:
            try:
                from bybit_ws.position_sizing import atr_margin, suggest_qty
                ticker = _rpc("rpc/all")
                mark_price = price
                if not mark_price and ticker and 'positions' not in ticker:
                    mark_price = None
                if not mark_price:
                    from bybit_ws.api import bybit as _api
                    t = _api('GET', f'/v5/market/tickers?category=linear&symbol={symbol}')
                    if t and t.get('retCode') == 0:
                        items = t['result'].get('list', [])
                        if items:
                            mark_price = float(items[0].get('lastPrice', 0))

                lev = int(leverage) if leverage else 10
                sizing = atr_margin(symbol, score=5.5, side=side,
                                    price=mark_price, leverage=lev)
                if sizing.get('margin', 0) > 0 and mark_price and mark_price > 0:
                    qty = suggest_qty(sizing['margin'], mark_price, leverage=lev)
                    log_event(f'ATR-сайзинг {symbol}: ATR=${sizing["atr"]:.4f}, '
                              f'стоп={sizing.get("stop_pct", 0)*100:.1f}% '
                              f'(дист ${sizing["stop_distance"]:.4f}), '
                              f'маржа=${sizing["margin"]:.1f} @ {lev}x → qty={qty}')
                else:
                    qty = 0
            except Exception as e:
                log_event(f'⚠️ ATR-сайзинг {symbol}: {e}')
                qty = 0

        # Fail-closed: qty=0 после автосайзинга (позиция меньше шага лота,
        # цена не получена) — не шлём заведомо невалидный /enter.
        if qty <= 0:
            return [TextContent(
                type="text",
                text=f"❌ qty too small: ATR-сайзинг дал 0 контрактов для {symbol} "
                     f"(позиция меньше шага лота) — увеличь маржу или укажи qty вручную",
            )]

        body = {"symbol": symbol, "side": side, "qty": qty, "confirm": True}
        if leverage is not None:
            body["leverage"] = leverage
        
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
            # Фактическое плечо: из ответа /enter, иначе из живых позиций
            actual_lev = data.get("leverage")
            if actual_lev is None:
                try:
                    for _p in (_rpc("rpc/positions") or []):
                        if isinstance(_p, dict) and _p.get("symbol") == symbol:
                            actual_lev = _p.get("leverage")
                            break
                except Exception:
                    actual_lev = None
            text += f"\n   Leverage: {actual_lev if actual_lev is not None else '?'}x"
            if leverage is not None:
                text += f" (requested {leverage}x)"
            text += "\n"
            if (leverage is not None and actual_lev is not None
                    and int(float(actual_lev)) != int(leverage)):
                text += (f"   ⚠️ LEVERAGE MISMATCH: запрошено {leverage}x, на бирже {actual_lev}x — "
                         f"SL/ликвидация считаются от другого плеча\n")
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
