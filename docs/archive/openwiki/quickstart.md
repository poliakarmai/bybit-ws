# bybit-ws — Quickstart

**bybit-ws** is an AI-native, autonomous futures trading engine for [Bybit](https://bybit.com). It runs 24/7 as a systemd service (~45 MB RAM), executing a **Bollinger Grid** strategy across LONG (3x) and SHORT (3x) positions with adaptive ATR-based SL/TP, trailing stops, DCA ladders, correlation hedging, and a multi-tier risk management system.

The engine is built for **AI-agent orchestration**: it exposes a JSON-RPC API (port 8766) and an MCP server that agents use to monitor positions, execute trades, and reload configuration — all without direct exchange API access.

**Current version:** v7.7 (see [CHANGELOG.md](/CHANGELOG.md))

---

## Quick Start

### Prerequisites

- Python 3.11+
- A Bybit account with API key (permissions: futures trade + read)
- Linux system with systemd (user-level) — or Docker

### Installation

```bash
# Clone the repository
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws

# Create and activate virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
# Copy the example config and edit
cp config.example.yaml ~/.config/bybit-ws/config.yaml

# Set up environment variables (loaded by systemd)
mkdir -p ~/.config/bybit-ws
cat > ~/.config/bybit-ws/env << 'EOF'
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
RPC_TOKEN=your_rpc_token
TG_BOT_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_telegram_chat_id
NTFY_TOPIC=your_ntfy_topic
EOF
```

The configuration system supports environment variable substitution (`${VAR}` and `${VAR:-default}`) in YAML. See [config.example.yaml](/config.example.yaml) for all 734 lines of documented parameters.

### Running

```bash
# As a systemd user service (recommended)
systemctl --user link "$PWD/deploy/bybit-ws-async.service"
systemctl --user daemon-reload
systemctl --user start bybit-ws-async

# Check status
systemctl --user status bybit-ws-async

# Or run directly for testing
python -m bybit_ws.main_async
```

### Quick Health Check

```bash
curl http://localhost:8766/health
# {"alive":true,"uptime":1234,"cycle_count":42,...}
```

---

## Repository Map

This is a **flat Python package** (`bybit_ws/`) with ~90 source modules (~9500+ lines). The engine has no framework dependencies — it uses raw `asyncio`, `requests`, and `websocket-client`.

| Area | Key Files | Description |
|------|-----------|-------------|
| **Main Loop** | `main_async.py`, `main.py` | Async orchestration (30s cycles) |
| **API Layer** | `api.py`, `exchange_adapter.py` | Bybit REST/WS clients |
| **SSOT** | `state_db.py` | SQLite (WAL) — the single source of truth |
| **RPC Server** | `rpc.py`, `mcp_server.py` | JSON-RPC (port 8766) + MCP for agents |
| **LONG Strategy** | `auto_entry.py`, `auto_tp.py`, `dca.py`, `sl_reentry.py` | Bollinger Grid LONG entries |
| **SHORT Strategy** | `auto_short.py`, `auto_sl.py`, `trailing_sl.py`, `junk_trail.py` | SHORT entries + JUNK pump-shorts |
| **X10 Scalp** | `bb_scalp.py`, `mean_revert.py`, `funding_entry.py`, `atr_sizer.py`, `x10_limits.py` | High-leverage scalping (10x) |
| **Risk** | `risk_manager.py`, `health.py`, `margin_alerts.py` | BlackSwan, circuit breaker, limits |
| **Filters** | `mtf_confirmation.py`, `orderbook_filter.py`, `volume_filter.py`, `entry_judge.py`, `correlation.py` | 7-stage entry pipeline |
| **ML** | `ml_scorer.py`, `lstm_regime.py`, `ensemble.py`, `rl_agent.py`, `rl_env.py` | ML signal scoring + regime detection |
| **Optimization** | `optuna_tuner.py`, `dspy_optimizer.py`, `optimize_params.py`, `walk_forward_validate.py` | Parameter tuning |
| **Notifications** | `push_notifier.py`, `alerts.py` | ntfy + Telegram alerts |
| **Journal/Learning** | `journal/` (self_learn.py, analyzer.py, adapter.py) | Self-learning with canary mode |
| **Dashboard** | `dashboard.py`, `web/` | SVG dashboard + web UI |
| **Paper Trading** | `paper_trading.py`, `paper_api.py` | Simulated trading environment |
| **Deployment** | `deploy.sh`, `Dockerfile`, `docker-compose.yml` | Systemd + Docker |
| **Testing** | `test_smoke.py`, `test_modules.py`, `test_regression.py`, `test_mtf.py`, `test_ws_client.py` | ~113 tests across 8 files |

---

## Documentation Sections

| Page | Contents |
|------|----------|
| [Architecture](architecture.md) | Main loop (light/heavy/daily cycles), SSOT (SQLite), RPC/WS servers, async pattern, data flow |
| [Strategies](strategies.md) | LONG Grid, SHORT, JUNK, X10 scalps — entry conditions, SL/TP, DCA, 7-filter pipeline, tiers, session params, post-trade analysis |
| [Operations](operations.md) | Risk management (BlackSwan 3-tier, circuit breaker), deployment (systemd, Docker), notifications, health monitoring, environment variables, security |
| [Testing](testing.md) | All test files and coverage, CI/CD, parameter optimization (Optuna, DSPy, walk-forward), deployment pipeline checks |

---

## Navigation for AI Agents

This project is designed for AI-agent control. The key entry points for agent interaction are:

1. **`/bybit_ws/rpc.py`** — HTTP JSON-RPC server on port 8766. ~32 endpoints for reading positions, entering/closing trades, reloading config, kill switch. Bearer token auth.
2. **`/bybit_ws/mcp_server.py`** — MCP (Model Context Protocol) server for direct tool integration.
3. **`/bybit_ws/api.py`** — Bybit REST v5 wrapper; all exchange communication goes through this module.

**When making code changes**, always:
- Run `python test_logic_integrity.py` — AST-based verification that all `apply_*` functions are called
- Run `python test_smoke.py` — 16 integration tests
- Run `python test_regression.py` — 4-layer regression shield (compile → import → logic → deploy)
- Check `git log` for recent bugfix patterns — the project has frequent edge-case fixes

---

## Feature Flags

Controlled via environment variables loaded in `/bybit_ws/feature_flags.py`:

| Flag | Env Variable | Default | Production | Controls |
|------|-------------|---------|------------|----------|
| `ml_enabled` | `BYBIT_ML_ENABLED` | 1 | ✅ 1 | RF ML Gate — Random Forest signal filter |
| `dspy_enabled` | `BYBIT_DSPY_ENABLED` | 0 | ❌ 0 | DSPy Gate — prompt optimization |
| `optuna_enabled` | `BYBIT_OPTUNA_ENABLED` | 0 | ❌ 0 | Optuna hyperparameter tuning |
| `ws_full_enabled` | `BYBIT_WS_FULL_ENABLED` | 0 | ❌ 0 | Real-time positions via WebSocket |
| `ws_bb_enabled` | `BYBIT_WS_BB_ENABLED` | 1 | ✅ 1 | WebSocket for kline/BB cache |
| `ab_enabled` | `BYBIT_AB_ENABLED` | 0 | ❌ 0 | A/B testing for ML strategies |
| `regime_auto` | `BYBIT_REGIME_AUTO` | 0 | ❌ 0 | LSTM → auto LONG/SHORT regime |
| `push_enabled` | `PUSH_ENABLED` | 1 | ✅ 1 | Push notifications (ntfy + Telegram) |
| `production` | `BYBIT_WS_PRODUCTION` | 0 | ❌ 0 | Production guard mode |
| `exchange` | `BYBIT_EXCHANGE` | bybit | ✅ | Exchange selection |
| `data_dir` | `BYBIT_DATA_DIR` | `~/.local/share/bybit-ws` | ✅ | Data directory override |

---

## Data Directory

Runtime data lives at `~/.local/share/bybit-ws/` (~23 MB):

| File | Purpose |
|------|---------|
| `state.db` (+ `-wal` + `-shm`) | **Primary SQLite DB** — WAL mode, 5s busy timeout |
| `bb_cache.json` | Bollinger Bands cache |
| `positions.json` | Current positions snapshot |
| `orders.json` | Active orders |
| `trades.jsonl` | Trade history (append-only JSONL) |
| `events.log` | Event log (~15 MB) |
| `alerts.log` | All alerts |
| `health.txt` | Timestamp health check |
| `canary_state.json` | Self-learning canary state |
| `ml/` | ML models (RandomForest, scaler) |
| `backtests/` | Backtest results |
| `backups/` | Age-encrypted backups |

---

## Key Technical Decisions

- **WAL-mode SQLite** as SSOT — no dual-write, no JSON-file conflicts
- **Async main loop** with 30s cycles, heavy cycles every 5min, daily cycles
- **Fail-closed** for risk (Entry Judge, Risk Manager) — block if uncertain
- **Fail-open** for filters (MTF, Orderbook, Volume) — skip the filter, -10% score
- **ATR-adaptive** SL/TP — 4 volatility regimes, capped ±50%
- **Self-learning** with canary mode — 10% of entries test new params, 48h evaluation, auto-rollback
- **Multi-tier Black Swan** — 3 escalation levels based on BTC drop magnitude

---

## Related Documentation

- [Existing docs](/docs/) — ARCHITECTURE.md, CAPABILITIES.md, API.md, SECURITY.md, TROUBLESHOOTING.md, WEBHOOKS.md, ROADMAP.md
- [AGENTS.md](/AGENTS.md) — Detailed agent navigation with complete module tree
- [DESIGN.md](/DESIGN.md) — Original design document (v3.10, partially stale)
- [DESIGN-STRATEGIES.md](/DESIGN-STRATEGIES.md) — Strategy architecture (v3.12, partially stale)
- [CHANGELOG.md](/CHANGELOG.md) — Full version history

1. **Core Trading Engine**
   - Main execution loop (`main_async.py`, `main.py`)
   - Risk management (`risk_manager.py`)
   - Position management (`auto_entry.py`, `auto_sl.py`, `auto_tp.py`)

2. **Trading Strategies**
   - Various strategy implementations (`bb_scalp.py`, `funding_entry.py`, `mean_revert.py`, etc.)
   - Machine learning components (`ml_scorer.py`, `rl_agent.py`)

3. **Exchange Integration**
   - Bybit WebSocket and REST API integration (`bybit_ws_sdk.py`, `api.py`)
   - Adapter layer (`exchange_adapter.py`)

4. **Monitoring & Reporting**
   - Dashboard (`dashboard.py`)
   - Alerts (`alerts.py`, `push_notifier.py`)
   - Reporting (`reporting.py`)

5. **Testing & Optimization**
   - Test suite (`test_*.py`)
   - Parameter optimization (`optuna_tuner.py`)

## Getting Started

1. **Configuration**
   - Copy `.env.example` to `.env` and set your credentials
   - Review `config.example.yaml` for system configuration

2. **Running**
   - Main entry point: `python main_async.py`
   - For development: `python -m bybit_ws`

3. **Monitoring**
   - Access dashboard: `python dashboard.py`
   - Check system health: `python health.py`

## Documentation Sections

- [Architecture Overview](architecture/overview.md)
- [Strategy Development](strategies/development.md)
- [Risk Management](risk/management.md)
- [Exchange Integration](integration/bybit.md)
- [Testing Framework](testing/approach.md)

## Important Notes

- This system requires Bybit API credentials
- Risk parameters should be carefully configured before live trading
- The system maintains state in `state.db`