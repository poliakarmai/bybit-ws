# bybit-ws — AI-Native Trading Engine

**English** · [Русский](README.md)

**24/7 automated futures trading on Bybit. Bollinger Grid + AI filters. Plug it in — it earns.**

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/pytest-passing-brightgreen)](./AGENTS.md) [![Smoke](https://img.shields.io/badge/smoke-52%2F52-brightgreen)](./test_smoke.py)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](./LICENSE)
[![Phase 11](https://img.shields.io/badge/phase-11-blue)](./CHANGELOG.md)

---

> **The design write-up is in English:** [bybit-ws: a trading tool, an analyst, and an assistant](https://dev.to/alexey_polyakov_cfe2095e3/bybit-ws-a-trading-tool-an-analyst-and-an-assistant-12al) — what holds up and what doesn't. Documentation below is translated progressively; Russian originals sit next to each English file. If you think part of this design is wrong, say it in [issues](../../issues): the pinned issue lists five things I'm not sure about, and one line — "you'll regret this, because X" — is worth more than a star.

## What it is

An autonomous trading engine that:
- Finds entries via Bollinger Bands + 9-metric AI scoring
- Opens LONG/SHORT automatically
- Manages risk: stop-losses, take-profits, trailing, DCA
- Protects the deposit: Circuit Breaker, BlackSwan, Anti-ludomania, correlation block
- Self-learns: LSTM market classifier, Thompson Sampling, Dynamic Bandit, Drift Detector, Ensemble (20+ mechanics)

Runs **24/7 on a $5/mo VPS**. pytest suite + 52 smoke checks, atomic deploy.

---

## Results

We don't keep stats here: they change several times a day, and a static snapshot in a README is lying within a day. Live numbers come from the bot itself:

```bash
curl -s localhost:8766/metrics          # trades, entries, SL/TP for the day
curl -s localhost:8766/positions        # open positions + PnL (Bearer)
python3 -m bybit_ws.reconcile --days 30 # reconcile state.db against the exchange
python3 -m bybit_ws.paper_trade SOLUSDT --days 90   # backtest with your own parameters
```

> **Not a guarantee of returns.** Numbers from a specific installation (`strategy='auto'`, `manual=0`)
> are not a promise. `trade_history.side` is the **closing** side (`Buy` = closed a SHORT); `/positions`
> is the opposite: raw Bybit.

---

## Quick start (3 steps)

### 1. Clone and configure

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -r requirements.txt
```

### 2. Add Bybit keys

```bash
mkdir -p ~/.config/bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml

# Create env with API keys (chmod 600!)
cat > ~/.config/bybit-ws/env << 'EOF'
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
RPC_TOKEN=your_random_token_32_chars
TG_BOT_TOKEN=optional_telegram_bot_token
TG_CHAT_ID=optional_chat_id
EOF
chmod 600 ~/.config/bybit-ws/env
```

### 3. Run

```bash
# Test run (no real trades — make sure everything works)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30

# Production run
python3 test_smoke.py            # 52 checks → PASS
python3 -m pytest -q             # full suite (should be green)
python3 -m pytest tests/ -q      # tests/ only
bash deploy.sh                 # atomic deploy with canary check
```

Done. The engine is running. Positions open automatically on signals.

---

## Features

### 🤖 Auto-trading
- **Bollinger Grid** LONG/SHORT on the daily timeframe
- **9-metric scoring**: BB%, volume, falling days, funding, volatility, quality + ML Gate
- **LSTM market classifier** (5 classes; 82.3% — claimed 01.08, not re-measured)
- **Auto-entries** with MTF confirmation, orderbook analysis and Entry Judge (DeepSeek, fail-closed, fail-open only on prolonged LLM failure)
- **Auto-TP**: ATR-adaptive, 3 legs `[0.40, 0.35, 0.25]` (% of size), ATR multipliers per regime. BB 20% Middle / 80% Upper — fallback when ATR is unavailable
- **Unified SL**: 5 mechanisms → one takes priority (tight > simple > hard > BE > default)

### 🛡️ Deposit protection
- **Circuit Breaker** at 80% of the daily limit
- **BlackSwan v2**: 5+ red positions + uPnL < -$150 → alert (no auto-close)
- **Anti-ludomania**: 3 losses in an hour → auto-entries blocked for 30 minutes
- **Correlation block**: entry denied at >80% correlation with an open position
- **Pump Detection**: pump detection +24h/+230% wk → blocks entry
- **Kill Switch**: instant close of all positions via RPC

### 📊 Backtesting
- **Paper Trading** on historical data (`paper_trade.py`)
- 6-metric scoring, LONG/SHORT, SL/TP with RR
- Metrics: Sharpe, max drawdown, profit factor, win rate

### 🔌 Interfaces
- **RPC API** (port 8766): `/health`, `/scan`, `/enter`, `/close`, `/positions`, `/metrics`, `/risk`, `/balance` — full path list in `do_GET`/`do_POST` (`rpc.py`)
- **HTTP RPC** for bots and integrations (`http.server`, not JSON-RPC)
- **MCP Server** for AI agents (Claude Code, Hermes, Codex)
- **Telegram alerts**: entries, SL, TP, DCA, pumps, errors
- **One-click trading** via Hermes chat: "scan SOL" → "take 5%"

### 🧠 AI & Self-Learning (v10 — 20+ mechanics)
- **Thompson Sampling**: Dynamic Bandit — auto-pruning + arm generation, uncertainty-aware selection
- **Ensemble Learning**: separate bandit per regime — 6 (5 LSTM classes + `CHOPPY`)
- **Drift Detector**: per-regime windows with EMA baseline, causal inference (market vs parameters)
- **Stress Testing**: 4 historical backtest scenarios + 1000 Pareto Monte Carlo simulations
- **Composite Score**: WR+PF+Sharpe+MaxDD+AvgHold with adaptive per-regime weights and exponential decay
- **Micro-updates**: learn after every trade with outlier protection (>3σ)
- **Canary mode**: Bayesian A/B test, adaptive 5-20%, auto-rollback
- **Entry Judge**: one model (DeepSeek, fail-closed); Qwen removed 17.10, no Nemotron in the call path
- **LSTM Market Regime**: 5 classes, auto-adapts LONG/SHORT (82.3% — claimed, not re-measured)
- **LSTM World Model**: multi-task OHLCV prediction for entry scoring

---

## Who it's for

| You | How to use it |
|----|-----------------|
| 🧑‍💻 **Trader** | Deploy to a VPS, connect Telegram alerts — the engine runs 24/7 |
| 🤖 **Quant/developer** | Fork it, plug in your model, use the RPC API |
| 💼 **Signal provider** | Connect @Gridbolbot to the RPC — clients get signals |
| 🏢 **Fund/team** | Multi-instance, shared RPC, shared monitoring |

---

## Architecture

```
┌───────────── MAIN LOOP (async, 30s) ─────────────┐
│  Every cycle:                                    │
│  • unified_sl.py   — Unified SL (5 mechanisms)   │
│  • time_exit.py    — Time exit (every cycle, max 24h)│
│  • auto_tp.py      — ATR-based TP                │
│  • risk_manager.py — Circuit Breaker + BlackSwan │
│                                                  │
│  Every 10 cycles (HEAVY, ~5 min):                │
│  • auto_entry.py   — LONG with AI scoring        │
│  • auto_short.py   — SHORT + JUNK tier           │
│  • pump_detect.py  — pump detection              │
│  • correlation.py  — correlation matrix          │
│  • dca.py          — DCA buys                    │
│                                                  │
│  Every 24 hours (self-learn):                    │
│  • self_learn.py   — Dynamic Bandit, Drift, Ensemble│
│  • canary mode     — Bayesian A/B test of params │
└──────────────────────────────────────────────────┘
```

[Full architecture →](docs/ARCHITECTURE.md)

---

## Ecosystem projects

| Project | Purpose |
|--------|-----------|
| **bybit-ws** | Trading engine (this repo) |
| [@Gridbolbot](https://t.me/Gridbolbot) | Telegram signals bot (Pro subscription: unlimited + alerts) |
| [hermes-trader](https://github.com/poliakarmai/hermes-trader) | Built-in trading in Hermes Agent |
| [Paper Trading](bybit_ws/paper_trade.py) | Backtesting on history |
| [docs/INDEX.md](docs/INDEX.md) | Documentation map: what's current, what's archived |

---

## Documentation

| File | Contents |
|------|-----------|
| [ONBOARDING.md](ONBOARDING.md) | **Step-by-step guide:** from zero to first trade |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture, data flows, Mermaid diagrams |
| [docs/API.md](docs/API.md) | RPC API: actual endpoint list from `rpc.py`, curl examples |
| [docs/INDEX.md](docs/INDEX.md) | Documentation map + status of each file |
| [docs/SECURITY.md](docs/SECURITY.md) | Security: secrets, threat model, audit |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Troubleshooting: 60+ causes |
| [docs/PRD-one-click.md](docs/PRD-one-click.md) | One-click trading via chat |
| [AGENTS.md](AGENTS.md) | Navigation for AI agents |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

---

## Requirements

- **Python 3.11+**
- Bybit Unified Trading Account + API keys (read/write + trading)
- Linux server (VPS $5/mo) or Docker
- Minimum deposit: $50 on testnet, $200 on mainnet

---

## Security

- API keys via `env` (chmod 600) — never in code
- RPC write endpoints require a Bearer token
- IP whitelist at the Bybit level
- Atomic deploy: 3 pre-deploy steps (repo cleanliness → logic → smoke) + 8 canary checks
- Kill Switch: instant close of all positions

[Details →](docs/SECURITY.md)

---

## Disclaimer

**This software trades real money on futures with leverage up to 10x. You can lose the entire deposit.** The authors are not responsible for financial losses. Start on testnet. Backtest via `paper_trade.py`. Past results do not guarantee future ones.

---

## License

AGPL-3.0

---

*Built for traders. Hardened by AI. Questions: [@Poliakarm](https://t.me/Poliakarm).*
