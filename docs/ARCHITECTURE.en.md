# ARCHITECTURE.md — bybit-ws v11.5

**English** · [Русский](ARCHITECTURE.md)

> **Purpose:** HOW the bot is structured (internal architecture).
> For functionality: [CAPABILITIES.md](CAPABILITIES.md). For API: [API.md](API.md).

> **Version:** 11.5 | **Date:** 2026-10-25 | **Python 3.11+** | **asyncio** | **SQLite SSOT**
> **Verified against code and the live system:** 2026-11-01. Verify facts with commands from the
> “Parameters” and “Prometheus Metrics” sections, not from memory: the tables below have already diverged from reality once.

## SSOT (Single Source of Truth)

**SQLite state.db is the only source of truth.** All JSON files (`metrics.json`, `short_positions.json`) are read-only caches/backups (positions are returned by `async_positions_snapshot_ws()`, which is a function, not a file). Data is written ONLY to SQLite. On startup, JSON caches are recreated from SQLite. Dual-write is not used.

## Main Loop (async, 30s)

```
CYCLE (30s)
 ├─ Positions + orders snapshot (REST, 20s timeout)
 ├─ Lightweight checks (each cycle):
 │   ├─ SL check + fix (ATR-adaptive, capped -50%/+50%)
 │   ├─ Trailing SL
 │   ├─ Breakeven SL (every 4 cycles)
 │   └─ Margin check + Circuit Breaker
 ├─ Heavy cycle (every 10 cycles = 5 min):
 │   ├─ BB pre-fetch (batch, 5 min cache)
 │   ├─ Market regime (LSTM → NEUTRAL fallback)
 │   ├─ Black Swan check (`check_correlation_panic`: 5+ red positions AND uPnL < -$150)
 │   ├─ Auto-SHORT + Dry Spell Throttle
 │   ├─ Correlations + Pumps/Overbought
 │   ├─ DCA + Partial TP
 │   ├─ Auto-Entry (LONG) — 7 filters:
 │   │   ├─ MTF Confluence (D+W+M)
 │   │   ├─ Orderbook Imbalance
 │   │   ├─ Volume Confirmation
 │   │   ├─ Entry Judge (LLM DeepSeek, fail-closed)
 │   │   ├─ Correlation sizing (r>0.85→block, r>0.70→×0.5)
 │   │   ├─ Post-trade cluster check (<40% WR→block)
 │   │   └─ Risk Manager (CB, margin, max pos, banned)
 │   ├─ Auto-TP (ATR-based: 1.0×/2.0×/3.0× ATR)
 │   ├─ TP/SL Self-Check (direct REST)
 │   ├─ Time-Based Exit (6h/48h)
 │   ├─ Post-trade cluster analysis (once a day)
 │   └─ Journal insights (once a day)
 ├─ Self-learning (event-driven: ≥10 trades and ≥6h since the previous run, otherwise 24h fallback)
 │   ├─ journal/analyzer.py — FIFO + bias diagnostics
 │   ├─ journal/self_learn.py — min_score, SL/TP adaptation
 │   └─ post_trade.py — cluster analysis, <40% WR block
 ├─ pump_state auto-cleanup (each cycle)
 ├─ Heartbeat (every 12h)
 └─ Reporting
```

## Parameters (prod)

| Parameter | Value | Source |
|----------|----------|--------|
| Max positions | 5 | `position_sizing.max_positions` (defaults: LONG 12, JUNK 2 — `config.py:199`, `config.py:103`) |
| Max total margin | $300 | `risk.max_total_margin`, default `risk_manager.py:935` |
| Max daily loss | $200 | `risk.max_daily_loss` |
| Max gross leverage | 5.0 | `risk.max_gross_leverage` |
| Cycle time | 30s | `bybit_ws/main_async.py` |
| Heavy cycle | 300s (10 cycles) | `bybit_ws/main_async.py` |
| Healthcheck | `GET /health` — timestamp of the last cycle | `rpc.py` |
| RAM | varies; do not store as a number | `systemctl --user show bybit-ws-async -p MemoryCurrent` |

Value verification (not from memory):

```bash
python3 - <<'EOF'
import yaml, os
c = yaml.safe_load(open(os.path.expanduser('~/.config/bybit-ws/config.yaml')))
print('risk:', c.get('risk'))
print('position_sizing:', c.get('position_sizing'))
EOF
```

Session adjustments (`session_params.py`) change `max_positions` and SL/TP multipliers for
NY open / Asia night / Weekend — their values live in the code, not in this table.

## Where the Code Lives

Modules are in the `bybit_ws/` package. Some root files are symlinks to the same sources
(`bybit_ws/rpc.py -> ../rpc.py`), while some exist only in the package (`bybit_ws/post_trade.py`).
Look for a module in `bybit_ws/`, not in the root: `ls -l bybit_ws/*.py | grep '^l'` shows
what is duplicated and what is not. Full list — `ls bybit_ws/*.py`.

```
bybit_ws/
├── main_async.py         ← main loop (async, 30s) + heavy cycle every 10
├── api.py / exchange_adapter.py  ← Bybit v5 REST + HMAC + retry
├── ws_client.py          ← WebSocket: kline, tickers, BB cache
├── rpc.py                ← HTTP-RPC :8766, /metrics, one-click (Bearer)
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout 5s), _migrate() migrations
├── auto_entry.py         ← LONG: MTF, order book, volume, judge, correlations
├── auto_short.py         ← SHORT + JUNK + DCA
├── unified_sl.py         ← unified SL (tight>simple>hard>BE>auto>default)
├── auto_tp.py / partial_tp.py / trailing_sl.py / time_exit.py
├── risk_manager.py       ← limits, Risk CB, BlackSwan (alert-only), emergency_close
├── entry_judge.py        ← LLM entry judge (fail-closed) + counters for /metrics
├── lstm_regime.py / lstm_world_model.py / ml_scorer.py / dspy_optimizer.py
├── session_params.py     ← NY / Asia / Weekend adjustments
├── watchdog.py           ← entry watchdog (6h window, 20-block threshold)
├── journal/              ← self-learning: adapter → analyzer → self_learn
└── experiments/          ← offline lab: engine, validation, triple_barrier, bandit, meta_label
```

In the repository root: `deploy.sh`, `verify.sh`, `backup_db.sh`, `test_smoke.py`, `reconcile.py`,
`paper_trade.py`, `tests/`, `docs/`.

## Auto-SL — ATR-adaptive

| Mode | ATR/Price | k (multiplier) |
|-------|-----------|---------------|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

SL = entry ± k × ATR(14), capped at -50%/+50% from entry.

## Auto-TP — ATR-based 3 Levels

| Level | k | % of volume |
|---------|---|----------|
| TP1 | 1.0 | 40% |
| TP2 | 2.0 | 35% |
| TP3 | 3.0 | 25% |

PERM_SKIP with 24h time-decay. Flag: `BYBIT_ATR_TP_ENABLED=1`.

## JUNK Mode (Junk Shorts)

For symbols with pumps >80%:
- **SL:** -15% hard stop (not “without SL”)
- **Max position:** no more than $5 margin
- **Max simultaneous:** no more than 2 JUNK positions
- **Circuit breaker:** 2 JUNK closures at -15% → mode disabled for 24h
- **pump_state:** auto-cleanup when the position is closed

## ML/LLM Components

| Component | File | Purpose | Fail mode |
|-----------|------|-----------|-----------|
| Entry Judge | entry_judge.py | LLM verdict pass/revise | fail-closed |
| ML Gate (RF) | ml_scorer.py | Random Forest filter | fail-open |
| DSPy Gate | dspy_optimizer.py | Prompt optimization | fail-open |
| LSTM Regime | lstm_regime.py | Market regime | fallback NEUTRAL |
| Optuna | optuna_tuner.py | Hyperparameters | use of defaults |

### Entry Judge — hard gate

1. One model: direct `api.deepseek.com` (`deepseek-chat`, env `DEEPSEEK_JUDGE_MODEL`) via `~/.hermes/scripts/cross-model-judge.py`, prompt-mode `signal`
2. Response = `content or reasoning_content` (reasoning models return empty `content`)
3. Failure/timeout → revise (block, fail-closed)
4. 3 consecutive failures → disabled for 1 hour (Judge CB)
5. Cache: 300s TTL, key includes `sl_price:tp_price`
6. Timeout: 20s (`JUDGE_TIMEOUT`; 5s cut off live calls — Qwen/Nemotron removed 17.10)

## Self-Learning (event-driven: ≥10 trades and ≥6h since the previous run, otherwise 24h fallback)

- journal/analyzer.py — FIFO matching, win rate, P/L ratio, 4 bias diagnostics
- journal/self_learn.py — min_score adaptation (+30% at WR<40%), SL (+20%), bias flags
- post_trade.py — cluster analysis (symbol×regime×session), <40% WR block
- Log: self_learn.jsonl — audit trail of all changes

## Session Adaptation

| Session | BB period | SL mult | TP mult | Max pos | Entry bonus |
|--------|-----------|---------|---------|---------|-------------|
| NY open | +5 | 0.7 | 1.2 | 5 | +10 |
| Asia | -5 | 1.3 | 1.0 | 10 | -5 |
| Weekend | +10 | 0.8 | 0.8 | 3 | +15 |
| Normal | 0 | 1.0 | 1.0 | 8 | 0 |

## Feature Flags

| Flag | Prod | What it does |
|------|------|-----------|
| BYBIT_WS_BB_ENABLED | 1 (default) | WS for kline/BB cache |
| BYBIT_WS_FULL_ENABLED | 0 (default) | Real-time positions via WS |
| BYBIT_ML_ENABLED | **0 (prod env)** | RF ML Gate |
| BYBIT_DSPY_ENABLED | **1 (prod env)** | DSPy gate |
| BYBIT_OPTUNA_ENABLED | 0 (prod env) | Optuna parameters |
| BYBIT_REGIME_AUTO | **1 (prod env)** | LSTM → auto LONG/SHORT + `BYBIT_REGIME_MIN_CONFIDENCE` (60) |
| BYBIT_WORLD_MODEL | **1 (prod env)** | World-model score in SHORT scoring |
| BYBIT_AB_ENABLED | **1 (prod env)** | A/B testing |
| BYBIT_ENTRY_JUDGE_ENABLED | **1 (prod env)** | LLM entry judge |
| BYBIT_ATR_TP_ENABLED | 1 (default) | ATR-based TP |

> “Prod env” values were read from `~/.config/bybit-ws/env` on 25.10 — previously the table showed defaults
> and diverged from reality (`DSPY/AB/REGIME_AUTO` were actually enabled, `ML` was disabled).

## Circuit Breakers (two independent)

**Risk CB** — based on daily PnL: triggers at >80% of max_daily_loss.
**Judge CB** — based on LLM errors: 3 consecutive failures → Entry Judge disabled for 1 hour.

## Graceful Shutdown

On SIGTERM: `log_event` → exit. Without blocking SL checks.

## Backup & Recovery

```
# Automatic: systemd timer bybit-backup.timer, OnCalendar=daily (00:00)
#   → /bin/bash ~/bybit-ws/backup_db.sh
# Check when the last one ran: systemctl --user list-timers bybit-backup.timer
# Manual snapshot before deployment: cp state.db state.db.bak
# On startup: PRAGMA integrity_check
# WAL recovery: automatic (SQLite)
```

## Health Check and Entry Watchdog

- `GET /health` returns the timestamp of the last successful cycle; `verify.sh` (check #3)
  reads `cycle_count` twice and requires it to increase — this catches a stuck cycle.
- There is no external cron “every 90 seconds.” The watchdog is `bybit_ws/watchdog.py`, launched
  by the Hermes cron hourly: 6h window, 0 entries **and** ≥20 blocks → alert.
  Lines saying “… skipping” are active candidate filtering, not blocking.

```bash
python3 -m bybit_ws.watchdog --hours 6 --dry-run --json
```

## Data Flow

```
Bybit API ←→ api.py (REST, HMAC)
              ↕
Bybit WS  ←→ ws_client.py (kline, tickers, orderbook)
              ↕
main_async.py ←→ state_db.py (SQLite WAL)
   ↓                  ↓
rpc.py (:8766)    journal/
   ↓                  ↓
MCP/Hermes        self_learn.jsonl
Grafana           post_trade_features.jsonl
Android App
```

## Dashboard Connection to RPC

```
web/proxy_server.py (:8765)
    → HTTP GET /rpc/positions
        → rpc.py (:8766)
            → SQLite query
                → state.db
```

## Deployment

```bash
1. cp state.db state.db.bak           # backup
2. git pull                            # new code
3. bash deploy.sh --force              # atomic symlink swap + tests
4. systemctl --user restart bybit-ws-async
5. curl http://127.0.0.1:8766/health  # verify
```

## Retry Policy (Bybit API)

| Error | Strategy |
|--------|----------|
| 5xx | Retry 3× (1s, 2s, 4s backoff) |
| 429 (rate limit) | Wait 60s + retry |
| Network error | Retry 5× (1s, 2s, 4s, 8s, 16s) |
| Bybit API CB | 5 consecutive errors → pause 5 min; 10 errors/hour → pause 30 min |

## SQLite Connection

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
```

All modules use a shared connection pool (`check_same_thread=False`).

## Logging

Format: `[2026-06-28 12:00:00] LEVEL module: message`.

| Level | Purpose |
|---------|-----------|
| DEBUG | Cycle details |
| INFO | Entries, exits, TP/SL |
| WARNING | Skips, CB |
| ERROR | API errors, timeouts |
| CRITICAL | Black swan, kill switch |

## Prometheus Metrics

`/metrics` requires a Bearer token (as does `/positions`). Token: `SELECT value FROM kv_store WHERE key='rpc_auth_token'`.

| Series | Type | What it shows |
|-------|-----|----------------|
| `bybit_ws_active_positions{side}` | gauge | Open positions by side |
| `bybit_ws_unrealized_pnl` | gauge | Unrealized PnL |
| `bybit_ws_uptime_seconds` | gauge | Process uptime |
| `bybit_ws_cycle_duration_seconds` | gauge | Duration of the last cycle |
| `bybit_ws_cycle_count` | counter | Cycle number (increases — process is alive) |
| `bybit_ws_daily_pnl` | gauge | Realized PnL for today |
| `bybit_ws_judge_calls` | counter | Judge calls (after all skips) |
| `bybit_ws_judge_verdicts_total{verdict}` | counter | `pass` / `revise` verdicts |
| `bybit_ws_judge_errors_total` | counter | Judge script failed or returned garbage |
| `bybit_ws_judge_timeouts_total` | counter | Judge timeouts |
| `bybit_ws_judge_breaker_hits_total` | counter | Open breaker activations |
| `bybit_ws_judge_fallback_open_total` | counter | Entries in degraded mode (fail-open) |
| `bybit_ws_judge_cache_hits_total` | counter | Verdict cache hits |
| `bybit_ws_judge_last_latency_ms` | gauge | Latency of the last call |

> Judge series are counted in process memory “since startup” and reset on restart
> (history — `events.log`). In the code as of 01.11.2026, they appear after a service restart.

```bash
TOKEN=$(sqlite3 ~/.local/share/bybit-ws/state.db "SELECT value FROM kv_store WHERE key='rpc_auth_token'")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/metrics | grep '^bybit_ws'
```

## Alerting Rules

| Level | Condition | Channel |
|-------|---------|-------|
| CRITICAL | Black swan (5+ red positions and uPnL < -$150) | ntfy + Telegram |
| CRITICAL | Risk CB triggered | ntfy + Telegram |
| WARNING | Judge down >30min | Telegram |
| WARNING | Bybit API errors >5/min | Telegram |
| INFO | New position | Telegram |
| INFO | Position closed (PnL) | Telegram |
| INFO | Self-learning applied | events.log |

## Testing Strategy

| Type | Description | Coverage |
|-----|---------|---------|
| Unit/integration | `python3 -m pytest -q` | entire suite green (`--timeout=90`); number — from the run output |
| Smoke | deploy validation (`test_smoke.py`) | 52/52 |
| CI | GitHub Actions, GSC security scan | on every push |

## Security

| Level | Mechanism |
|---------|---------|
| state.db | chmod 600, owner openclaw |
| API keys | .env (chmod 600), not in logs |
| RPC token | UUID v4, rotation via /reset-token |
| Emergency token | separate in .env (EMERGENCY_TOKEN) |
| Firewall | only :8766 (RPC), :8888 (Grafana) |
| HTTPS | nginx + Let's Encrypt (production) |

## Disaster Recovery

| Parameter | Value |
|----------|---------|
| RTO | <15 minutes |
| RPO | <24 hours (daily backup at 00:00, `bybit-backup.timer`) |
| Backup | systemd timer daily + manual snapshot before deployment |
| Recovery | restore state.db + systemctl restart |
| DR drill | quarterly |

## Troubleshooting

**Bot does not start:**
```bash
systemctl --user status bybit-ws-async
journalctl --user -u bybit-ws-async -f
# SQLite lock: rm state.db-wal state.db-shm (safe)
```

**Positions do not close:**
```bash
curl http://127.0.0.1:8766/positions
# Check directly on Bybit — is the position still open?
```

**Judge does not work:**
```bash
TOKEN=$(sqlite3 ~/.local/share/bybit-ws/state.db "SELECT value FROM kv_store WHERE key='rpc_auth_token'")
curl -s http://127.0.0.1:8766/rpc/circuit_breaker -H "Authorization: Bearer $TOKEN"
systemctl --user restart bybit-ws-async   # reset CB — restart only
```

**High memory:**
```bash
ps aux | grep bybit-ws
systemctl --user restart bybit-ws-async
```