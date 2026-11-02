# bybit-ws API Reference

**English** · [Русский](API.md)

> HTTP REST API of the trading monitor (`rpc.py`, **`http.server`**, not JSON-RPC).
> Verified against code 2026-11-02 (32 GET and 17 POST in `rpc.py`; all GET additionally
> passed live probing with token — 200), product version 11.5.0, `api_version: "v1"` in every response.
> The route table in `rpc.py` (`do_GET` / `do_POST`) is the source of truth; anything not listed there does not exist.

## Authentication

`Authorization: Bearer <token>` (the token is `kv_store['rpc_auth_token']` in `state.db`, or a JWT for Android).
Rotation: `POST /reset-token`.

**Without a token (public):** only `/health`. Everything else — including `/metrics`, `/rpc/paths`, `/dashboard`, and `/rpc/dash/*` — requires Bearer (verified live on 01.11.2026: `401`).

**Dashboard:** the server also serves the `GET /dashboard` page only with Bearer. The `?token=<UUID>` parameter is not needed by the server. The page stores it in `localStorage` and then sends it to `/rpc/dash/*`.

**Emergency** endpoints (`/kill_switch`, `/emergency_close`) require **two** headers — `Authorization: Bearer <token>` **and** `X-Emergency-Auth: <EMERGENCY_TOKEN>` — **only if** `EMERGENCY_TOKEN` is set in the service environment. Without that variable the second check is skipped (`rpc.py:540`, `if emergency and not compare_digest(...)`) and Bearer alone is enough. As of 01.11.2026 the variable is absent from `~/.config/bybit-ws/env`: check with `grep -c EMERGENCY_TOKEN ~/.config/bybit-ws/env`.

⚠️ Production: only through nginx/Cloudflare (TLS externally); the server itself listens on `127.0.0.1:8766`.

## Response Format

```json
{"api_version":"v1","status":"ok","<data>":"..."}
{"api_version":"v1","error":"description","detail":"details","status":400}
```

Codes: `200`, `400` (invalid JSON/parameters), `401` (token), `404` (route not found), `429` (rate limit), `500`.

## Rate Limit

One limit per IP: `rpc.rate_limit_per_min` from the configuration (default **60/minute**), applied equally to GET and POST. There are no separate limits per endpoint.

---

## GET

### Public

| Path | Purpose |
|------|-----------|
| `/health` | `status` (`alive`/`stale`), `alive`, `uptime`, `cycle_count`, `trading_ready` (pause/CB → false) |

The only public GET endpoint. Everything else, including `/rpc/paths`, `/dashboard`, and `/rpc/dash/*`,
returns `401` without Bearer — verified live on 26.10.2026.

### With Token

| Path | Purpose |
|------|-----------|
| `/rpc` · `/` | Index: service name, `api_version`, list of all endpoints (for external agents) |
| `/rpc/health` | Same as `/health`, but with Bearer (alias for clients that always send a token) |
| `/rpc/paths` | Installation paths (state.db, events.log, configuration, port, systemd unit name) for external agents |
| `/dashboard` | HTML dashboard (the server requires Bearer; `?token=` is for its own requests to `/rpc/dash/*`) |
| `/rpc/dash/all` · `/rpc/dash/risk` · `/rpc/dash/signals` | Dashboard data (all / risk / signals) |
| `/positions` · `/rpc/positions` | Open positions + uPnL (raw Bybit `side`: **Buy=LONG, Sell=SHORT**) |
| `/orders` · `/rpc/orders` | Active orders (SL/TP) |
| `/balance` | Account balance (USD, account level — not USDT coins): `balance` (`totalWalletBalance`), `available` (`totalAvailableBalance`), `equity` (`totalEquity`) |
| `/risk` · `/rpc/risk` · `/rpc/risk_full` | Limits, daily PnL, circuit breaker, correlations |
| `/rpc/circuit_breaker` | CB status (reset — service restart, no separate POST) |
| `/signals` · `/rpc/signals` | Latest BB signals LONG/SHORT |
| `/config` · `/rpc/config` | Configuration without secrets |
| `/rpc/trades` | Trade history from `trades.jsonl` |
| `/rpc/alerts` | Latest alerts |
| `/rpc/all` | Everything in one request (positions, orders, alerts, metrics, trades) |
| `/rpc/metrics` | Metrics (JSON) |
| `/metrics` | **Prometheus** text/plain (`bybit_ws_active_positions`, `bybit_ws_unrealized_pnl`, `bybit_ws_uptime_seconds`, `bybit_ws_cycle_duration_seconds`, `bybit_ws_cycle_count`, `bybit_ws_daily_pnl`) |
| `/rpc/ab_test_report` · `/rpc/ab_status` | A/B report and status |
| `/rpc/ml_toggle` | ML gate toggle |
| `/rpc/symbol_stats` | Symbol statistics |
| `/rpc/analyze_history` | Analysis history |

`/positions`:

```json
{"api_version":"v1","positions":{"AAVEUSDT":{"symbol":"AAVEUSDT","side":"Sell","leverage":"10",
  "entryPrice":"98.60","markPrice":"93.50","unrealisedPnl":"4.18","stopLoss":"95.00","size":"1"}},
 "total_unrealised_pnl":"4.18","count":1}
```

## POST (Bearer required)

| Path | Purpose | Body |
|------|-----------|------|
| `/scan` | GridSignal scan | `{mode:"long"\|"short", interval:"D"\|"W"\|"4h"\|"1h"\|"15m"\|"5m", symbol?}` |
| `/calc_qty` | Size calculation | `{symbol, risk_pct, leverage}` |
| `/enter` | Entry | `{symbol, side, qty, sl?, tp?, leverage?, confirm}` |
| `/close` | Close position | `{symbol}` |
| `/move_sl` | Move SL/TP | `{symbol, stop_loss, take_profit?}` |
| `/set-tp` | Set TP | `{symbol, tp}` |
| `/kill_switch` | Close all + block until `/resume` | — · **+ `X-Emergency-Auth`** |
| `/emergency_close` | Close all without blocking | `{symbol?}` · **+ `X-Emergency-Auth`** |
| `/pause` · `/resume` | Pause/resume automatic entries (all sources) | — |
| `/reload-config` | Reload configuration | — |
| `/logs` | Latest log lines | `{lines?}` |
| `/reset-token` | New RPC token | — |
| `/generate-jwt` | JWT for Android | — |
| `/paper/balance` · `/paper/positions` · `/paper/summary` | Paper Trading (`BYBIT_PAPER_ENABLED=1`) | — |

### `/enter` — Two-Step Confirm

```bash
# 1) preview
curl -s -X POST http://127.0.0.1:8766/enter -H "Authorization: Bearer $TOKEN" \
  -d '{"symbol":"LINKUSDT","side":"Sell","qty":14,"sl":15.50,"tp":14.00}'
# 2) execution (without confirm:true, the order is NOT placed)
curl -s -m 60 -X POST http://127.0.0.1:8766/enter -H "Authorization: Bearer $TOKEN" \
  -d '{"symbol":"LINKUSDT","side":"Sell","qty":14,"sl":15.50,"tp":14.00,"confirm":true}'
```

`side`: `Buy`=LONG, `Sell`=SHORT. `leverage` (1–100) is optional: if provided, exactly that value is set; if omitted, leverage is not forced (`/enter`), while the automatic branch sets the configured value through `api.set_leverage()`. `-m 60` is required: Bybit responds within up to 30–40 seconds.

## WebSocket

`ws://127.0.0.1:8768/ws` — **separate port** (aiohttp, started from `main_async.py`; default in `start_ws_server()` — 8767, live launch — 8768). Not 8766.

Push: state snapshot on connection (`position_update`, `alert`, `order_update`).
Authentication is at the network level (bind to loopback); there is no token in the URL.

## What Is NOT in the API

Do not invent: `/alerts`, `/trades`, `/summary`, `/cancel_order`, `/order`, `/webhook`, `/openapi.json`, `/set_leverage`, `/dash/dashboard`, `POST /circuit_breaker`. CB reset is a service restart. `/rpc/all` exists, but as a JSON aggregate.

To check whether a route exists, use the code, not this table:

```bash
python3 - <<'EOF'
import re
code = open('rpc.py', encoding='utf-8').read()
for m in ('GET', 'POST'):
    body = code[code.index(f'def do_{m}'):]
    body = body[:body.find('\n    def ', 10)] if '\n    def ' in body[10:] else body
    print(m, sorted(set(re.findall(r'path == "(/[^"]+)"', body))))
EOF
```

## MCP Tools (through Hermes)

| Tool | → REST |
|-----------|--------|
| `scan_market(mode, interval)` | `POST /scan` |
| `get_positions()` | `GET /positions` |
| `get_metrics()` | `GET /rpc/metrics` |
| `get_risk_status()` | `GET /risk` |
| `place_entry(symbol, side, qty, sl, tp, order_type, price, leverage)` | `POST /enter` |
| `get_journal()` | `GET /rpc/trades` + FIFO analysis |

## Changes

| Date | Change |
|------|-----|
| 2026-10-25 | File rewritten based on actual `rpc.py` routes: nonexistent endpoints removed, WS port corrected (8768), rate limit corrected (one per IP), public list corrected, `/health.trading_ready`, `X-Emergency-Auth` |
| 2026-06-28 v7.1 | REST instead of JSON-RPC, string types, Idempotency-Key, `/balance`, `/summary` |
| 2026-06-29 Phase 7 | Paper Trading `/paper/*` |
