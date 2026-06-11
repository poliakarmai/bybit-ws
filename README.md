# bybit-ws — AI-Native Trading Engine

**Bollinger Grid × 7 strategies + DCA overlay. REST API + MCP. Built for AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE) [![Version](https://img.shields.io/badge/version-3.9-blue)](./CHANGELOG.md) [![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org) [![Bybit](https://img.shields.io/badge/trade-Bybit_$30_bonus-orange)](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen)

---

## 👥 Who is this for?

- **AI developers** building autonomous trading agents (Claude Code, Codex, Cursor, Hermes)
- **Quant traders** who want a configurable Bollinger engine with HTTP API
- **Crypto enthusiasts** who want 24/7 automated monitoring with Telegram alerts

---

## ❓ Why bybit-ws?

| | bybit-ws | freqtrade | hummingbot |
|---|:---:|:---:|:---:|
| MCP server for AI agents | ✅ | ❌ | ❌ |
| Bollinger-specific strategies (7 variants) | ✅ | ❌ | ❌ |
| No database required | ✅ | ❌ (SQLite) | ❌ |
| Single-file config | ✅ | ✅ | ⚠️ |

- **AI-first**: MCP server + REST API designed for LLM agents, not just humans
- **Ready to trade**: 7 Bollinger-based strategies + DCA overlay — clone and run, no strategy coding required
- **Zero infra**: one YAML config, no database, no Docker required (but supported)
- **Private development since 2025, open-sourced June 2026**

---

## ⚠️ Before you start

> **This software trades real money on leveraged futures markets (up to 10x). You can lose your entire deposit. Start with testnet.**

```bash
# Testnet first!
# In ~/.config/bybit-ws/config.yaml:
api:
  base_url: "https://api-testnet.bybit.com"
```

---

## ⚡ Quick Start

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -e .                    # install bybit-ws + dependencies
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Add your API keys to config.yaml (api.key + api.secret)
# ⚠️ First run: use testnet! (api.base_url: "https://api-testnet.bybit.com")
bybit-ws daemon
```

```python
from bybit_ws_sdk import Monitor

m = Monitor("http://localhost:8766", token="your-rpc-token")

signals = m.scan(mode="long", limit=5)
for s in signals["signals"]:
    if s["score"] >= 7.0:
        # Step 1: preview without executing (dry-run)
        preview = m.enter(s["symbol"], "Buy", s["qty"], confirm=False)
        print(f"Margin: ${preview['margin']}, Liq: ${preview['liq_price']}")

        # Step 2: execute with SL and TP
        m.enter(s["symbol"], "Buy", s["qty"],
                sl=preview["sl_suggested"],
                tp=preview["tp_suggested"],
                confirm=True)
```

```text
# Terminal dashboard (bybit-ws dashboard command)
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

**[Full Quickstart →](./QUICKSTART.md)** — 5 minutes from clone to first trade.

---

## ⚙️ Configuration

Key parameters in `~/.config/bybit-ws/config.yaml` (simplified — see [`config.example.yaml`](./config.example.yaml) for all options):

```yaml
api:
  key: "${BYBIT_API_KEY}"
  secret: "${BYBIT_API_SECRET}"
  base_url: "https://api-testnet.bybit.com"  # testnet first!

strategy:
  long:
    leverage: 3
    max_positions: 15            # max simultaneous LONG positions
    sl_offset: 0.07              # -7% from Lower BB
  short:
    leverage: 3
    max_positions: 3             # max simultaneous SHORT positions
    bb_threshold: 85             # short when BB% > 85%

risk:
  max_drawdown_pct: 15           # global stop: -15% from peak
  max_daily_loss: 50             # halt trading at -$50/day
  emergency_close_all: true

rpc:
  port: 8766
  auth_token: "${RPC_TOKEN}"     # Bearer token — REQUIRED for write endpoints
```

Full config with comments: [`config.example.yaml`](./config.example.yaml)

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

⚡ = x10 leverage — **liquidation at ~10% adverse move. High risk.**  
**DCA overlay** applies to strategies #1–#7: adds to losing positions at −5/−10/−15% from entry (up to 2 adds).

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
│  • 7 strategies + DCA overlay with multi-metric scoring  │
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
| Drawdown guard | Alert + optional emergency close at configurable drawdown from peak |
| Daily loss stop | Configurable daily loss limit (halt after threshold) |
| Correlation block | Reject if ≥2 positions correlated > 0.8 |
| X10 limits | Max 3 losing x10 trades → 24h cooldown |
| Cascade protection | Market-close if price 2× closer to liquidation than SL |
| Banned symbols | Config-driven permanent ban list |

---

## 🔒 Security

- **API keys**: stored in environment variables (`${BYBIT_API_KEY}`), never hardcoded
- **RPC auth**: Bearer token required for all write endpoints (`/enter`, `/close`)
- **Bind**: `127.0.0.1` by default (localhost only) — never use `0.0.0.0` without `auth_token`
- **Bybit side**: enable IP whitelist for API keys
- **Config**: `chmod 600 ~/.config/bybit-ws/config.yaml`

---

## 📈 Track Record

> *Self-reported from production instance. Unaudited. Past performance ≠ future results.*

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
| [`STRATEGIES.md`](./STRATEGIES.md) | All 7 strategies + DCA with parameters + roadmap |
| [`ERRORS.md`](./ERRORS.md) | Error reference: Bybit codes + bybit-ws errors |
| [`WEBHOOKS.md`](./WEBHOOKS.md) | Alert payloads & parsing examples |
| [`CHANGELOG.md`](./CHANGELOG.md) | Version history 3.0 → 3.9 |
| [`openapi.yaml`](./openapi.yaml) | OpenAPI 3.0 REST schema |
| [`bybit_ws_sdk.py`](./bybit_ws_sdk.py) | Python SDK class |
| [`config.example.yaml`](./config.example.yaml) | Full config with comments |

---

## 🚀 Deploy

```bash
# Docker
docker compose up -d

# systemd — user-level (recommended, no root needed)
mkdir -p ~/.config/systemd/user
cp bybit-ws.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bybit-ws

# Health check
curl http://localhost:8766/health
# → {"status":"alive","uptime":86400,"cycle_count":2880}
```

---

## 📋 Requirements

- Python 3.11+
- Dependencies: `requests`, `pyyaml`, `websocket-client`, `numpy` (auto-installed via `pip install -e .`)
- Bybit Unified Trading Account + API keys (read/write + trading)
- Linux VPS ($5/mo works) or Docker

---

## 🔮 Live Demo

**@GridSignalBot** — public demo bot running the author's instance. Free tier: 10 scans/day.
- `/scan` — top-5 LONG signals
- `/scan short` — top-5 SHORT signals

To set up your own Telegram alerts, see [`WEBHOOKS.md`](./WEBHOOKS.md).

Follow live signals and market analysis: **[@criptapolyaka](https://t.me/criptapolyaka)**

---

## 🤝 Contributing

PRs welcome. See [DESIGN.md](./DESIGN.md) for architecture. Priority areas:
- WebSocket migration (replace REST polling)
- Backtesting module
- Multi-exchange support

---

## ⚠️ Disclaimer

**This software trades real money on leveraged futures markets. You can lose your entire deposit.** The authors are not responsible for any financial losses. Past performance does not guarantee future results. Use at your own risk. Start with testnet.

---

## 📄 License

MIT — see [LICENSE](./LICENSE).

---

*Built for AI agents. Ready for yours. Questions? Open an issue or DM [@Poliakarm](https://t.me/Poliakarm).*

*Need a Bybit account? [Sign up with $30 bonus](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen) — supports the project.*
