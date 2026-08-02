# I Built an AI-Native Trading Engine in Python. 5 Months Later, Here's What Changed

> 9 strategies → 12. ML scoring, backtesting, partial take-profit, Telegram bot that survived a 3-echelon audit. Open source, AGPL-3.0, trading real money.

---

## Why I Built This (And Kept Building)

Trading bots come in two flavors: black-box SaaS at $50/month, or GitHub scripts that crash at 3 AM. I wanted neither.

Five months ago I shipped v1 of **bybit-ws** — an AI-native trading engine for Bybit futures. The core loop was simple: scan Bollinger Bands daily, score every signal across 8 metrics, enter when the score passes 5.5/10. It worked. It made money. And then I kept building.

This is the v2 story: machine learning on real trade data, a backtesting engine that runs against historical klines, partial take-profit logic, and a Telegram bot that survived 14 production bugs found by three AI agents in parallel.

---

## Architecture: What 5 Months of Iteration Looks Like

v1 was clean. v2 is battle-tested.

```
                    ┌──────────────────────────┐
                    │      Hermes Agent          │
                    │  Voice/chat orchestrator   │
                    └───────────┬────────────────┘
                                │ MCP / REST (port 8766)
                    ┌───────────▼────────────────┐
                    │     bybit-ws (systemd)      │
                    │                             │
                    │  main.py — 30s light cycle  │
                    │         — 120s heavy cycle  │
                    │         — 480s x10 cycle    │
                    │                             │
                    │  ┌─ auto_sl.py              │
                    │  ├─ trailing_sl.py           │
                    │  ├─ trailing_sl_x10.py   ★   │
                    │  ├─ partial_tp.py        ★   │
                    │  ├─ funding_rotation.py  ★   │
                    │  ├─ ml_scorer.py         ★   │
                    │  ├─ backtest.py          ★   │
                    │  ├─ gridsignal_scanner.py    │
                    │  ├─ pump_detect.py           │
                    │  ├─ rpc.py (JSON-RPC)        │
                    │  └─ state_db.py (SQLite)     │
                    └───────────┬────────────────┘
                                │ WebSocket
                    ┌───────────▼────────────────┐
                    │       Bybit API v5          │
                    └────────────────────────────┘
```

★ = new in v2

Three cycles instead of two. The new **x10 cycle** (every 8 minutes) handles trailing stops for high-leverage positions without touching the 3x positions. The heavy cycle (every 2 minutes, down from 7) now includes partial take-profit and funding rotation checks.

**State is SQLite, period.** v1 had a mix of in-memory dicts and JSON snapshots. v2 uses SQLite with WAL mode as the single source of truth. 8 tables, atomic UPDATEs with WHERE clauses, zero race conditions. JSON snapshots are backup-only.

---

## 12 Strategies (Up from 9)

| Strategy | Lev | Timeframe | What's New |
|----------|-----|-----------|------------|
| Bollinger Grid LONG | 3x | Daily | — |
| Bollinger Grid SHORT | 3x | Daily | — |
| Junk Short | 3x | Daily | — |
| SL Re-entry | 3x | Daily | — |
| DCA Ladder | 3x | — | — |
| BB Scalping | 10x | M5 | — |
| Mean Reversion | 10x | Daily | — |
| Funding Momentum | 10x | Daily | — |
| ATR Risk Sizing | layer | 15m | — |
| **Partial TP** | ★ | dynamic | 20→50% scale-out, no numpy |
| **Trailing SL x10** | ★ | x10 only | Tight trailing on high-lev positions |
| **Funding Rotation** | ★ | auto | Closes positions before negative funding hits |

The golden rule still applies: every entry passes through **scoring across 8+ metrics** with a 5.5/10 threshold. But now there's a new layer on top.

---

## ML Scoring: When Heuristics Aren't Enough

After 5 months, the system had logged **282 real signals** with outcomes — entry price, exit price, PnL, whether it hit take-profit or stop-loss. That's a dataset.

I trained a **RandomForest classifier** on it:

```python
features = ['score', 'price_vs_lower', 'price_vs_upper',
            'volume_24h', 'funding_rate', 'rsi_14',
            'bb_squeeze', 'consecutive_down_days']
target   = 'is_profitable'  # 1 if PnL > 0, else 0
```

Results:
- **F1 score: 0.69** on a 262-signal test set
- **Top features:** score (0.31 weight), price_vs_lower (0.22), RSI (0.15)
- **Combined scoring:** 70% ML + 30% heuristic. If they disagree, ML wins.

The model runs every heavy cycle, re-scores open positions, and can veto new entries. It's not a black box — feature importance is logged, so I can see *why* it made a decision.

Is F1=0.69 "AI that prints money"? No. It's a filter that catches bad entries the heuristic would miss. In backtesting, the ML layer improved average PnL per trade by 18% just by rejecting the bottom quartile of signals.

---

## Backtesting: Walk-Forward on Real Klines

You can't trust a strategy you haven't backtested. I built a walk-forward engine that pulls historical klines from Bybit's REST API and replays them day by day:

```python
# Each day: scan → score → simulate entry → track PnL
for day in trading_days:
    klines = fetch_klines(symbol, start=day.start_ms)
    signals = scan(klines, strategy='bollinger_grid')
    for sig in signals:
        trade = simulate(sig, klines[day:day+30])
        results.append(trade)
```

Tested on BTC, ETH, SUI, and ADA with Daily signals:

| Symbol | Win Rate | Avg PnL | Best Trade | Worst Trade |
|--------|----------|---------|------------|-------------|
| SUIUSDT | 42.9% | +6.56% | +27.3% | −12.1% |
| ADAUSDT | 38.5% | +4.82% | +19.4% | −11.7% |
| BTCUSDT | 35.7% | +3.11% | +14.2% | −9.3% |
| ETHUSDT | 33.3% | +1.95% | +11.8% | −10.5% |

Not every strategy is a winner. The backtest exposed that ETH is borderline — win rate barely above random, average PnL close to fees. That's valuable information. I'd rather know from backtesting than from a blown account.

---

## Risk Management: The Boring Stuff That Saves Accounts

### Partial Take-Profit

Most bots close a position all at once. Partial TP scales out gradually:

```python
# Dynamic split: 20% at first TP → up to 50% at final TP
tp_levels = calculate_partial_tp(
    entry_price, mark_price, unrealized_pnl_pct
)
# First fill: 20% of position, SL moves to breakeven
# Second fill: 30% more, trailing SL activates
# 50% left rides with trailing stop
```

No numpy. Pure `statistics` module. Works on any Python 3.11+ without extra dependencies.

### Trailing Stop for x10

High-leverage positions move fast. The new `trailing_sl_x10` module runs every heavy cycle, but **only for positions with leverage ≥10**. It tightens the stop-loss when:

- W-BB drops below 25% (price approaching lower band for LONG)
- PnL exceeds 15% (lock in profits)
- Update threshold: 0.5% from current mark price (avoids SL churn)

### Auto Funding Rotation

Negative funding rates eat your margin. The rotation module checks every heavy cycle:

1. If any open position has funding rate below −0.01%: flag it
2. If a better alternative exists (positive funding + BB signal): close current, open new
3. Order matters: **open first, then close** — never be out of the market during rotation

---

## The Telegram Bot That Survived Production

The @Gridbolbot (formerly @GridSignalBot) is a 2,153-line Telegram bot that scans markets, sends alerts, and lets users execute trades inline. Five months in, it went silent.

Not "slow" — dead silent. But the logs were clean, the process was running, systemd showed `active`. Classic Heisenbug.

I ran a **3-echelon AI audit** — three agents in parallel, each with a different focus:

- **Source-Driven:** code vs official documentation
- **Security:** secrets, CVEs, command injection
- **Adversarial:** race conditions, blocking calls, logic bugs

Four minutes later: **14 findings**. Five CRITICAL. Highlights:

1. `_valid_symbol()` — called in 3 places, **defined in zero**
2. Nine blocking `subprocess.run()` calls inside async handlers
3. Race condition in daily scan limit (`UPDATE ... SET +1` without WHERE check)
4. Ghost buttons in the keyboard with no handlers
5. SQLite without WAL mode, database sitting in `.gitignore` blind spot

After fixes: **45/45 smoke tests green**, bot responds instantly. Full story in the [separate article](https://dev.to/alexey_polyakov_cfe2095e3/i-built-an-ai-native-trading-engine-in-python-heres-how-it-works-54p0).

---

## Testing: 45 Tests, Zero External Services

Every fix, every feature, every release — the smoke test suite runs:

```python
@pytest.mark.asyncio
async def test_scan_button_does_not_block_event_loop():
    """Verify scan handler yields control back to event loop."""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = CompletedProcess(...)
        start = time.monotonic()
        await cmd_scan(update, context)
        assert time.monotonic() - start < 0.5

def test_race_condition_scan_limit():
    """Double-tap must not exceed daily limit."""
    db.execute("UPDATE users SET scans_today = 9 WHERE id = 1")
    await cmd_scan(...)   # 10th — pass
    with pytest.raises(RateLimitExceeded):
        await cmd_scan(...)  # 11th — blocked
```

Tools: `pytest`, `pytest-asyncio`, `unittest.mock`, SQLite `:memory:` databases. No external services — pure deterministic tests in under 2 seconds.

45 tests. 0 failures. CI green.

---

## Numbers That Matter

| Metric | v1 (Jan 2026) | v2 (Jun 2026) |
|--------|---------------|---------------|
| Strategies | 9 | 12 |
| Codebase | 2,100 lines | 4,600+ lines |
| Test coverage | 0 tests | 45 smoke tests |
| ML layer | None | RandomForest F1=0.69 |
| Backtesting | None | Walk-forward on REST klines |
| Risk management | Basic SL | Partial TP + trailing x10 + funding rotation |
| Telegram bot | Working, untested | 45/45 tests, 14 bugs fixed |
| Deployment | Manual | systemd + white-label script |
| Monitoring | None | Prometheus /metrics + daily health alerts |
| Memory | ~200 MB | ~23.5 MB (SQLite beats in-memory dicts) |

The memory drop isn't a typo. Moving from Python dicts to SQLite cut RAM by 88%.

---

## Roadmap: Phase 4

- 🔜 **ATR-based risk sizing** — position size from volatility, not fixed
- 🔜 **Multi-timeframe confluence** — D/W/M agreement required for entries
- 🔜 **Grafana dashboard** — real-time PnL, drawdown, position heatmap
- 🔜 **Telegram Mini App** — dashboard right in Telegram
- 🔮 **OKX/Binance support** — same strategies, more liquidity

---

## Try It

```bash
git clone https://github.com/poliakarmai/bybit-ws
cd bybit-ws
cp config.example.yaml config.yaml
# Insert your Bybit API keys
pip install -r requirements.txt
python -m bybit-ws
```

Or deploy as a systemd service:

```bash
sudo cp bybit-ws.service /etc/systemd/system/
sudo systemctl enable --now bybit-ws
```

**GitHub:** [poliakarmai/bybit-ws](https://github.com/poliakarmai/bybit-ws)  
**Bot:** [@Gridbolbot](https://t.me/Gridbolbot)  
**License:** AGPL-3.0

---

*The author is a trader and AI engineer. Writes about trading infrastructure, multi-agent systems, and the boring risk management that actually saves accounts.*
