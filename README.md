# bybit-ws — AI-Native Trading Engine

**Bollinger Grid + 8 strategies. REST API + MCP. Built for AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Version](https://img.shields.io/badge/version-3.8-blue)](./CHANGELOG.md)

---

## What is it?

A production-grade trading monitor for Bybit that runs 24/7, scans for opportunities, enters positions, manages risk, and logs everything — all accessible through an HTTP API designed for AI agents.

**Not a bot. An engine.** Your AI agent calls `/scan`, picks signals, calls `/enter`. The engine handles execution, SL/TP, risk limits, and reporting.

## Quick Start (for AI Agents)

```bash
git clone https://github.com/poliakarm/bybit-ws.git
cd bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Set BYBIT_API_KEY and BYBIT_API_SECRET
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

**[Full Quickstart →](./QUICKSTART.md)**

## Strategies

| # | Strategy | Leverage | Trigger |
|---|----------|----------|---------|
| 1 | **BB Grid LONG** | 3x | BB < 25% Daily, scoring ≥ 5.5 |
| 2 | **BB Grid SHORT** | 3x | BB > 85% Daily, Tier A/B |
| 3 | **Junk SHORT** | 3x | Daily pump ≥ 80%, no SL, hard stop |
| 4 | **SL Re-entry** | 3x | Ladder after stop-loss (-5%, -10%, -15%) |
| 5 | **BB Scalp M5** ⚡ | 10x | BB touch M5 + RSI filter |
| 6 | **Mean Revert** ⚡ | 10x | BB% < 5% or > 95% Daily |
| 7 | **Funding Momentum** ⚡ | 10x | Extreme funding + BB + trend |
| 8 | **DCA** | 3x | Add to losing positions at lower levels |

**[All strategies with parameters →](./STRATEGIES.md)**

## Architecture

```
AI Agent (Claude Code / Codex / Cursor / Hermes)
    │
    ├── REST API (port 8766): scan, enter, close, health
    ├── MCP Server: scan_market, get_positions, get_metrics
    └── Python SDK: Monitor + WebhookHandler classes
    │
bybit-ws (30s cycle, systemd or Docker)
    ├── 8 strategies with scoring + risk checks
    ├── Position sizing: dynamic % of deposit
    ├── Correlation check: block entries >0.8
    ├── X10 safety: daily loss stop, ATR validation
    ├── Auto SL/TP via trading-stop
    └── Dashboard: SVG with winrate, margin, regime
```

**[Full architecture →](./DESIGN.md)**

## Risk Management

- **Position sizing:** dynamic margin = deposit × risk% / positions × score
- **Drawdown guard:** auto-close all at -15% from peak
- **Daily loss stop:** halt at -$50/day
- **Correlation block:** reject entries if ≥2 positions correlated >0.8
- **X10 limits:** max 3 losing x10 trades → 24h cooldown
- **Cascade protection:** market-close if price 2x closer to liquidation than SL
- **Banned symbols:** config-driven permanent ban

## Files

| File | For |
|------|-----|
| [`QUICKSTART.md`](./QUICKSTART.md) | 5-minute setup |
| [`DESIGN.md`](./DESIGN.md) | Full architecture for devs & agents |
| [`STRATEGIES.md`](./STRATEGIES.md) | All 8 strategies + roadmap |
| [`ERRORS.md`](./ERRORS.md) | Error reference: what + fix |
| [`WEBHOOKS.md`](./WEBHOOKS.md) | Alert payloads & parsing |
| [`CHANGELOG.md`](./CHANGELOG.md) | Version history |
| [`openapi.yaml`](./openapi.yaml) | OpenAPI 3.0 spec |
| [`bybit_ws_sdk.py`](./bybit_ws_sdk.py) | Python SDK |
| [`config.example.yaml`](./config.example.yaml) | Full config with comments |

## Requirements

- Python 3.11+
- Bybit Unified Trading Account with API keys
- Linux server (VPS $5/mo works) or Docker

## License

MIT — see [LICENSE](./LICENSE).

---

*Built for AI agents. Ready for yours.*
