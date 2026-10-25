# Architecture

The bybit-ws engine is a **single-process async event loop** that orchestrates trading logic in 30-second cycles. It has no framework dependencies (no Django, no FastAPI) — just raw `asyncio`, `requests`, and `websocket-client`.

---

## Main Loop (`main_async.py`)

The engine runs three tiers of processing within the main event loop:

```
CYCLE (30s)
├── LIGHT (every cycle):
│   ├── Snapshot positions (REST, 20s timeout)
│   ├── Import closed-pnl history from Bybit → trades.jsonl + post_trade_features.jsonl
│   ├── Black Swan check (3-tier: -3%/15min→50%, -5%/30min→80%, -8%/1h→100%)
│   ├── SL check + fix (ATR-adaptive, capped -50%/+50%)
│   ├── Trailing SL (LONG + SHORT)
│   ├── Breakeven SL (every 4 cycles)
│   └── Margin utilization check
│
├── HEAVY (every 10 cycles = 5 min):
│   ├── BB pre-fetch (batch, cache 5 min)
│   ├── Market regime (LSTM or NEUTRAL fallback)
│   ├── Auto-SHORT + Dry Spell throttle
│   ├── Correlations + Pumps + Overbought
│   ├── DCA + Partial TP
│   ├── Auto-Entry (LONG) — 7-filter pipeline:
│   │   ├── MTF Confluence (D+W+M)
│   │   ├── Orderbook Imbalance
│   │   ├── Volume Confirmation
│   │   ├── Entry Judge (LLM gate, 5s timeout)
│   │   ├── Correlation sizing (r>0.85→block, r>0.70→×0.5)
│   │   ├── Post-trade cluster analysis
│   │   └── Risk Manager (CB, margin, max pos, banned clusters)
│   ├── Auto-TP (ATR-adaptive: 1.0×/2.0×/3.0× ATR)
│   ├── TP/SL Self-Check (direct REST query)
│   └── Time-Based Exit (6h no PnL / 48h absolute)
│
├── DAILY (every 2880 cycles ≈ 24h):
│   ├── Post-trade cluster analysis (WR by clusters + auto-block)
│   └── Self-learning + Canary mode (min_score, sl_pct adjustment)
│
├── SL re-entry (with regime filter)
├── Heartbeat (every 12h)
└── Reporting (summary, profit triggers)
```

**Source:** `/bybit_ws/main_async.py` (~39k), `/bybit_ws/heavy_cycle_opt.py` (~15k)

### Async Pattern

- The main loop is `asyncio`-based with 30s `asyncio.sleep()` between cycles
- All I/O (REST calls to Bybit, SQLite writes) runs through `run_in_executor` to avoid blocking
- RPC server runs in a **background thread** with its own synchronous `HTTPServer`
- WebSocket push runs on `aiohttp` in another thread with its own event loop + `asyncio.run_coroutine_threadsafe` for cross-thread broadcast
- The old synchronous main loop (`main.py`) exists for backward compatibility

**Source:** `/bybit_ws/main_async.py` lines 1-100 (async helpers, SHUTDOWN flag, executor usage)

---

## SSOT: SQLite Database (`state_db.py`)

SQLite in WAL mode is the **single source of truth**. No dual-write to JSON files. JSON snapshots are read-only caches regenerated from SQLite on startup.

### Schema

| Table | Purpose | Key Columns |
|-------|---------|------------|
| `trade_history` | Trade audit trail | symbol, side, strategy, entry/exit price, pnl, fees, timestamps |
| `positions` | Open position cache | symbol, side, entry, mark, size, SL, TP, liq_price |
| `short_state` | Short position tracker | entry/exit price, bb_pct, is_junk, dca_level |
| `pump_state` | Pump detection tracking | peak_price, daily/weekly pump flags |
| `x10_limits` | Daily X10 loss limits | daily_loss, reset_at |
| `x10_positions` | X10 strategy positions | entry_price, atr-based sizing |
| `cooldowns` | Cooldown state | type, symbol, cooldown_until |
| `alert_dedup` | Alert deduplication | alert_hash, last_ts |
| `ab_tests` | A/B test results | name, variant, trades, wins, pnl |
| `post_trade_clusters` | Cluster analysis | symbol, cluster_key, win_rate, blocked |
| `features` | ML features cache | symbol, features_json |

**Configuration:** `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`

**Source:** `/bybit_ws/state_db.py` (~21k)

---

## RPC Server (`rpc.py`)

HTTP JSON-RPC server on **port 8766** with ~32 endpoints.

### Authentication

- **Bearer token** in `Authorization` header (`RPC_TOKEN` env var or config)
- **Emergency endpoints** require `X-Emergency-Auth` header (separate secret)
- **Grace period**: 5 minutes grace after token rotation (old+new tokens accepted)

### Endpoint Categories

| Category | Paths | Description |
|----------|-------|-------------|
| **Health** | `GET /health` | Alive check, uptime, cycle count |
| **Positions/Orders** | `GET /rpc/positions`, `/rpc/orders`, `/orders`, `/positions` | Active positions and orders |
| **Trades** | `GET /rpc/trades` | Trade history (paginated) |
| **Alerts/Metrics** | `GET /rpc/alerts`, `/rpc/metrics`, `/metrics` | Alerts and Prometheus metrics |
| **Risk** | `GET /rpc/risk`, `/rpc/risk_full`, `/rpc/circuit_breaker` | Risk limits, correlations, CB status |
| **Config** | `GET /rpc/config`, `POST /reload-config` | View and reload configuration |
| **Signals** | `GET /rpc/signals`, `/signals` | BB signals |
| **Trading** | `POST /enter`, `/close`, `/move_sl`, `/cancel_order`, `/set_leverage` | Trade execution |
| **Scan** | `POST /scan` | Run GridSignal scanner |
| **Control** | `POST /pause`, `/resume`, `/reset-token` | Engine control |
| **Emergency** | `POST /emergency_close`, `/kill_switch` | Emergency position close |
| **Paper** | `POST /paper/*` | Paper trading endpoints |
| **Logs** | `POST /logs` | Log retrieval |
| **ML** | `GET /rpc/ml_toggle`, `/rpc/ab_test_report` | ML pipeline control |
| **Analysis** | `GET /rpc/analyze_history`, `/rpc/symbol_stats` | Trade analysis |
| **WebSocket** | `ws://host:8766/ws?token=...` | Real-time push with heartbeat (30s) |

**Rate limiting:** Token bucket, 60 tokens/min, 1 token/sec recovery.

**Source:** `/bybit_ws/rpc.py` (~68k)

### MCP Server (`mcp_server.py`)

A secondary server implementing **Model Context Protocol** for AI-agent tool integration. Provides direct tool calling capabilities to Claude and other MCP-compatible agents.

**Source:** `/bybit_ws/mcp_server.py` (~19k)

---

## Exchange Layer

### REST API (`api.py`)

Native Bybit v5 REST client using `requests` + HMAC signing. All exchange communication flows through this module.

Key methods:
- `fetch_positions()` / `fetch_open_orders()` — snapshot
- `set_trading_stop()` — SL/TP placement
- `place_active_order()` — new orders
- `get_bb_data()` — Bollinger Bands from klines
- `cancel_active_order()` — order cancellation

**Pattern:** Retry with backoff ([1s, 3s, 10s]), 30s timeout, 3 retries.

**Source:** `/bybit_ws/api.py` (~28k)

### WebSocket Client (`ws_client.py`)

Manages Bybit public WebSocket streams:
- Kline data → BB cache
- Orderbook data
- (Optional) Real-time position data when `BYBIT_WS_FULL_ENABLED=1`

**Fallback:** If WS is disconnected, BB data is fetched via REST.

**Source:** `/bybit_ws/ws_client.py` (~30k)

### Exchange Adapter (`exchange_adapter.py`)

Abstraction layer for potential multi-exchange support. Currently only implements Bybit v5 but structured for adding Binance, OKX, etc.

**Source:** `/bybit_ws/exchange_adapter.py` (~20k)

---

## Entry Pipeline (7 Filters)

Every LONG entry candidate passes through 7 filters in sequence:

```
Candidate Symbol
├── 1. MTF Confirmation      (Daily+Weekly+Monthly confluence)  [fail-open]
├── 2. Orderbook Imbalance   (bid/ask ratio)                     [fail-open]
├── 3. Volume Confirmation   (vol vs SMA)                        [fail-open]
├── 4. Entry Judge           (LLM: Nemotron→DeepSeek, 5s)        [fail-closed]
├── 5. Correlation Sizing    (r>0.85→block, r>0.70→×0.5)        [fail-open]
├── 6. Post-trade Cluster    (WR<40% cluster→block)             [fail-open]
└── 7. Risk Manager          (CB, margin, max pos, banned)       [fail-closed]
```

**Note:** [fail-open] means the filter is skipped on error but the candidate gets a -10% score penalty. [fail-closed] means the entry is blocked on any error.

---

## Feature Flags System

Controlled via environment variables in `/bybit_ws/feature_flags.py`:

| Flag | Production | Purpose |
|------|-----------|---------|
| `ml_enabled` | ✅ 1 | RandomForest signal filter |
| `ws_bb_enabled` | ✅ 1 | WebSocket for kline/BB cache |
| `push_enabled` | ✅ 1 | ntfy + Telegram notifications |
| `dspy_enabled` | ❌ 0 | DSPy prompt optimization |
| `optuna_enabled` | ❌ 0 | Optuna hyperparameter tuning |
| `ws_full_enabled` | ❌ 0 | Real-time WebSocket positions |
| `ab_enabled` | ❌ 0 | A/B testing |
| `regime_auto` | ❌ 0 | Automatic LSTM regime |

**Source:** `/bybit_ws/feature_flags.py`

---

## Data Flow

```
                        ┌──────────────────────────┐
                        │      AI Agent (you)        │
                        │  Claude / GPT / Others     │
                        └─────┬──────────────────────┘
                              │ REST API (:8766) / MCP
                              ▼
┌────────────────────────────────────────────────────────┐
│                    main_async.py                        │
│  ┌─────────────── ASYNC LOOP (30s) ──────────────┐    │
│  │  Light cycle → Heavy cycle → Daily cycle       │    │
│  │  run_in_executor for sync modules               │    │
│  └────────────────────────────────────────────────┘    │
│         │                │              │               │
│         ▼                ▼              ▼               │
│  ┌──────────┐   ┌────────────┐   ┌──────────┐        │
│  │ api.py   │   │ state_db    │   │ rpc.py    │        │
│  │ (Bybit)  │   │ (SQLite)    │   │(:8766)    │        │
│  └──────────┘   └────────────┘   └──────────┘        │
│         │              │              │                │
└─────────┼──────────────┼──────────────┼────────────────┘
          │              │              │
          ▼              ▼              ▼
   Bybit REST API    ~/.local/share/   Dashboard/WS clients
                      bybit-ws/
```

---

## Source Map

| File | Lines | Role |
|------|-------|------|
| `main_async.py` | ~39k | Async main loop orchestrator |
| `rpc.py` | ~68k | HTTP JSON-RPC + WebSocket server |
| `state_db.py` | ~21k | SQLite single source of truth |
| `api.py` | ~28k | Bybit REST v5 client |
| `ws_client.py` | ~30k | WebSocket client (kline, orderbook) |
| `exchange_adapter.py` | ~20k | Multi-exchange abstraction |
| `config.py` | ~20k | YAML config with env var substitution |
| `heavy_cycle_opt.py` | ~15k | Heavy cycle optimization/asyncio.gather |
| `feature_flags.py` | ~3k | Environment variable feature flags |

---

## Change Guidance

When modifying the main loop (`main_async.py`):
- Always run `python test_logic_integrity.py` — it AST-parses `main_async.py` and verifies all `apply_*` functions are called
- The `SHUTDOWN` global flag must be checked in long-running operations
- Adding new heavy-cycle logic requires updating the cycle counter and the `test_logic_integrity.py` expectations

When modifying the database (`state_db.py`):
- Schema changes require WAL checkpoint and careful migration if `state.db` exists
- The `adb` async wrapper must mirror all `db` sync methods

When modifying RPC (`rpc.py`):
- New GET endpoints go in `do_GET()`, new POST endpoints in `do_POST()`
- Bearer token validation is in `_check_auth()`; emergency endpoints also check `_check_emergency_auth()`
- Document new endpoints in both `api.py` (OpenAPI) and the docs
