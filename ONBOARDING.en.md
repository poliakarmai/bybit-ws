# Onboarding — from zero to the first trade

**English** · [Русский](ONBOARDING.md)

> Step-by-step guide. 15 minutes—and bybit-ws trades for you.

---

## Step 1: Bybit API keys (3 minutes)

1. Open [Bybit API Management](https://www.bybit.com/app/user/api-management)
2. Click **Create New Key** → **System-generated API Key**
3. Configure:
   - **API Key Type**: Unified Trading Account
   - **Permissions**: Read-Write + Trade (❗ required)
   - **IP Whitelist**: add your VPS IP (`curl ifconfig.me`)
   - **No withdrawal permission** — security
4. Save the **API Key** and **API Secret** (the Secret is shown only once!)

> **Testnet first!** Create separate keys on [testnet.bybit.com](https://testnet.bybit.com) for testing.

---

## Step 2: Server (3 minutes)

```bash
# Minimum requirements: Ubuntu 22.04+, 1GB RAM, 10GB disk
# Recommended: $5/month VPS (Hetzner, DigitalOcean, Vultr)

# Installation
ssh root@your-server
apt update && apt install -y python3 python3-pip git

# Clone
git clone https://github.com/poliakarmai/bybit-ws.git
cd bybit-ws
pip install -r requirements.txt

# Check
python3 test_smoke.py
# Expected: PASS=52 FAIL=0
```

---

## Step 3: Configuration (4 minutes)

```bash
# Configuration
cp config.example.yaml ~/.config/bybit-ws/config.yaml
nano ~/.config/bybit-ws/config.yaml
```

Important parameters in `config.yaml`:

```yaml
api:
  base_url: "https://api-testnet.bybit.com"  # testnet first!

risk:
  max_daily_loss: 50         # daily loss limit
  emergency_close_all: true  # close everything on breach

strategy:
  long:
    max_positions: 5         # moderate for the start
  short:
    enabled: false           # start with LONG only

position_sizing:
  long_risk_pct: 0.10        # 10% of the deposit at risk
```

```bash
# Secrets (chmod 600 — required!)
cat > ~/.config/bybit-ws/.env << 'EOF'
BYBIT_API_KEY=your_testnet_key
BYBIT_API_SECRET=your_testnet_secret
RPC_TOKEN=$(openssl rand -hex 16)
EOF
chmod 600 ~/.config/bybit-ws/.env
```

---

## Step 4: Pre-launch check (2 minutes)

```bash
# 1. Tests
python3 test_smoke.py                    # should be 52/52 PASS

# 2. Paper trading (backtest)
python3 -m bybit_ws.paper_trade SOLUSDT --days 30

# 3. Config check
python3 -c "from bybit_ws.config import Config; c = Config(); print('OK')"

# 4. Key check (testnet)
python3 -c "
from bybit_ws.api import bybit
r = bybit('GET', '/v5/account/wallet-balance?accountType=UNIFIED')
print(r['result']['list'][0]['totalEquity'] if r['retCode']==0 else r)
"
# Should display the balance
```

---

## Step 5: Launch (2 minutes)

```bash
# Install as a systemd service
mkdir -p ~/.config/systemd/user
cp bybit-ws-async.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bybit-ws-async

# Check
systemctl --user status bybit-ws-async    # active (running)
journalctl --user -u bybit-ws-async -f   # view logs
curl http://localhost:8766/health         # {"status":"alive"}
```

---

## Step 6: Monitoring (1 minute)

```bash
# Positions
curl http://localhost:8766/positions

# Balance
curl http://localhost:8766/balance

# Metrics
curl http://localhost:8766/metrics
```

### Telegram alerts

Add to `.env`:
```bash
TG_BOT_TOKEN=123:abc
TG_CHAT_ID=123456789
```

You will receive notifications:
- 🔵 New entry
- 🟢 TP triggered
- 🔴 SL triggered
- ⚠️ Pump detected
- 🛑 Circuit Breaker

---

## Step 7: Live launch (switching from testnet)

After confirming that everything works on testnet:

1. Create **new** API keys on [mainnet Bybit](https://www.bybit.com/app/user/api-management)
2. Fund the deposit (min. $200 for comfortable trading)
3. Change in `config.yaml`:
   ```yaml
   api:
     base_url: "https://api.bybit.com"  # removed -testnet
   ```
4. Update `.env` with the live keys
5. Restart: `systemctl --user restart bybit-ws-async`

---

## Frequently asked questions

### How much money is needed to start?
- Testnet: $0 (virtual)
- Mainnet LONG-only: $200-500
- Mainnet LONG+SHORT: $500-1000

### Which VPS should I choose?
- Hetzner CX22 (~$4/month) — sufficient
- Most important: stable ping to the Bybit API (<100ms)

### What should I do if positions are not opened?
1. Check: `curl localhost:8766/positions`
2. Check the logs: `journalctl --user -u bybit-ws-async -n 50`
3. Make sure the balance is > $30
4. Check Circuit Breaker: `curl localhost:8766/risk`

### Can I trade manually in parallel?
Yes. The engine does not interfere with manual trades. However:
- Do not place opposite-direction orders (the engine may close them)
- SL/TP are managed by the engine—do not change them manually

### How do I update?
```bash
cd ~/bybit-ws
git pull
python3 test_smoke.py
bash deploy.sh        # atomic deployment with checks
```

---

## Next steps

- [ ] Configure Telegram alerts
- [ ] Run `paper_trade` on 90 days of historical data
- [ ] Optimize the parameters for your risk profile
- [ ] Connect @Gridbolbot for signals to friends
- [ ] Configure monitoring (Prometheus + Grafana)

---

*Questions? [@Poliakarm](https://t.me/Poliakarm) on Telegram.*