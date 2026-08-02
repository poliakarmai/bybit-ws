# Operations

This page covers risk management, deployment, notifications, health monitoring, environment configuration, and security.

---

## Risk Management (`risk_manager.py`)

The risk manager is the **final gate** in the entry pipeline. It operates as a fail-closed circuit breaker — if any check fails, the entry is blocked.

### BlackSwan Multi-Tier Protection

Three escalation levels based on BTC price drop magnitude. Checked every cycle (30s):

| Tier | Trigger | Action |
|------|---------|--------|
| **1** | BTC -3% in 15 min | Close 50% of positions (worst PnL first) |
| **2** | BTC -5% in 30 min | Close 80% of positions |
| **3** | BTC -8% in 1h | Close 100% (full emergency) |
| **PnL Override** | Total PnL loss > 2× max_daily_loss | Full emergency close |

Positions are sorted by unrealized PnL — worst performing closed first.

### Circuit Breaker (CB)

Two independent circuit breakers:

1. **Risk CB** (auto-reset daily): Activates when daily PnL exceeds 80% of `max_daily_loss`. Blocks all new entries until midnight.
2. **Judge CB** (auto-reset after 1h): Activates after 3 LLM (`entry_judge.py`) failures. Disables the Entry Judge for 1 hour, falling back to score-only entries.

### Daily Drawdown Limit

- Configurable `max_daily_loss` in YAML (e.g., -5% of deposit)
- Includes **unrealized PnL** (v7.6 fix: `risk_manager.py` commit dbce633)
- When hit: circuit breaker activates, all entries blocked for 24h
- State reset at midnight UTC

### Margin Controls

| Check | Limit | Action |
|-------|-------|--------|
| Max total margin (% of deposit) | Configurable | Block new entries |
| Max positions per tier | Configurable | Block tier entries |
| Max LONG positions | 12 (default) | Block LONG entries |
| Max SHORT positions | 3 (default, shared with JUNK) | Block SHORT entries |
| Per-sector concentration | Configurable % | Block sector entries |
| Banned symbols list | Configurable | Block specific coins |

### Liquidation Monitoring (`health.py`)

- Tracks distance to liquidation price per position
- Alert at ≤20% distance, escalating as price approaches
- Margin utilization checks every 30s

**Source:** `/bybit_ws/risk_manager.py` (~34k), `/bybit_ws/health.py` (~7k), `/bybit_ws/margin_alerts.py` (~8k)

---

## Deployment

### Systemd (Primary — `deploy/bybit-ws-async.service`)

```bash
systemctl --user link /path/to/deploy/bybit-ws-async.service
systemctl --user daemon-reload
systemctl --user start bybit-ws-async
systemctl --user status bybit-ws-async
systemctl --user stop bybit-ws-async  # SIGTERM → 10s → SIGKILL
```

Service hardening:
- `ProtectProc=invisible`, `NoNewPrivileges=true`, `PrivateTmp=true`
- `ProtectSystem=strict`, `ProtectHome=read-only` (except data dirs)
- `MemoryMax=512M`, `MemoryHigh=400M`
- Restricted network families: AF_INET, AF_INET6 only
- `SystemCallFilter=@system-service`

Only `~/.local/share/bybit-ws/` and `~/.config/bybit-ws/` are writable.

### Docker (`Dockerfile` + `docker-compose.yml`)

```bash
docker-compose up -d
# healthcheck: curl -f http://localhost:8766/health (30s interval)
```

Mounts `config.yaml` (read-only) and `bybit_data` volume.

### Deploy Script (`deploy.sh`)

5-step automated deployment pipeline:

```
1. Git cleanliness check (or --force)
2. Run test_logic_integrity.py (AST verification)
3. Run test_smoke.py (16 integration tests)
4. Graceful stop (SIGTERM → 10s → SIGKILL)
5. Start service via systemctl --user
6. Canary monitoring: 8 checks × 5s, verify health.txt age < 60s
```

All steps must pass. If canary monitoring fails, the script exits with an error.

**Source:** `/deploy.sh` (~2.4k), `/deploy/bybit-ws-async.service`, `/Dockerfile`, `/docker-compose.yml`

---

## Notifications

### Push Notifier (`push_notifier.py`)

Two channels:

| Channel | Priority | Use Case |
|---------|----------|----------|
| **ntfy** (primary) | max/high/default | SL hit, entry, TP, critical alerts |
| **Telegram** (fallback) | N/A | Daily summaries, health warnings |

Priority mapping to ntfy:
| Priority | ntfy Level | ntfy Tags | Sound | When |
|----------|------------|-----------|-------|------|
| CRITICAL | max (5) | warning,siren | Siren | SL hit, liquidation, CB trip |
| HIGH | high (4) | arrow_up | Alert | Entry, TP, DCA buy |
| NORMAL | default (3) | bell | Silent | Signals, confluence, info |

**Deduplication:** Same alert hash blocked for 5 minutes.

**Source:** `/bybit_ws/push_notifier.py` (~13k), `/bybit_ws/alerts.py` (~8k)

### Alert Types (from `reporting.py` and `health.py`)

| Alert | Trigger | Channel |
|-------|---------|---------|
| Daily summary | Every 24h (or at profit/loss thresholds) | Telegram |
| Profit trigger | PnL > configurable threshold | Push + Telegram |
| SL hit | Stop loss executed | Push (CRITICAL) |
| TP hit | Take profit executed | Push (HIGH) |
| Liquidation warning | Distance to liq ≤ 20% | Push (CRITICAL) |
| BB squeeze | BB width < 2% across watchlist | Push (HIGH) |
| Funding flip | Funding rate sign change | Push (HIGH) |
| Drawdown cooldown | Daily drawdown limit hit | Push (CRITICAL) |

---

## Health Monitoring (`health.py`)

Every light cycle (30s):
- **Liquidation distance** per position (alerts at ≤20%, ≤10%, ≤5%)
- **BB squeeze** detection (BB_WIDTH < 2% for 25+ symbols)
- **Funding rate flips** (negative→positive and vice versa)
- **Drawdown cooldown** state tracking

File-based health: `~/.local/share/bybit-ws/health.txt` (timestamp) — checked by deploy canary every 5s for 40s after restart.

---

## Environment Variables

### Required (no defaults)

| Variable | Purpose |
|----------|---------|
| `BYBIT_API_KEY` | Bybit API key |
| `BYBIT_API_SECRET` | Bybit API secret |
| `RPC_TOKEN` | RPC authentication token (required if bind ≠ 127.0.0.1) |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `TG_BOT_TOKEN` | — | Telegram bot token |
| `TG_CHAT_ID` | — | Telegram chat ID |
| `NTFY_TOPIC` | — | ntfy push topic |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server URL |
| `PUSH_ENABLED` | 1 | Enable/disable push notifications |
| `BYBIT_WS_PRODUCTION` | 0 | Production guard mode |
| `BYBIT_DATA_DIR` | `~/.local/share/bybit-ws` | Override data directory |
| `BYBIT_ML_ENABLED` | 1 | RandomForest ML Gate |
| `BYBIT_DSPY_ENABLED` | 0 | DSPy prompt optimization |
| `BYBIT_OPTUNA_ENABLED` | 0 | Optuna tuning |
| `BYBIT_REGIME_AUTO` | 0 | LSTM regime auto-detection |
| `BYBIT_AB_ENABLED` | 0 | A/B testing |
| `BYBIT_WS_FULL_ENABLED` | 0 | Real-time WebSocket positions |
| `OPENAI_API_KEY` | — | LLM Entry Judge API key |
| `BYBIT_HMAC_SECRET` | — | Model integrity HMAC key |
| `PYTHONUNBUFFERED` | — | Unbuffered Python output (set by systemd) |

**Source:** `/.env.example`, `/bybit_ws/feature_flags.py`

---

## Security Model

Based on `/docs/SECURITY.md` (the most comprehensive document in the repo, ~25k):

### Key Principles

1. **Least privilege**: Bybit API key has only futures trade + read permissions (no withdrawal)
2. **Network isolation**: RPC server binds to `127.0.0.1` by default; external access requires WireGuard
3. **Token rotation**: `POST /reset-token` rotates RPC token with 5-minute grace period
4. **Rate limiting**: Token bucket (60/min) per IP
5. **CORS**: Restricted to `localhost:*` and known agent domains
6. **Emergency auth**: Separate `X-Emergency-Auth` header for `/emergency_close` and `/kill_switch`
7. **Encrypted backups**: `age`-encrypted backups of `state.db`
8. **Systemd hardening**: NoNewPrivileges, ProtectSystem=strict, limited syscalls

### Attack Surface

| Vector | Mitigation |
|--------|------------|
| RPC token leak | Grace period rotation, CORS, rate limiting |
| Bybit API leak | Permission-limited key, no withdrawal |
| Network exposure | Bind to 127.0.0.1, WireGuard for remote |
| Configuration disclosure | Secrets masked in `/rpc/config` |
| Replay attacks | `recv_window` (5s), idempotency keys |
| Code tampering | Git audit trail, deploy.sh checks git status |

**Source:** `/docs/SECURITY.md`, `/bybit_ws/rpc.py` (auth, rate limiting sections)

---

## Operational Scripts

| Script | Purpose | When to Use |
|--------|---------|-------------|
| `deploy.sh` | Full deployment pipeline | Every code change |
| `cleanup.py` | Remove expired/stale orders | Auto-run, can trigger manually |
| `recycle.py` | Re-invest TP profits into new limit orders | Auto-run (max 1x per 30 min per symbol) |
| `backtest/__init__.py` | Backtesting engine | Strategy evaluation |
| `backtest/bb_strategy.py` | BB strategy backtest | Strategy evaluation |

---

## Data Backup

- SQLite WAL mode means the `state.db` file is always consistent
- Backups go to `~/.local/share/bybit-ws/backups/` (age-encrypted)
- Trade history also stored as append-only JSONL (`trades.jsonl`) — cannot be lost by DB corruption

---

## Change Guidance

When modifying risk management (`risk_manager.py`):
- The `_get_config()` function uses a lazy singleton with `_FailsafeConfig` fallback — never assume config is available
- BlackSwan thresholds should never be lowered blindly; test with `test_smoke.py`
- Circuit breaker state is in-memory, not persisted — restarts reset CB

When modifying the deploy script (`deploy.sh`):
- The script runs from the repo root
- Canary monitoring expects `health.txt` to be updated every 60s
- `--force` flag skips git cleanliness check (use only in emergencies)

When adding notification types:
- Add to `push_notifier.py` with appropriate priority level
- Add dedup hash logic to prevent alert storms
- Document new alert in `/docs/WEBHOOKS.md`

When changing environment setup:
- Update `/.env.example` with new variables
- Update `feature_flags.py` if adding a togglable feature
- Update systemd `EnvironmentFile` path if moving config
