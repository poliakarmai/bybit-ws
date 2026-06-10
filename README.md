# bybit-ws — AI-Native Trading Engine

**Bollinger Grid × 8 strategies. REST API + MCP. Built for AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/version-3.9-blue)](./CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Bybit](https://img.shields.io/badge/exchange-Bybit-yellow)](https://bybit.com)

---

## 🤖 What is it?

A production-grade 24/7 trading monitor for Bybit. Scans the market, scores opportunities, enters positions, manages risk — all through an HTTP API and MCP server designed for **AI agents** (Claude Code, Codex, Cursor, Hermes, GitHub Copilot).

> **Not a bot. An engine.** Your AI agent calls `/scan`, picks signals, calls `/enter`. bybit-ws handles execution, SL/TP, risk limits, and reporting.

## ⚡ Quick Start

```bash
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Add BYBIT_API_KEY + BYBIT_API_SECRET
python3 -m bybit_ws.main
```

```python
from bybit_ws_sdk import Monitor
m = Monitor("http://localhost:8766")

signals = m.scan(mode="long", limit=5)
for s in signals["signals"]:
    if s["score"] >= 7.0:
        m.enter(s["symbol"], "Buy", s["qty"])
```

**[Full Quickstart →](./QUICKSTART.md)** — 5 minutes from clone to first trade.

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

## 🏗 Architecture

```
AI Agent (Claude Code / Codex / Cursor / Hermes)
    │
    ├── REST API (port 8766): scan, enter, close, health, positions, orders
    ├── MCP Server: scan_market, get_positions, get_metrics, health_check
    └── Python SDK: Monitor + WebhookHandler
    │
bybit-ws engine (30s cycle, systemd or Docker)
    ├── 8 strategies with multi-metric scoring
    ├── Dynamic position sizing (% of deposit)
    ├── Auto SL/TP via trading-stop
    ├── X10 safety pack: ATR validation, daily loss limits, cooldowns
    ├── Correlation matrix, funding tracker, regime classifier
    ├── SVG dashboard (winrate, margin, funding, correlation)
    └── Telegram bot (@GridSignalBot) for live signals
```

**[Full architecture →](./DESIGN.md)**

## 🛡 Risk Management

| Feature | Detail |
|---------|--------|
| Position sizing | Dynamic: deposit × risk% / max_positions × score multiplier |
| Drawdown guard | Auto-close all at −15% from daily peak |
| Daily loss stop | Halt after $50/day in losses |
| Correlation block | Reject if ≥2 positions correlated > 0.8 |
| X10 limits | Max 3 losing x10 trades → 24h cooldown |
| Cascade protection | Market-close if price 2× closer to liquidation than SL |
| Banned symbols | Config-driven permanent ban list |

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

## 📋 Requirements

- Python 3.11+
- Bybit Unified Trading Account + API keys (read/write + trading)
- Linux VPS ($5/mo works) or Docker

## 🔮 Live Demo

Try the Telegram bot for live Bollinger Grid signals:
- **@GridSignalBot** — `/scan` for top-5 LONG signals, `/scan short` for SHORT, x10 modes
- Free tier: 10 scans/day

## 📄 License

MIT — see [LICENSE](./LICENSE).

---

*Built for AI agents. Ready for yours. Questions? Open an issue or DM [@Poliakarm](https://t.me/Poliakarm).*
