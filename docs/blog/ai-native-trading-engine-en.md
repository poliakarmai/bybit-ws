# I Built an AI-Native Trading Engine in Python. Here's How It Works

> 8 strategies, Bollinger Grid, AI scoring, and full agent-based orchestration via Hermes. Open source, MIT.

---

## Why I Built This

Trading bots are either black-box subscriptions at $50/month, or DIY scripts that break at 3 AM. I wanted a system that:

- **Runs unattended** — fire and forget
- **Understands context** — not just "RSI < 30 = buy", but scoring across 9 metrics
- **Responds to voice/text** — "go AVAX x10" and the AI agent executes everything
- **Is open source** — so anyone can fork and customize

That's how **bybit-ws** was born — an AI-native trading engine for Bybit.

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────┐
│                  Hermes Agent                     │
│  (AI orchestrator: voice, text, auto-entry)      │
└──────────────┬──────────────────────────────────┘
               │ MCP / REST
┌──────────────▼──────────────────────────────────┐
│              bybit-ws (systemd)                   │
│  main.py → 30s cycle                             │
│  ├── auto_entry.py   (LONG via scoring)           │
│  ├── auto_short.py   (SHORT Tier A/B + junk)      │
│  ├── auto_sl.py      (BB-based stop-loss)         │
│  ├── auto_tp.py      (auto take-profit)           │
│  ├── bb_scalp.py     (⚡ x10 M5 scalping)         │
│  ├── mean_revert.py  (⚡ x10 extreme reversals)    │
│  ├── funding_entry.py(⚡ x10 funding momentum)     │
│  ├── dca.py          (ladder entries)              │
│  ├── rpc.py          (HTTP-RPC on port 8766)      │
│  └── dashboard.py    (SVG dashboard)              │
└──────────────┬──────────────────────────────────┘
               │ WebSocket
┌──────────────▼──────────────────────────────────┐
│               Bybit API v5                        │
└─────────────────────────────────────────────────┘
```

Core principle: **one module = one file**. Not a 5,000-line monolith, but 30+ files with clear responsibilities.

---

## 9 Strategies — From Conservative to Extreme

| Strategy | Leverage | Timeframe | Signal |
|----------|----------|-----------|--------|
| Bollinger Grid LONG | 3x | Daily | Lower BB −3% |
| Bollinger Grid SHORT | 3x | Daily | BB >85% |
| Junk Short | 3x | Daily | Pump ≥80% |
| SL Re-entry | 3x | Daily | Lower BB after stop |
| DCA Ladder | 3x | — | −5/−10/−15% from entry |
| ⚡ BB Scalping | 10x | M5 | Band touch + RSI filter |
| ⚡ Mean Reversion | 10x | Daily | BB% <5% or >95% |
| ⚡ Funding Momentum | 10x | Daily | Extreme funding rate |
| ⚡ ATR Risk Sizing | layer | 15m | Validation: SL = 1.5×ATR |

**The golden rule:** the system doesn't guess. Every entry passes through **scoring across 8+ metrics**:

- Tier bonus (S/A/B/C/D token)
- BB% position (0–100%)
- 24h volume
- RSI(14)
- Funding rate
- BB Squeeze (band compression)
- Consecutive down days
- Weekly/monthly BB%

Minimum threshold: **5.5/10**. Anything below — the system silently skips.

---

## AI Integration: More Than Buttons

The interesting part is the AI layer. I use **Hermes Agent** (by Nous Research) as the orchestrator:

- **MCP Server**: the agent sees positions, orders, and metrics through a standardized protocol
- **REST API**: port 8766, CORS, rate-limiting, RPC token
- **Auto-entry**: the agent decides on entries based on scoring
- **GridSignal Bot** (@GridSignalBot on Telegram): real-time scanner, LONG/SHORT/x10 buttons

Example interaction:

```
Me: "go AVAX x10 at market"
Agent: checks BB%, RSI, funding, correlations
       → scoring 7.2/10 → OK
       → calculates: margin=$5, qty=15.1, SL=−5%
       → order placed: 53e99c39
```

All via voice or text. While driving, on shift, in the shower — doesn't matter.

---

## Numbers & Hardware

- **Python 3.11**, systemd service
- **WebSocket** — real-time data from Bybit
- **30-second light cycle** — positions, orders, health
- **7-minute heavy cycle** — scoring, BB, RSI, squeeze, auto-entry
- **Memory**: ~200 MB at runtime
- **Uptime**: weeks without restart

---

## Roadmap

- 🔜 **Telegram Mini App** — dashboard right in Telegram
- 🔜 **Paper trading mode** — risk-free testing
- 🔜 **Docker one-liner** — `docker run` and done
- 🔮 **OKX/Binance support** — multi-exchange

---

## Try It

```bash
git clone https://github.com/poliakarmai/bybit-ws
cd bybit-ws
cp config.example.yaml config.yaml
# insert your Bybit API keys
systemctl --user enable --now bybit-ws
```

**GitHub:** [poliakarmai/bybit-ws](https://github.com/poliakarmai/bybit-ws)
**Bot:** [@GridSignalBot](https://t.me/GridSignalBot)
**License:** MIT

💰 **Trade on Bybit with $30 bonus:** [bybit.com/invite?ref=DQ0EAQ](https://www.bybit.com/invite?ref=DQ0EAQ&medium=referral&utm_campaign=evergreen)

---

*Questions? Open an issue, message the Telegram bot, or fork and customize. The code is alive, trades real money, and improves every day.*
