# bybit-ws — AI-Native Trading Engine

**Bollinger Grid × 8 strategies. REST API + MCP. Built for AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE) [![Version](https://img.shields.io/badge/version-3.9-blue)](./CHANGELOG.md) [![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org) [![Bybit](https://img.shields.io/badge/trade-Bybit_$30_bonus-orange)](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen)

---

## 📸 What it looks like

> *[Screenshot: SVG dashboard + Telegram alerts — coming soon]*

```text
┌─────────────────────────────────────────────────────────┐
│  📊 BYBIT-WS DASHBOARD           Uptime: 27d 14h        │
│  ─────────────────────────────────────────────────────  │
│  Margin: $57.12/$100.00          Winrate: 68%           │
│  Open: 6 pos  │  PnL: -$12.67    Alerts: 847           │
│  ─────────────────────────────────────────────────────  │
│  POSITIONS:                                              │
│  MOVE  +$3.21  BB 18%  ████░░░░░░░░░░░░░░              │
│  DOGE  +$5.24  BB 23%  █████░░░░░░░░░░░░░              │
│  XRP   -$8.12  BB 35%  ████████░░░░░░░░░░              │
│  ...                                                     │
└─────────────────────────────────────────────────────────┘
```

---

## 👥 Who is this for?

- **AI developers** building autonomous trading agents (Claude Code, Codex, Cursor, Hermes)
- **Quant traders** who want a configurable Bollinger engine with HTTP API
- **Crypto enthusiasts** who want 24/7 automated monitoring with Telegram alerts

---

## ❓ Why bybit-ws?

| | bybit-ws | freqtrade | hummingbot |
|---|:---:|:---:|:---:|
| AI-first (MCP + REST for LLMs) | ✅ | ❌ | ❌ |
| Single YAML config, no DB | ✅ | ❌ | ❌ |
| 8 strategies out of the box | ✅ | ❌ | ❌ |
| Runs on $5 VPS | ✅ | ✅ | ❌ |
| Python SDK for agents | ✅ | ❌ | ❌ |

- **AI-first**: MCP server + REST API designed for LLM agents, not just humans
- **Zero infra**: one YAML config, no database, no Docker required (but supported)
- **Battle-tested**: runs 24/7 on real money since 2025
- **Ready engine**: 8 strategies with multi-metric scoring — not a framework you need to code

---

## ⚡ Quick Start

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -r requirements.txt
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Add BYBIT_API_KEY + BYBIT_API_SECRET to config.yaml
python3 -m bybit_ws.main
```

```python
import sys; sys.path.insert(0, '.')
from bybit_ws_sdk import Monitor

m = Monitor("http://localhost:8766")

signals = m.scan(mode="long", limit=5)
for s in signals["signals"]:
    if s["score"] >= 7.0:
        m.enter(s["symbol"], "Buy", s["qty"])
```

**[Full Quickstart →](./QUICKSTART.md)** — 5 minutes from clone to first trade.

---

## ⚙️ Configuration

Key parameters in `~/.config/bybit-ws/config.yaml`:

```yaml
bybit:
  api_key: "your_key"
  api_secret: "your_secret"
  testnet: false

trading:
  max_positions: 12
  leverage: 3
  position_size_pct: 5        # % of deposit per position
  daily_loss_limit: 50        # halt trading at $ loss/day

strategies:
  bb_grid_long:
    enabled: true
    interval: "D"
    bb_threshold: 25           # enter when BB% < 25%
    score_min: 5.5
  # ... 7 more strategies
```

Full config with comments (Russian): [`config.example.yaml`](./config.example.yaml)

---

## 📊 Strategies

| # | Strategy | Lev | Timeframe | Entry Trigger | SL | TP | Max Pos |
|---|----------|-----|-----------|---------------|-----|-----|---------|
| 1 | **BB Grid LONG** | 3x | Daily | BB < 25%, score ≥ 5.5 | −7% | Middle/Upper | 12 |
| 2 | **BB Grid SHORT** | 3x | Daily | BB > 85%, Tier A/B | +5–7% | Middle | 3 |
| 3 | **Junk SHORT** | 3x | Daily | Pump ≥ 80%, BB > 70% | −15% | Middle | 2 |
| 4 | **SL Re-entry** | 3x | Daily | Ladder after SL | −7% | Middle | per coin |
| 5 | **BB Scalp M5** ⚡ | **10x** | M5 | Band touch + RSI filter | 3% | Middle | 3 |
| 6 | **Mean Revert** ⚡ | **10x** | Daily | BB% < 5% or > 95% | 5% | Middle | 5 |
| 7 | **Funding Momentum** ⚡ | **10x** | Daily | Funding ±0.1% + BB + trend | 4% | Middle | 3 |
| 8 | **DCA Ladder** | 3x | — | −5/−10/−15% from entry | shared | shared | 2 adds |

⚡ = x10 leverage strategies — high risk, high reward.

**[Full strategy docs →](./STRATEGIES.md)**

---

## 🏗 Architecture

```
┌─── Interfaces (how AI agents connect) ──────────────────┐
│                                                          │
│  AI Agent (Claude Code / Codex / Cursor / Hermes)       │
│      │                                                   │
│      ├── REST API (port 8766)                            │
│      ├── MCP Server                                      │
│      └── Python SDK (bybit_ws_sdk.Monitor)               │
│                                                          │
└──────────────────────────────────────────────────────────┘
                         │
┌─── Engine internals (30s cycle, systemd or Docker) ─────┐
│                                                          │
│  • 8 strategies with multi-metric scoring                │
│  • Dynamic position sizing (% of deposit)                │
│  • Auto SL/TP via trading-stop                           │
│  • X10 safety pack: ATR validation, daily loss limits    │
│  • Correlation matrix, funding tracker, regime classifier│
│  • SVG dashboard (winrate, margin, funding, correlation) │
│  • Telegram bot (@GridSignalBot) for live signals        │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**[Full architecture →](./DESIGN.md)**

---

## 🛡 Risk Management

| Feature | Detail |
|---------|--------|
| Position sizing | Dynamic: deposit × risk% / max_positions × score multiplier |
| Drawdown alert | Alert at configurable drawdown from daily peak |
| Daily loss stop | Configurable daily loss limit (halt after threshold) |
| Correlation block | Reject if ≥2 positions correlated > 0.8 |
| X10 limits | Max 3 losing x10 trades → 24h cooldown |
| Cascade protection | Market-close if price 2× closer to liquidation than SL |
| Banned symbols | Config-driven permanent ban list |

---

## 📈 Track Record

> *Live data from production instance*

- **Uptime**: 99.7% (30-day rolling)
- **Trades executed**: 847 (last 30 days)
- **Win rate (LONG)**: ~68%
- **Avg hold time**: 14h

---

## 📦 Files

| File | Purpose |
|------|---------|
| [`QUICKSTART.md`](./QUICKSTART.md) | 5-minute setup guide |
| [`DESIGN.md`](./DESIGN.md) | Full architecture (for devs & AI agents) |
| [`STRATEGIES.md`](./STRATEGIES.md) | All 8 strategies with parameters + roadmap |
| [`ERRORS.md`](./ERRORS.md) | Error reference: Bybit codes + bybit-ws errors |
| [`WEBHOOKS.md`](./WEBHOOKS.md) | Alert payloads & parsing examples |
| [`CHANGELOG.md`](./CHANGELOG.md) | Version history 3.0 → 3.9 |
| [`openapi.yaml`](./openapi.yaml) | OpenAPI 3.0 REST schema |
| [`bybit_ws_sdk.py`](./bybit_ws_sdk.py) | Python SDK class |
| [`config.example.yaml`](./config.example.yaml) | Full config with comments (Russian) |

---

## 🚀 Deploy

```bash
# Docker
docker compose up -d

# systemd (recommended)
sudo cp bybit-ws.service /etc/systemd/user/
systemctl --user enable --now bybit-ws

# Health check
curl http://localhost:8766/health
# → {"status":"alive","uptime":86400,"cycle_count":2880}
```

---

## 📋 Requirements

- Python 3.11+
- Bybit Unified Trading Account + API keys (read/write + trading)
- Linux VPS ($5/mo works) or Docker

---

## 🔮 Live Demo

Try the Telegram bot for live Bollinger Grid signals:
- **@GridSignalBot** — `/scan` for top-5 LONG signals, `/scan short` for SHORT, x10 modes
- Free tier: 10 scans/day

Follow live signals and market analysis: **[@criptapolyaka](https://t.me/criptapolyaka)**

---

## 🤝 Contributing

PRs welcome. See [DESIGN.md](./DESIGN.md) for architecture. Priority areas:
- WebSocket migration (replace REST polling)
- Backtesting module
- Multi-exchange support

---

## 📄 License

MIT — see [LICENSE](./LICENSE).

---

*Built for AI agents. Ready for yours. Questions? Open an issue or DM [@Poliakarm](https://t.me/Poliakarm).*

*Need a Bybit account? [Sign up with $30 bonus](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen) — supports the project.*
