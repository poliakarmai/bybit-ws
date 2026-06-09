# bybit-ws Quickstart for AI Agents

> **Goal:** Go from zero to trading in 5 minutes.
> **Audience:** AI coding agents (Claude Code, Codex, Cursor, Hermes).
> **You need:** A Bybit account with API keys + a Linux server (or Docker).

---

## 1. Get API Keys (1 min)

1. Go to [Bybit](https://www.bybit.com) → Account → API Management
2. Create API Key with:
   - ✅ **Read-Write** permissions
   - ✅ **No withdrawal** permissions (safety)
   - ✅ **Unified Trading Account** enabled
3. Copy `API_KEY` and `API_SECRET`

## 2. Install (1 min)

### Option A: Docker (recommended)

```bash
git clone https://github.com/poliakarm/bybit-ws.git
cd bybit-ws
cp config.example.yaml config.yaml
# Edit config.yaml → set api.key, api.secret, rpc.auth_token
docker-compose up -d
```

### Option B: Bare metal

```bash
git clone https://github.com/poliakarm/bybit-ws.git ~/.local/lib/bybit_ws
cd ~/.local/lib/bybit_ws
python3 -m pip install -r requirements.txt
mkdir -p ~/.config/bybit-ws
cp config.example.yaml ~/.config/bybit-ws/config.yaml
# Edit ~/.config/bybit-ws/config.yaml → set api.key, api.secret
python3 -m bybit_ws.main
```

## 3. Verify (1 min)

```bash
# Health check — should return {"status":"ok","cycle_seconds":30}
curl http://localhost:8766/health

# List current positions
curl http://localhost:8766/positions

# Scan for SHORT candidates
curl http://localhost:8766/scan?mode=short
```

## 4. Your First Trade (2 min)

### Scan for opportunities

```bash
# LONG opportunities (BB < 25%)
curl "http://localhost:8766/scan?mode=long&limit=5"

# Response example:
# {"signals":[{"symbol":"ADAUSDT","direction":"LONG","bb_position":12.3,
#   "lower_bb":0.42,"middle_bb":0.48,"score":7.5,"tier":"A"}]}
```

### Enter a position

```bash
curl -X POST http://localhost:8766/enter \
  -H "Content-Type: application/json" \
  -d '{"symbol":"ADAUSDT","side":"Buy","qty":100}'

# Response: {"order_id":"abc123...","status":"filled"}
```

The monitor will auto-set SL and TP based on your strategy config.

### Check your position

```bash
curl http://localhost:8766/positions
# {"positions":[{"symbol":"ADAUSDT","side":"Buy","size":100,"markPrice":0.43,
#    "unrealisedPnl":1.23,"stopLoss":0.39,"takeProfit":0.48}]}
```

### Close manually (optional)

```bash
curl -X POST http://localhost:8766/close \
  -H "Content-Type: application/json" \
  -d '{"symbol":"ADAUSDT"}'
```

---

## API Reference (TL;DR)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Monitor status + cycle freshness |
| GET | `/positions` | All open positions with PnL |
| GET | `/orders` | Active limit orders |
| GET | `/scan?mode=long\|short&limit=5` | BB scan with scoring |
| GET | `/signals?mode=long\|short` | Cached signals from last scan |
| GET | `/metrics` | Daily stats (SL/TP count, entries) |
| POST | `/enter` `{"symbol","side","qty"}` | Place market order |
| POST | `/close` `{"symbol"}` | Close position by symbol |
| POST | `/pause` | Pause auto-entry |
| POST | `/resume` | Resume auto-entry |
| GET | `/logs?lines=50` | Recent log entries |
| POST | `/reload-config` | Hot-reload config.yaml |

**Auth:** `Authorization: Bearer <RPC_TOKEN>` (optional; localhost skips auth)

---

## Systemd Service (optional)

```bash
cat > ~/.config/systemd/user/bybit-ws.service << 'EOF'
[Unit]
Description=Bybit Bollinger Grid Monitor
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m bybit_ws.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now bybit-ws
```

---

## What Happens Next?

After your first trade, the monitor:

1. **Every 30s:** fetches positions, checks SL/TP triggers
2. **Every 5 min:** scans for new entries (BB + scoring + correlation check)
3. **On fill:** auto-sets SL (trading-stop) and TP (Middle BB)
4. **On SL hit:** logs to `trades.jsonl`, starts cooldown timer
5. **On emergency:** if drawdown > 15% → closes all positions

**You don't need to touch it again.** The agent trades autonomously within your risk limits.

---

## Next Steps for AI Agents

1. Read [`DESIGN.md`](./DESIGN.md) — full architecture and strategy details
2. Read [`ERRORS.md`](./ERRORS.md) — what each error means and how to handle it
3. Use [`bybit_ws_sdk.py`](./bybit_ws_sdk.py) — Python SDK: `Monitor` + `WebhookHandler`
4. Check [`STRATEGIES.md`](./STRATEGIES.md) — all 8 strategies with parameters

**Python SDK quick example:**

```python
from bybit_ws_sdk import Monitor

m = Monitor("http://localhost:8766", token="your-token")

# Health
m.health()  # → {"status": "ok", "cycle_seconds": 30}

# Scan
signals = m.scan(mode="short", limit=5)
for s in signals.get("signals", []):
    if s["score"] >= 7.0:
        m.enter(s["symbol"], "Sell", s["qty"])
```
