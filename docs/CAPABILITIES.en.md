# bybit-ws — Bollinger Grid trading monitor v11.5.x

**English** · [Русский](CAPABILITIES.md)

> ✅ Verified against the code on 2026-10-25 (`entry_judge.py`, `config.py`, service env, `rpc.py`).
> See `CHANGELOG.md` §11.5.0 for exact changes. Documentation map — [INDEX.md](INDEX.md).

Automated Bybit futures trading. 24/7 systemd service. SQLite WAL (SSOT).
Documentation: [ARCHITECTURE.md](ARCHITECTURE.md) (internal design), [API.md](API.md) (endpoints), [ROADMAP.md](ROADMAP.md) (plan).

## 1. Strategy

Bollinger Grid: LONG when the price is below the lower BB band, SHORT when the price is above the upper band.
`BB(20, 2.0)` by default, adapted to the session.

## 2. Entry Decision Tree

```
BB signal (score ≥ min_score)
 ├─ MTF Confluence (D+W+M)     → fail-open: skip filter, -10% score
 ├─ Orderbook Imbalance         → fail-open: skip filter, -10% score
 ├─ Volume Confirmation         → fail-open: skip filter, -10% score
 ├─ Entry Judge (LLM)           → fail-closed: BLOCK
 ├─ Correlation                 → fail-open: skip filter, -10% score
 ├─ Post-trade cluster          → fail-open: skip filter, -10% score
 └─ Risk Manager (CB/margin)    → fail-closed: BLOCK
                                  ↓
                               ENTRY
```

## 3. Entry Judge — hard gate

| Parameter | Value |
|----------|---------|
| Chain | **one model**: direct `api.deepseek.com` through `~/.hermes/scripts/cross-model-judge.py` (`DEEPSEEK_JUDGE_MODEL`, `deepseek-chat` by default). Qwen/Nemotron/OpenRouter removed on 17.10 |
| Mode | fail-closed; response is read as `content or reasoning_content` |
| Timeout | **20s** (`JUDGE_TIMEOUT`; 5s cut off live calls) |
| Prompt-mode | `--mode signal` (evaluates entry quality, not “artifact completeness”), `pass` is explicitly allowed |
| All failed | revise (blocked) |
| Judge CB | 3 failures → disabled for 1h |
| Cache | 300s TTL, key includes `sl_price:tp_price` |
| Enabled | `BYBIT_ENTRY_JUDGE_ENABLED=1` (prod) |

## 4. Auto-SL — ATR-adaptive

Live path: `main_async.py` → `unified_sl.manage_sl` (5 modules merged, priority
tight > simple > hard > BE > auto > default). `auto_sl.py` is **legacy**, connected only to
`main.py`, not used by the service.

**Default SL:** 1.5×ATR(14), distance clamp **3–10%** from entry, fallback −10% (`unified_sl.py:215-233`).

**Trailing (hard trail):** based on weekly BB — LONG when `bb_pos > 75`, SHORT when `bb_pos < 25`;
SL = `entry ± 0.15 × (mark − entry)` (`TRAIL_SL_PERCENT = 0.15`), moves only toward profit.

## 5. Auto-TP — ATR-adaptive (3 legs)

| Leg | Size % | Action |
|------|-----------|---------|
| Leg 1 | 40% | Close 40%, move SL → breakeven |
| Leg 2 | 35% | Close 35%, tighten SL |
| Leg 3 | 25% | Close the remainder |

Split `ATR_TP_SPLITS = [0.40, 0.35, 0.25]` (3 legs, % of size), ATR multipliers are scaled by market regime. BB split 20%/80% is the fallback when ATR is unavailable (guard `not bb_used`). `BYBIT_ATR_TP_ENABLED` is ON by default (`'1'`). PERM_SKIP: 24h time-decay.

## 6. DCA (Dollar Cost Averaging)

| Parameter | Value |
|----------|---------|
| Condition | Drawdown to level: **−10% / −20% / −30%**, margin multiplier **1.5 / 2.0 / 2.5** (`dca.py:35-44`) |
| Maximum | **2 DCA** per position (`max_dca_count`) |
| Margin | base $10, `max_pos_value` $40, `max_margin_per_symbol` $80 |
| Volume filter | **none** in the code (verified on 26.10: neither `vol` nor `sma` in `dca.py`) |
| SL after DCA | **not set** in `dca.py` |

## 7. Pump Detection

| Level | Criterion | Action |
|--------|---------|---------|
| Early warning | Volume spike >5×SMA(20) + price >20%/1h | alert, do not short |
| JUNK | +80% per day (`PUMP_THRESHOLD = 0.80`) | SHORT with 5% SL (`sl_tier_cd`), `max_loss_pct` 15%, `max_hold_hours` 48 |

The JUNK circuit breaker (“2 SL closes → off for 24h”) **is absent from the code** (verified on 26.10).
A similar mechanism is `should_disable_short(window=30, min_pf=1.0)` in `auto_short.py:341`,
but it is a profit-factor filter, not a JUNK-CB.

> `check_junk_dca` is legacy (junk DCA logic), and is not called from the live loop.

## 8. Black Swan — alert-only (no auto-close)

BlackSwan **only sends alerts** and does not close positions itself.

| Trigger | Action |
|---------|---------|
| ≥5 red positions **AND** total uPnL < −$150 | alert asking “Close red positions?” (waits for the user’s response) |

Auto-close is available only through RPC `/emergency_close` or `/kill_switch` (`emergency_close_all()` is called only from there). Thresholds: `BLACKSWAN_MIN_RED_POSITIONS = 5`, `BLACKSWAN_TOTAL_UPNL_THRESHOLD = -150` (comment: “not a close!”).

## 9. Self-Learning

Event-driven: ≥10 trades **and** ≥6h since the previous run; otherwise fallback after 24h (`should_run_self_learn_v6`, min_trades=10, min_hours=6, fallback_hours=24):
- journal/analyzer.py — FIFO matching, win rate, P/L ratio
- journal/self_learn.py — adaptation of min_score (±30%), SL/TP (±20%)
- post_trade.py — cluster analysis, blocks <40% WR

**Canary — DISABLED in prod (verified on 26.10.2026).** `CANARY_ENABLED = False`,
`CANARY_ENTRY_PCT = 0.0`, `CANARY_WINDOW_HOURS = 6` (`bybit_ws/journal/self_learn.py:55-57`);
the service env file contains no `CANARY` at all. Canary code is active and covered by a test
(`tests/test_self_learn_attribution.py:95` records `False`), but it is not applied to live entries:
there is currently no parameter promotion through canary.

## 10. Session Params

> ⚠️ **Dead module.** `bybit_ws/session_params.py` is not imported by anything — `grep -rn SESSION_PARAMS`
> finds only the file itself (verified on 26.10.2026). The table below is historical and does not apply to live entries.
> The live session modifier is `journal/self_learn.get_session_modifier`: `min_score`
> multipliers (US 0.85 → lower threshold) and `tp_mult` (US 1.2 → wider TP); there are no position limits or SL multipliers there.

Max positions = **min(session limit, high_vol limit)**.

| Session | BB adj | SL mult | TP mult | Max pos | Bonus |
|--------|--------|---------|---------|---------|-------|
| NY open | +5 | 0.7 | 1.2 | 5 | +10 |
| Asia | -5 | 1.3 | 1.0 | 10 | -5 |
| Weekend | +10 | 0.8 | 0.8 | 3 | +15 |
| Normal | 0 | 1.0 | 1.0 | 8 | 0 |

high_vol: maximum 5 positions (takes priority over the session limit).

## 11. Risk Manager

| Parameter | Value |
|----------|---------|
| Max positions | min(session, high_vol) |
| Max daily loss | **−$200** (`risk.max_daily_loss` in `Config()`) |
| Max margin | **$300** (`risk.max_total_margin`) |
| Risk CB | 80% of max_daily_loss → **$160** |
| DCA margin per symbol | $80 (`strategy.dca.max_margin_per_symbol`) |
| Kill Switch | full block until /resume (no timeout) |

## 12. SQLite (SSOT)

`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`
Backup every 6 hours (cron). 7-day retention.
At startup: `PRAGMA integrity_check`.

## 13. Feature Flags

| Flag | Prod | Activation plan |
|------|------|---------------|
| Flag | Prod (env) | Default in code | What it does |
|------|-----------|---------------|-----------|
| `BYBIT_ENTRY_JUDGE_ENABLED` | **1** | 0 | LLM entry judge |
| `BYBIT_REGIME_AUTO` | **1** | 0 | LSTM regime → side gate (+`BYBIT_REGIME_MIN_CONFIDENCE`, default 60) |
| `BYBIT_WORLD_MODEL` | **1** | 0 | World-model score in SHORT scoring |
| `BYBIT_AB_ENABLED` | **1** | 0 | A/B in `auto_entry` |
| `BYBIT_DSPY_ENABLED` | **1** | 0 | DSPy gate |
| `BYBIT_ML_ENABLED` | **0** | 0 | Random-Forest ML Gate |
| `BYBIT_OPTUNA_ENABLED` | **0** | 0 | Optuna tuning |
| `BYBIT_WS_BB_ENABLED` | — (default) | 1 | WS candle cache for BB |
| `BYBIT_WS_FULL_ENABLED` | — (default) | 0 | Positions/orders through private WS |
| `BYBIT_ATR_TP_ENABLED` | — (default) | 1 | ATR-based TP |
| `BYBIT_PAPER_ENABLED` | — (default) | 0 | Paper Trading |
| `STRUCTURED_LOGGING` | — (default) | 0 | JSON logs in events.jsonl |

> Values were obtained from `~/.config/bybit-ws/env` on 25.10. `BYBIT_AB_ENABLED=1` and `BYBIT_DSPY_ENABLED=1`
> are in prod, not “planned” as previously stated; `BYBIT_ML_ENABLED`, conversely, is disabled.

## 14. Emergency Endpoints

All write endpoints require a Bearer token. Emergency endpoints additionally require `X-Emergency-Auth` (`EMERGENCY_TOKEN`):

```
# Kill Switch — full block until /resume
POST /kill_switch  (Bearer + X-Emergency-Auth)

# Emergency Close — close everything (or symbol)
POST /emergency_close  (Bearer + X-Emergency-Auth)
{"symbol": "LINKUSDT"}  // optional

# Reset CB
POST /circuit_breaker {"action":"reset"}
```

## 15. Glossary

| Term | Expansion |
|--------|------------|
| BB | Bollinger Bands — SMA(20) ± 2×STD |
| ATR | Average True Range — average volatility over 14 periods |
| MTF | Multi-Timeframe — D+W+M analysis |
| CB | Circuit Breaker — safety mechanism |
| DCA | Dollar Cost Averaging — averaging down |
| WR | Win Rate — % of profitable trades |
| PnL | Profit and Loss |
| SSOT | Single Source of Truth — state.db |
| JUNK | Junk mode — high-risk shorts |

## 16. Backtesting

The live tool is `bybit_ws/experiments/backtest_runner.py` (deterministic engine, candle cache,
run manifest) and `paper_trade.py`. **There are no ready-made “backtest results” in this table anymore:**
the previous “500+ trades / WR 58% / PF 1.8 / DD −12%” cannot be reproduced by any artifact in the repo —
the figures were removed to avoid presenting them as facts.

Reproduce honestly:
```bash
python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 180 --no-db
python3 -m bybit_ws.paper_trade SOLUSDT --days 90
```
Anti-overfit gate: `bybit_ws/experiments/validation.py` (DSR + PBO + permutation test).
