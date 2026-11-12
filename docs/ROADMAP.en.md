# ROADMAP — bybit-ws

**English** · [Русский](ROADMAP.md)

> Development plan. Version: 11.5.x. Updated: **2026-11-12** (checked against code and the live DB:
> LSTM regression, REGIME gate, ms-P0 closed, test count, RPC route count).
> **Live backlog tracker — `ROADMAP.md` in the repo root** (score 8/10, T3/T4). This file — phases 8–13.

## Current status (v11.5.x)

| Component | Status |
|-----------|--------|
| Bollinger Grid LONG/SHORT | ✅ prod |
| SHORT optimization: World Model 33.1% + ML filter | ✅ prod |
| Android: server side (JWT + /set-tp + /generate-jwt) | ✅ prod |
| ATR-adaptive SL + ATR-based TP (3 levels) | ✅ prod |
| 7 entry filters (MTF, Orderbook, Volume, Entry Judge, Correlation, Post-trade, Risk) | ✅ prod |
| LSTM Market Regime (5 regimes; **dead**: `val_accuracy 0.653` in `models/lstm_regime_features.json`, only two values in 22 days `TRENDING_DOWN 41/42%` — stuck output; see `ROADMAP.md`) | ⚠️ needs retraining |
| LSTM World Model (multi-task OHLCV, entry scoring) | ✅ prod |
| REGIME_AUTO: regime gate (since 20.10 LONG is NOT blocked; `RANGING`/`CHOPPY` close both sides; threshold `BYBIT_REGIME_MIN_CONFIDENCE=60` with live conf <=42% = gate disabled) | ✅ prod (effectively neutral) |
| Adaptive TP/SL by LSTM regime (v9) | ✅ prod |
| Self-learning (event-driven: ≥10 trades and ≥6h since the previous run, otherwise fallback after 24h) | ✅ prod |
| Anti-ludomania: 3 losses/hour → 30-minute block | ✅ prod |
| BlackSwan v2: correlation alert (without auto-close) | ✅ prod |
| Symbol blacklist (permanent, auto_short + auto_entry) | ✅ prod |
| MTF candle cache (TTL 300s, reduces REST load) | ✅ prod |
| SHORT time exit by opened_at (not createdTime) | ✅ prod |
| Paper Trading (historical backtest) | ✅ prod |
| Web dashboard (SVG + HTML/Chart.js) | ✅ prod |
| MCP tools (10) | ✅ prod |
| RPC REST API (51 path handlers GET+POST, see `docs/API.md`) | ✅ prod |
| One-click trading through Hermes chat | ✅ prod |
| Alerts (ntfy + Telegram) | ✅ prod |
| Tests: 52 checks in `test_smoke.py` + **577** in `tests/` (`python3 -m pytest -q`, 12.11); CI runs only 5 scripts — full suite is outside the gate (T4 in `ROADMAP.md`) | ✅ prod |

## Roadmap

### Phase 8.0 — Documentation + Paper Trade + Dashboard ✅ (01-02.08.2026)

| Task | Status |
|--------|--------|
| Paper Trading on historical data | ✅ |
| Web dashboard (SVG + HTML/Chart.js) | ✅ |
| Product README + ONBOARDING | ✅ |
| Graphify-driven refactoring (import cycles) | ✅ |
| MIT → AGPL-3.0 license | ✅ |

### Phase 8.1 — LSTM World Model + Anti-ludomania ✅ (01-04.08.2026)

| Task | Status |
|--------|--------|
| LSTM World Model (multi-task OHLCV) | ✅ |
| World Model in LONG + SHORT scoring | ✅ |
| Fix exit_reason (SL/TP based on price movement) | ✅ |
| Anti-ludomania (3 losses → block) | ✅ |
| REGIME_AUTO: LSTM regime gate (LONG unblocked since 20.10) | ✅ |
| Adaptive TP/SL by regime (v9) | ✅ |
| BlackSwan v2 (alert only, no auto-close) | ✅ |
| MTF gap closed (block without D-TF data) | ✅ |
| SL floor: 2% → 5% for LONG | ✅ |
| Heavy cycle: parallelization (56→44s) | ✅ |

### Phase 8.2 — Systemd Hardening ✅ (04.08.2026)

| Task | Status |
|--------|--------|
| MemoryDenyWriteExecute vs PyTorch fix | ✅ |
| LSTM HMAC mismatch fix | ✅ |
| TimeoutStopSec=10 (restart hang) | ✅ |
| lstm_world_model symlink in bybit_ws/ | ✅ |
| MTF discount for TRENDING_DOWN (min_tfs=1) | ✅ |
| log_event fallback to stderr | ✅ |
| AGENTS.md: Systemd Pitfalls | ✅ |
| CHANGELOG v8.0-v8.2 | ✅ |

### Phase 9 — SHORT optimization ✅ (08.08.2026)

| Task | Priority | Status |
|--------|-----------|--------|
| SHORT in TRENDING_DOWN: parameter calibration | 🔴 | ✅ max_loss 15→10%, max_hold 48→24h |
| Time-based SL: close losing positions >12h | 🔴 | ✅ check_short_time_sl() in the main cycle |
| Exclude imported trades from self-learn | 🔴 | ✅ strategy != 'imported' + manual=0 (v11.4) |
| SHORT win rate: analysis + improvement | 🔴 | ✅ analysis: without STGUSDT +$146 |
| World Model quality > 33% | 🟡 | ✅ 22.3% → 33.1% (400 epochs, λ=0.01) |

### Phase 9.1 — Junk-short risk fix ✅ (15.08.2026)

| Task | Status |
|--------|--------|
| Junk shorts (Tier C/D) SL +7% (sl_tier_cd) via trading-stop | ✅ |
| DCA martingale removed (check_auto_short + check_junk_dca) | ✅ |
| Dead code removed (JUNK_DCA_LEVELS, short_margin, SHORT_LEVERAGE) | ✅ |
| exit_reason detection by closedPnl sign (not by price) | ✅ |

### Phase 9.2 — FIFO matching self-learn fix ✅ (20.08.2026)

| Task | Status |
|--------|--------|
| adapter.load_from_sqlite builds RoundTrip directly from pnl/hold_hours | ✅ |
| analyzer.compute_profile_from_roundtrips (without FIFO recalculation) | ✅ |
| WR 28% → 66% (matches direct SQL) | ✅ |
| Canary problem cascade (false WR 28%) eliminated | ✅ |

### Phase 9.3 — Time-exit + Blacklist + Candle-cache ✅ (27.08.2026)

| Task | Status |
|--------|--------|
| check_short_time_sl: createdTime → opened_at/entry_ts (pitfall #14) | ✅ |
| symbol_blacklist.py — permanent blacklist (auto_short + auto_entry) | ✅ |
| candle_cache.py — MTF candle TTL cache (300s), deadline 20→30s | ✅ |

### Phase 10 — Android application 🟡

| Task | Status |
|--------|--------|
| **Code (separate repo `bybit-ws-android`):** Kotlin + Compose + Hilt, MVVM, 18 .kt / 1124 lines | ✅ |
| 5 screens: Dashboard, Detail, Scan, Alerts, Settings | ✅ |
| Server side: JWT, nginx :4444→8766 + rate-limit, /set-tp, /generate-jwt | ✅ |
| Global Kill-Switch from the application | ✅ |
| **APK build (assembleDebug/Release)** | ⬜ not checked — Android SDK + Gradle required |
| Auto-refresh by timer | ⬜ |
| LONG scan (currently SHORT only) | ⬜ |
| PnL chart / widget / alert filtering | ⬜ |
| Installation on a phone | ⬜ |

### Multi-exchange 🟡

| Task | Priority | Status |
|--------|-----------|--------|
| ExchangeAdapter (ABC + Bybit/Binance/OKX adapters) | 🟡 | ✅ written (Phase 6.5) |
| Integration into main_async/auto_entry/auto_short | 🟡 | ⬜ connected only in api.py (get_bb) |

### Technical debt — cleanup 27.08.2026

| Task | Status |
|--------|--------|
| CI: workflow master→main + logic/regression tests | ✅ |
| Legacy debris: .bak ×2, deploy.sh.old, =6.0 | ✅ removed |
| Risk/reward SHORT: confirmed by Phase 9.1 (PF 0.59→2.36) | ✅ diagnosis |
| Duplicate modules: auto_sl↔unified_sl, main↔main_async (docker-entrypoint) | ✅ 25.09.2026 — dead imports removed, docker → main_async, modules @deprecated |

### Long term

| Task | Priority |
|--------|-----------|
| DQN → PPO | 🟢 |
| — Feature Store / Data Pipeline for RL | 📋 (pre-req) |
| Grafana HTTPS (domain required) | 🟢 |
| Cross-exchange funding spread (Bybit vs Binance/OKX) → signal in check_funding_signals | 🟡 |

### Phase 11 — external audit and execution layer (25.10.2026)

Source: strategic audit “how to turn the project into a marketable product” (ChatGPT, 1331 lines).
The audit was based on the README and part of the code, so **each item was checked against the repository**, and the wording
was run through Petr in `verify` mode (contract: verdict + quote + rationale).
**Petr's verdict: CONDITIONAL — 8 of 10 findings confirmed by verbatim quotes, GO after the top three are closed.**

#### P0 — money and risk

| # | Task | Evidence | Petr | Status |
|---|--------|----------------|------|--------|
| 1 | Fail-closed in LONG risk checks | `auto_entry.py:785` correlation — `except Exception: pass`, entry proceeds with `corr_sizer=1.0`; `:798` concentration — log only, no `continue`; `:810` chase guard and `:821` daily limit — same | CONFIRMED (HIGH) | ✅ `a6d79cf` |
| 2 | Correlation check for SHORT | `grep -n "corr" auto_short.py` → 0 lines; LONG is blocked at r>0.85, SHORT — never | CONFIRMED (HIGH) | 🟡 shadow in `a6d79cf`, enforce after measurement |
| 3 | Kill switch to completion: cancel-all + verification | `rpc.py:1970` sets pause and calls `emergency_close_all`, which closes positions with MARKET IOC, but does not cancel open limit orders, does not repeat `fetch_positions`, and has no escalation on `failed>0` | CONFIRMED (HIGH) | ✅ `aa9bea9` |
| 4 | `orderLinkId` at all entry points | 18 `/v5/order/create` calls in 16 files; `orderLinkId` only in `dca.py:193` and `recycle.py:108` → duplicate possible on timeout | CONFIRMED (HIGH) | ✅ `a6d79cf` |

#### P1 — evidence and observability

| # | Task | Evidence | Petr | Status |
|---|--------|----------------|------|--------|
| 5 | Costs in the backtest engine (fee/funding/slippage) | `bybit_ws/experiments/engine.py` (347 lines): 0 mentions of fee/funding/slippage/commission | CONFIRMED (HIGH) | ✅ `c525be8` |
| 6 | Trading readiness in `/health` (`TRADING_DISABLED`/`DEGRADED`) | `rpc.py:998` returns status/alive/uptime/cycle/paused; no readiness indicator | CONFIRMED (MEDIUM) | ✅ `e0bb539` |
| 7 | Decision journal in the DB (risk-check rejections) | rejections go only to `events.log` (`alerts.log_event`, rotating file); not in the DB | CONFIRMED (clarified: log exists, DB does not) | ✅ `e0bb539` · on 31.10 tests do not write to the journal: `BYBIT_LOG_SILENT` suppresses writes to the production DB (`tests/test_risk_journal_no_prod_writes.py`), 2862 lines of debris removed |
| 8 | Cluster risk: `shadow` → `enforce` | `check_cluster_risk` is called (`risk_manager.py:867`), but in `shadow` it always returns True. **Alexey's decision:** first measure how many entries it cut | CONFIRMED by me (Petr did not have enough context) | ⬜ · 30.10 measurement **invalid**: `risk_decisions` journal was populated by tests (2862 lines, 0 live). Gate from 31.10 (`168aa5f`) → journal clean, remeasure on live trading |

### Phase 11.2 — deep code audit (25.10, second pass)

The second review read the source files (`risk_manager.py`, `position_sizing.py`, `api.py`, `state_db.py`, `rpc.py`),
15 issues. Checked against the code and the live machine state, and run through Petr:
**9 of 10 CONFIRMED, 1 “cannot be checked” — checked independently, result was stronger than in the audit.**

#### Fix first (money)

| # | Defect | Evidence | Petr |
|---|--------|----------------|------|
| 11 | **Daily circuit breaker does not see realized loss** | `get_daily_pnl()` (`risk_manager.py:218`) reads `metrics.json['pnl_total']`, but there are no writers for this key: `grep -rn pnl_total` → 4 reads, 0 writes; the key exists in `metrics.json` for 0 of 130 days; the `trades.jsonl` block in the same function is `pass`. Measurement: today 41 records in `trades.jsonl`, Σ pnl = −$2.00, `get_daily_pnl()['realized_pnl']` = 0.0 | CONFIRMED |
| 12 | **Repeated POST without idempotency** | `api.py:146-215`: `for attempt in range(retries+1)` retries `session.post` on `TimeoutException`/HTTP error; `orderLinkId` only in `dca.py:193`, `recycle.py:108` out of 18 `/v5/order/create` calls | CONFIRMED |
| 13 | **Concentration check allows entry when it cannot check** | `risk_manager.py:114-124`: `if not wallet: return True, '', 0.0`; invalid `equity` and `equity<=0` also return `True` | CONFIRMED |
| 14 | **`suggest_qty` inflates the lot** | `position_sizing.py:264` `return max(qty, lot_step)`: when qty is below the step, the step is returned (margin $1, 10x, price $1000, step 0.1 → nominal $100 instead of $10). One production call: `mcp_server.py:427` | CONFIRMED |
| 15 | **Circuit breaker exists only in memory** | `risk_manager.py:600`: module global `_circuit_breaker_active`, no persistence (`risk_state.json` absent), resets automatically when the date changes. Service restart removes the block | CONFIRMED |

#### Real but secondary

| # | Defect | Clarification | Petr |
|---|--------|-----------|------|
| 16 | `atr_margin`: docstring promises `margin = risk_budget/(ATR*mult)`, actual code uses `margin = risk_per_trade` (`position_sizing.py:230`) | Not used in the production path: `use_atr=True` is never called, only MCP `place_entry`. Documentation misleading, no monetary loss | ✅ docstring aligned with code (`532c6cb`) |
| 17 | Sizing runs on fallback constants | `_ps_cfg()` → `{}` **on the live machine**: there is no `position_sizing` section in `config.yaml`, so `_FB_*` (0.20 / 5 positions / $5 floor / 0.40 cap) is not an emergency mode but the actual model. Deposit $70.68 → budget $14.14 → per position $2.83 → hits the $5 floor | ✅ `532c6cb` — `get_config()` instead of `Config()` for each parameter; `position_sizing` section in the live `config.yaml` (values = previous behavior); test `tests/test_position_sizing_config.py` (4) |
| 18 | `/rpc/dash/*` and `/dashboard` without Bearer | `rpc.py:545` PUBLIC_PATHS; `curl -sk https://127.0.0.1:8443/dash/rpc/dash/all` without a token returns positions with entry/mark/size/leverage/SL/upnl/liq. **But closed externally:** public IP `https://2.27.48.142:8443/` → connection refused (4444 returns 401). Risk is local/LAN, not the internet | CONFIRMED (I clarified the perimeter) |
| 19 | One sqlite connection `check_same_thread=False` without a lock | `state_db.py:155`; the only `Lock(` in the file is `asyncio.Lock` (different class). Both the asyncio loop and the RPC server thread (`HTTPServer`, `rpc.py:1981`) write | CONFIRMED |
| 20 | `trade_history` deduplication by `external_id` is not guaranteed | `CREATE TABLE` has `UNIQUE`, but `_migrate()` adds the column `ALTER TABLE ... ADD COLUMN external_id TEXT` without UNIQUE; no constraint in the live DB, in fact 1 duplicate (2 rows), 8 rows with NULL | CONFIRMED |

#### Status as of 25.10 (evening)

| Task | Result |
|--------|------|
| 11 realized PnL | ✅ `a6d79cf` — `SUM(pnl)` from `trade_history` for today; live measurement realized −1.66 across 41 closes |
| 12 orderLinkId | ✅ `a6d79cf` — generated once per call in `api.bybit()`, covers all 25 send points (inventory: `max-order-sites.md`) |
| 13 fail-closed | ✅ `a6d79cf` + `fed63f5` — root cause also found: balance parser searched for `coin=='USDT'` at account level, while coins are in `result.list[0]['coin']` → always `None`, causing the check to silently allow everything |
| 15 CB persistence | ✅ `a6d79cf` — `risk_state.json`, restored after restart |
| min-lot vs cap | ✅ `fed63f5` — at equity $62.8, 5% cap = $3.15 < `min_margin` $5; minimum lot always allowed, percentage applies to the amount above it |
| 5 (SHORT correlation) | 🟡 shadow mode: written to the log, entry not blocked — awaiting measurement |
| 3 (kill switch) | ✅ `aa9bea9` — cancel-all before closing, repeated fetch_positions+fetch_orders, STOP alert escalation on failed>0 or remaining orders. Tests `tests/test_kill_switch.py` (4). Live measurement before the fix: 22 open orders (16 TP, 5 SL, 1 LIMIT_ENTRY LTCUSDT) — all would have survived the old kill switch |
| 19 (sqlite lock) | ✅ `a5c945d` — `threading.RLock` in `StateDB`, 21 write methods serialized. Test `tests/test_state_db_lock.py` |
| 14 (suggest_qty) | ✅ `96ee1b2` — `qty < lot_step` → `0` instead of inflation; fail-closed guard in `mcp_server.py` |
| 20 (external_id UNIQUE) | ✅ `15ffcda` — partial UNIQUE index (WHERE external_id IS NOT NULL), 1 duplicate removed during migration. Test `tests/test_external_id_unique.py` (3) |
| 18 (dashboard Bearer) | ✅ `3ac9b27` — `/rpc/*` made private, `44af74e` restored `/rpc/dash/*` routes (were 404), regression `tests/test_rpc_dash_routing.py` |

Tests: 343 passed → after #3 (kill switch) **143 passed** → after #19+#14 **148 passed** → after #6+#7 **164 passed** → after #5 **167 passed** (health_ready 5 + risk_journal 8 + engine_fees 3).

#### 31.10.2026 — two incidents found in live data

Not from the audit: found while checking the external code review (`position_sizing`, `risk_manager`, `rpc`).

| # | What happened | Root cause | Fix |
|---|----------|--------|------|
| 21 | **Auto-trading stopped for 1.5 days** — 1788 lines `💸 Deposit $-5.6 < $30 — entries blocked` | deposit was read as the USDT coin's `walletBalance`; in UTA it becomes negative when funds are held in other coins (USDT −5.58 with equity $500.86). The same error existed in `risk_manager._get_wallet_balance` (coin equity −2.94 → concentration fail-closed) and in the funds-check in `auto_entry`/`auto_short`/`dca` | `71efc0b` — `api.account_wallet()` (`totalWalletBalance`/`totalEquity`/`totalAvailableBalance`) as the sole source; WS wallet cache removed (it is coin-specific). Tests: `test_deposit_uses_account_total_not_usdt_coin`, `test_wallet_balance_uses_account_totals` |
| 22 | **`risk_decisions` journal was 100% garbage** — 2862 lines, 0 live | tests ran `check()` on fixtures (BTCUSDT/FOOUSDT/AAAUSDT) and wrote through singleton `state_db.db` to the production DB. As a result, the 30.10 shadow-mode measurement recorded “22 triggers” = pytest runs | `168aa5f` — `_log_decision` does not write to the DB in the production path when `BYBIT_LOG_SILENT=1` (an injected temporary DB is written); `conftest.py` sets the flag before import; test `tests/test_risk_journal_no_prod_writes.py`. Journal cleaned, backup `~/.hermes/backups/bybit-ws-state-20261031-091933.db` |

Consequence for #8/#5: until the journal accumulates live rows, there is no basis for enabling `enforce` — measure through `risk_decisions` after the gate.

### Petr backlog review (31.10.2026)

Run through `petr.py -f` (`verify` mode: Verifier + Missed-item Finder + Advocate + lead).
Verdict: **CONDITIONAL**. The changes below include verification of every statement against the code and DB.

#### First the numbers, because Petr's brief contained outdated values

The brief was written “from memory” (PF 0.75) — actual PF is 0.96, and the “loss” side depends on the window.
Command (live measurement 31.10):

```bash
sqlite3 ~/.local/share/bybit-ws/state.db "
SELECT CASE WHEN side='Sell' THEN 'LONG' ELSE 'SHORT' END s, count(*) n,
       round(sum(CASE WHEN pnl>0 THEN pnl ELSE 0 END),2) gross_win,
       round(sum(CASE WHEN pnl<0 THEN pnl ELSE 0 END),2) gross_loss,
       round(1.0*sum(CASE WHEN pnl>0 THEN pnl ELSE 0 END)/-sum(CASE WHEN pnl<0 THEN pnl ELSE -1e-9 END),2) pf
FROM trade_history WHERE strategy='auto' AND COALESCE(manual,0)=0 GROUP BY s;"
```

| window | side | n | PnL | PF |
|---|---|---|---|---|
| all time | LONG | 248 | +$35.75 | 1.05 |
| all time | SHORT | 361 | −$86.09 | 0.79 |
| 30 days | LONG | 93 | **−$273.81** | — |
| 30 days | SHORT | 280 | **+$1.06** | — |
| 7 days | LONG | 35 | −$160.20 | — |
| 7 days | SHORT | 116 | +$6.90 | — |

Top 5 losses over 30 days are all LONG: ZKUSDT −122.58, DOTUSDT −73.99, AKEUSDT −42.80, SEIUSDT −36.65, LTCUSDT −33.74 (total −$309.7).
**Consequence:** “SHORT is unprofitable” was historically true and is already false in the current window; the loss comes from 5 LONG symbols, not the side (matches the 19.10 conclusion: fix with a blacklist and time-SL, not by disabling the side).

#### Verdicts

| Item | Petr | Fact check | Result |
|---|---|---|---|
| #8 cluster risk → enforce | defer until 2 weeks of clean journal | journal clean since 31.10 09:19 (`168aa5f`), 0 live rows | ⏳ **2-week measurement**, enforce based on numbers only |
| #5 SHORT correlation | same | shadow log exists (`auto_short.py:652-661`: `🔗 CORR SHORT (shadow) … would block entry`), no blocking — `continue` absent | ⏳ same measurement |
| #3 ATR clamp 10% | **do first** | `unified_sl.py:247`; 235 of ~250 log records = exactly `ATR SL 10.0%`; 43% of ATR/price snapshots > 10% | ⚠️ measured 31.10: cap is not the root cause (raising to 15% did not improve results) → defaults kept, moved to `sl.*` config |
| #4 CI lint/types | fix pyright config, do not chase types | `pyright` is not installed as a module; 100+ messages are unresolved relative imports through `bybit_ws/*.py` symlinks | ⏳ after #3, zero effect on money |
| #5-new backtest artifact | do it (before/after tool) | `bybit_ws/experiments/engine.py` already calculates maker fee on entry and funding from `funding_log` (`c525be8`) | ✅ do first — #3 cannot be measured without it |
| #6 AGPL-3.0 | outside code backlog | `LICENSE`/`pyproject.toml` — legal decision | ✂️ **remove from code backlog** |
| #9 execution layer | defer until stabilization | weeks of work, touches the live path | ⏳ defer |
| Android | reduce, except `/set_tp` | server side exists: `rpc.py:699` `/set-tp` | ✂️ cross out 5 of 6 |
| Multi-exchange | cross out | failure surface without benefit | ✂️ cross out |
| Item 10 sizing `atr_margin` | do together with #3 | manual MCP `place_entry` path only (`mcp_server.py:410`); automated entries use `calculate_margin` | ✅ 31.10: risk-constant `margin = risk/(stop%×leverage)`, leverage passed through (was hardcoded 10x), `tests/test_atr_sizing.py` |

#### Add (Petr; verified)

1. **Aggregate exposure limit (manual + automated).** Fact on 31.10: 6 manual positions = $3967 notional at equity $501 (7.9x), automated entries unlocked and stack on top. Cluster risk (#8) is incomplete without this. Check in the risk path + test the “7.9x” scenario — do it.
2. **“0 entries with a live signal” watchdog.** Incident on 31.10 — silent block for 1.5 days. `no_agent` cron: if a signal exists for N cycles and 0 entries occur — alert with the reason. ~1 hour of work — do it.
3. ~~Stop-edge / disable auto-SHORT~~ — **rejected by facts**: 30-day SHORT +$1.06 across 280 trades, losses come from 5 LONG symbols. The 19.10 decision (symbol blacklist + time-SL) remains.
4. **MCP research tools (Item 1).** ✅ 31.10: `backtest_run`, `experiment_list`, `validation_report` — read-only, subprocess with lock and 180s timeout, SQLite `file:...?mode=ro`. Tests `tests/test_mcp_experiments.py`.
5. **Correlation shadow (Item 2).** ✅ 31.10: `check_correlation_shadow` in `position_sizing.py` — logs “would block”, but returns `True` (shadow-only, does not block entry). Fail-open on errors. Tests `tests/test_correlation_shadow.py`.
6. **Parity script (Item 3).** ✅ 31.10: `scripts_parity.py` — diagnostic script comparing live vs backtest entries (±15 min tolerance), report in `~/.local/share/bybit-ws/parity_reports/`. Tests `tests/test_parity_diff.py`.

#### Two-week order

| # | Work | Done when |
|---|---|---|
| 1 | Backtest artifact: commit SHA, config hash, data version, fee/funding/slippage, equity curve | ✅ 31.10: `--out` + 4 tests, repeated SOLUSDT 180d run returns the same `equity_hash` |
| 2 | #3 ATR clamp + #10 sizing | ⚠️ #3: measured, cap is not the root cause — defaults kept, controls in config; ✅ #10: risk-constant sizing + leverage from request |
| 3 | Silent entry-block watchdog | ✅ 31.10: `watchdog.py` + cron `a954882a4f59` (hourly, no_agent), 7 tests, live run catches the deposit gate |
| 4 | Aggregate exposure limit (manual+automated) | test the 7.9x scenario, rejection written to `risk_decisions` |
| 5 | Two weeks of clean journal → decision on #8/#5 | decision distribution without pytest rows, threshold proposed based on numbers |

#### 31.10.2026 measurement: ATR stop and sizing (#3, #10)

Run on the artifact (`backtest_runner`, 180d window through 2026-10-30, 8 symbols,
$1000 deposit per symbol, 5% risk/trade; costs — maker entry, taker exit, funding):

| variant | PnL | trades |
|---|---|---|
| A fixed 10% stop (fallback) | **−$774** | 230 |
| B ATR 1.5×, clamp 3–10% (as in live code) | **+$50** | 109 |
| C ATR 1.5×, clamp 3–15% | −$132 | 103 |
| D ATR 2.0×, clamp 3–15% | +$200 | 69 |

**Conclusion that had to be accepted:** the 10% cap is not the root cause. It triggers
(43% of ATR/price snapshots hit 10%, observed maximum — 12.5%), but raising
the cap to 15% did not improve results (C is worse than B), and the A→B difference is not “ATR coming alive”
but a lower number of trades.

Stability check (same symbols, two 90-day halves):

| window | B 1.5× | D 2.0× |
|---|---|---|
| through 2026-07-30 | −$289 | −$289 |
| through 2026-10-30 | −$415 | −$377 |

**Both halves are negative, 1.5× and 2.0× are indistinguishable** → D's advantage over the full window
is a single-run artifact, not an edge. Therefore **defaults were not changed**: 1.5× and clamp 3–10%
remain, but were moved to config (`sl.atr_multiplier` / `sl.min_pct` / `sl.max_pct`) —
they can be adjusted without code changes, based on measurement.

Command (reproduce):

```bash
python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 180 \
  --end-date 2026-10-30 --no-db --out /tmp/sol.json
# variants — through params.json: {"sl_atr_mult": 1.5, "sl_clamp": [0.03, 0.10]}
```

What was actually fixed (#10): `atr_margin(price=…, leverage=…)` calculates
`margin = risk / (stop% × leverage)` — trade risk in $ is constant (previously margin
was fixed and risk varied ×3.3 with ATR). `mcp_server.place_entry` passes the price and
**requested** leverage (previously `suggest_qty` was calculated with hardcoded 10x at any leverage —
the position was 3.3 times larger at leverage=3). Tests: `tests/test_atr_sizing.py`.

#### Aggregate exposure (31.10) — ⚠️ threshold exists, block not enabled

Live measurement: 6 positions, Σ|notional| **$3925** at equity **$453** → **8.7x**.
`risk.max_total_margin` ($300) measures only margin (positionIM); nobody checked notional:
75x LTC ($1974 = 436% of equity) and 75x ARB still passed as “normal”.

Done:
- `margin_alerts.get_gross_stats()` → `gross`, `equity`, `gross_leverage` in `/risk` alerts;
- alert in `check_margin_utilization` (already sent to Telegram through `main_async`):
  🆘 at `gross ≥ risk.max_gross_leverage` (5.0x), ⚠️ at 80%; cooldown 4h
  (exposure remains for hours, a 15-minute alert would mean 96 messages/day);
- `risk.max_gross_leverage` in `config.py` + `config.yaml`, test `tests/test_exposure_alert.py`.

Block NOT enabled and why: any threshold below 8.7x currently stops all automated entries
— repeating the 31.10 incident (silent inactivity). Manual and automated positions cannot be separated:
`pumps.json` is **empty**, `is_manual_position('LTCUSDT'/'ARBUSDT') == False`,
although 75x positions are manual entries. Until attribution is fixed, the correct step is visibility;
enable the block for automated positions after `mark_manual_position` is called by all
manual paths (`/enter`, MCP `place_entry`, raw entries).

#### Entry attribution: bot vs human (31.10)

The reliable indicator is the opening order's `orderLinkId`. The bot marks its orders
with prefixes: `ae_` (auto_entry), `as_` (auto_short), `re_` (sl_reentry), `dca_`, `bb_`
(scalping), `mr_` (mean-revert), `fe_` (funding), `pd_` (pump/junk). Empty link —
entry from the application: our marker cannot physically appear there, and no other indicator
can replace it.

`createType` is NOT suitable for distinction: Bybit returns `CreateByUser` for our
API orders as well (checked on JCTUSDT: link `h…`, price matched to 0.000%). Before 31.10
`bb_scalp`/`mean_revert`/`funding_entry`/`pump_detect` placed orders without a prefix and
received the default `h…` from `api.py` — the same as `/enter`; therefore 21 trades over
7 days remain indistinguishable, and the heuristic below is used for them.

Order-to-trade matching is by entry price (0.2% tolerance) and qty; time is the tie-breaker.
Do not filter by side: in closed-pnl, `side` is the closing side, and on JCT “Sell”
turned out to be the entry. Matching by side+time produced false “manual” matches
for 38 IRYS trades — entry price is the only unambiguous anchor.

Heuristic for trades older than 7 days (no order history): empty link — fact;
otherwise leverage must be outside the automated set {3, 10} **and** a second indicator from your triggers —
no `entry_diag` (no analysis) or no judge decision near the entry (the judge
is stored only in `events.log`, with a 10-minute cache index).

Live cycle: a position with an empty link automatically receives a manual marker (`🖐` in the log) —
otherwise `auto_sl`/`auto_tp` move SL/TP for your manual entries. Positions with `h…` are not marked:
before 31.10 an automated entry looked the same, and taking control of its position away from the bot is worse.

Seven days of history were reclassified once (`python3 scripts_origin_audit.py --apply`):
131 trades became `manual=1`, 14 automated trades remained (3x/10x leverage, all with diagnostics). Result:
self-learn no longer learns from your trades — previously it learned almost exclusively from them.

#### Manual markers survive closing (31.10)

Four positions liquidated on 30.10 were recorded as `manual=0`: the cycle cleans
`pumps.json` BEFORE importing `closed-pnl`, and the marker lived only while the position was open.
Removed: on close, the marker receives `closed_at` and lives for 48h; `is_manual_position(sym,
include_closed=True)` is an import question, live modules (`auto_sl`/`auto_tp`/`reporting`)
do not see the closed marker; an automated entry in the same pair removes the closing marker, and it is
deleted after 48h. The four 30.10 liquidations were retroactively marked `manual=1` (−$138.71
excluded from self-learn: the adapter loads `manual=0`). Test:
`tests/test_manual_attribution.py`.

#### Liquidations are visible and loud (31.10)

On 30.10 at 00:18:45 UTC, four positions closed in one second (DOT −78.2, LTC −27.36,
NEAR −17.14, MNT −16.01 = −$138.7) and entered the database as `exit_reason='Unknown'`: our
classifier checked only proximity to our SL/TP, while the exchange returned
`execType=BustTrade` — forced closure due to margin on cross-collateral
(loss on DOT $78.2 with position margin $28.7 — greater than the margin).

Fix: `classify_exit()` accepts `execType` and decides before price — `BustTrade`/
`Liquidation`/`PartialLiquidation` → `LIQ`, `AdlTrade` → `ADL`. For `LIQ`/`ADL`,
`💀` is written to `events.log` and a STOP alert is sent to Telegram. History fixed:
4 rows `Unknown → LIQ` by matching `external_id` with `closed-pnl`. 11 `Unknown`
remain over 3 days at −$3.2 — these are normal market exits.

`Unknown` now means exactly what it says: neither a level nor `execType` — a reason
to investigate, not a dumping ground for liquidations.

#### Entry locks: stop series and pair pause (31.10)

Problem: after a stop on a pair, the bot enters again — with the same signal, into the same pair,
and across the entire market. Locks are time-limited, survive restarts, and always write the reason
to the log (silent blocking = the 31.10 incident).

Rules (`protections` in config): ≥6 SL exits over 12h → global lock for 6h;
SL on a pair → pause that pair for 4h. The gate point is `margin_for_strategy()` (all
automated entries go through it), so strategies are not duplicated.

Measurement on 756 closed trades (`state.db`):

| variant | trades taken | PnL |
|---|---|---|
| without locks (actual) | 756 | −$573 |
| 6 SL/12h → 6h + 4h pair pause | 481 | **−$8** |
| 4 SL/12h + 4h pause | 460 | +$19 |
| 8 SL/12h + 4h pause | 485 | −$19 |
| 6 SL/12h + 2h pause | 488 | −$6 |

The gain does not depend on the exact settings (4…8 SL, 2…6h pause → −$30…+$19), so
defaults were selected in the middle. The “pair without edge” rule (≥8 trades, PF<0.8 over 7d)
was measured separately: it skips 140 trades for −$66 — does not pay off and was not added to the code.

Tests: `tests/test_protections.py` (10, including a gate through the
`BYBIT_PROTECTIONS_STATE` state file). Live state: `~/.local/share/bybit-ws/protections.json`
is recalculated every cycle; new locks are written to `events.log` as `🛑`.

#### Work order (updated after the second audit)

1. **11** — realized PnL from `trades.jsonl` (each record contains `pnl`) or from `state.db`; remove `pass`. Half an hour.
2. **13** — fail-closed in `check_symbol_concentration` + `continue` in `auto_entry` on exception. Half an hour.
3. **12** — `orderLinkId` at all entry points (shared helper) + retry POST only when the key exists. Half a day.
4. **15** — persist `_circuit_breaker_active` in `risk_state.json` (atomic write already exists in `_save_risk_state`). One hour.
5. **14**, **18**, **19**, **20** — one task at a time, after P0.

#### P2 — product and handover

| # | Task | Status |
|---|--------|--------|
| 9 | Unified execution layer (OrderIntent → RiskDecision → Executor → OrderRepository) — weeks of refactoring, touches live, only after P0 and a paper run | ⬜ |
| 10 | CI: add lint/types to the current py_compile + smoke + modules + logic + regression | ⬜ |
| 11 | Backtest report: commit SHA, config hash, data version, fees, equity curve | ✅ `backtest_runner --out` writes an artifact: `git_commit`, `engine_version`, `params_hash`, `config_hash` (sha256 of production config.yaml), candle `data_hash`, `fees_model`/`funding_model`, metrics with `total_fees`/`total_funding`, `equity_curve` + `equity_hash`; two runs → identical `equity_hash` (`tests/test_backtest_artifact.py`). Baseline artifact before the ATR fix: `~/.local/share/bybit-ws/experiments/runs/baseline_solusdt_180d.json` (17 trades, PF 0.43, PnL −33.0%, fees $18.31, funding $0.05) |
| 12 | License: repository under **AGPL-3.0** (`LICENSE`, `pyproject.toml`). License change does not cancel rights to published versions — legal matter, resolve before commercial packaging | ⬜ |

#### Removed from the audit (checked, not included)

- **“Do not give the AI agent direct access to trading”** — already implemented: write endpoints require Bearer, emergency
  (`/kill_switch`, `/emergency_close`) require **Bearer + X-Emergency-Auth**, `/enter` requires `confirm:true`.
- **“`emergency_close_all` fails when `positions=None`”** — incorrect: the function calls `fetch_positions()` itself
  and returns an error on failure (`risk_manager.py:1046`).
- **Unified config / model registry / SBOM / onboarding / doctor** — product wrappers, backlog items.
- **New ML mechanisms and strategies** — audit is against them, agreed.

### Phase 12 — self-learning: from 20 controls to one regulator (31.10.2026)

External audit of the self-learning mechanism (Manus, task `m5FdqqgbDPD5Gg5xa48t7h`), numbers recalculated
manually. The task is not to “write something new” but to **remove excess**: currently 20+ mechanisms compete
for the same signal, and each votes on a sample that is too small for voting.

Live numbers on 31.10 (command: `sqlite3 ~/.local/share/bybit-ws/state.db "SELECT
count(*) FROM trade_history WHERE manual=0"`; versions — `ls ~/.local/share/bybit-ws/params_history/ | wc -l`):
594 automated trades, 162 manual exclusions, **94 parameter versions**, 10 symbols × 6 regimes → **~10 trades per cell**.

#### Where overfitting occurs (arithmetic, not opinion)

- 594 / 10 symbols ≈ 59 per symbol, / 6 regimes ≈ **10 per regime** — exactly the trigger threshold
  “≥10 trades in 6h” (`main_async.should_run_self_learn_v6`). The trigger allows training on the size of one cell.
- Within a cell, the data is further divided: 6 bandits × per-regime × symbol profiles.
- `composite score` = WR+PF+Sharpe+DD+Hold with adaptive weights — five objectives fitted to the same data.
- `exponential decay` + micro-updates (after each trade) reduce the effective sample size;
  `auto-prune` + canary promotion = multiple testing (repeated winner selection).
- `causal inference` and 1000-simulation Pareto MC create false precision with an unknown distribution.

**The canary cannot distinguish anything** (recalculation): with a window of 10 trades (about 5 per group) and baseline WR 50%,
`SE(Δ) = √(2·0.25/5) = 0.316`, `MDE(80%, α=5%) = 2.802·0.316 = 88.6 pp.` Manus wrote 81 pp. — same conclusion.
For comparison: 25/group → 39.6 pp., 100 → 19.8, 400 → 9.9. Thus distinguishing a 10 pp. WR difference requires
about 400 trades per group. Current value — 5.

#### What to do (in order)

| # | Work | Done when | Status |
|---|--------|--------|--------|
| 1 | **`N_eff` in the log and training threshold.** `N_eff=(Σw)²/Σw²` by arm/regime/symbol; do not train at `N_eff<100`; do not calculate Sharpe/PF with fewer than 30 trades | `self_learn` writes `N_eff` to JSONL and skips the update below the threshold; test that “insufficient data → parameter unchanged” | ⬜ |
| 2 | **Kill micro-updates and promotion after 10 trades.** Batch retraining; canary is `log-only` (“would promote”) until the window provides MDE ≤ target | promotion requires an MDE threshold, not a trade-count threshold; log shows what would be promoted | ⬜ |
| 3 | **Parameter change rule:** bootstrap 95% CI of effect does not cover 0 **and** OOS effect in 2 consecutive windows | synthetic test: noise parameter is not accepted, real shift is accepted | ⬜ |
| 4 | **Collapse duplicates** (7 pairs): 4 bandit mechanisms (TS / Dynamic Bandit / Ensemble / uncertainty-aware) → one TS with uncertainty; regime detector ↔ drift ↔ handover → detector + cooldown; per-regime params ↔ symbol profiles → one hierarchical profile; composite ↔ adaptive weights ↔ decay → fixed score, decay for monitoring only; IQR ↔ 3σ ↔ robust → MAD; micro-updates ↔ retrain ↔ canary → batch + canary | in `self_learn.py`, mechanisms decrease rather than increase; each removed mechanism confirmed by `grep -c` = 0 production-path calls | ⬜ |
| 5 | **Remove `causal inference` and Pareto MC as “protection”**, keep walk-forward + stress test as a separate report | stress test does not affect parameters, only raises alerts | ⬜ |
| 6 | **Keep versioning and JSONL** — they do not train, but provide rollback | — | ✅ already exists (94 versions) |

**Order and risk:** items 1–3 change training behavior → live path, perform only on Alexey's instruction,
with a backup and before/after measurement on a backtest artifact (`backtest_runner --out`, `equity_hash`).
Item 4 is code removal: the diff must be negative, and every removed call is checked with `grep`.

**Do NOT:** add new ML mechanisms “for symmetry” or increase retraining frequency —
the audit is explicitly about the opposite.

### Phase 13 — second external audit (31.10.2026): code review

The audit was submitted as text (not from the repository), checked line by line against the code and live DB.
The purpose of this section is to record what is true, so it does not need to be reread,
and not to add disproved items to the backlog.

**Confirmed:** `main.py` is indeed dead (`main.py:3` → `sys.exit`); WAL and
`busy_timeout=5000` are present; ZEC details match the DB to the cent (80.6h `Time` −$68.80,
then SL −$40.19 / −$25.06 / −$23.94); there is no sample for 10x strategies — the DB has 8 trades
(`x10` 6 trades +$5.60 PF 1.29, `x10:scalp` 2 trades −$1.73); bus factor is effectively 1.

**Disproved (do not add to backlog):**

| Audit statement | Fact |
|---|---|
| License “MIT or undefined” | `LICENSE` = **AGPL-3.0**, `pyproject.toml` = AGPL-3.0. The m8ven catalog is wrong; the audit trusted the catalog instead of the file |
| Limits: $50 per day / $300 margin / $100 position | `risk.max_daily_loss: 200`, `risk.max_gross_leverage: 5.0`, 12 `banned_symbols`, `max_position_share: 0.40`. No $50/$300/$100 |
| LONG PF 9.64 WR 90% +$240 · SHORT PF 0.70 WR 67% | Outdated snapshot. Live: all-time LONG 248 +$35.75 PF 1.05, SHORT 361 −$86.09 PF 0.79; 30 days LONG 91 −$280.47, SHORT 280 +$1.06 |
| ZEC −$158, USELESS −$64, AKE −$367 | −$43.84 / −$23.15 / −$409.34; AKE is already in `banned_symbols` |
| manual −$201 vs auto +$210 | Manual 162 −$558.59; automated 594 −$14.78 (PF 0.99); clean `strategy='auto'` 478 +$97.86 (PF 1.11, WR 70.1); the pit is `imported` 30 −$242.88 |
| “7 MCP tools” | 10 (on 31.10, `backtest_run`, `experiment_list`, `validation_report` were added) |
| “8 tables, no migrations” | 15 tables; `_migrate()` is idempotent, Alembic intentionally absent |
| Estimate $40K–$600K | not verifiable, outside the code |

#### Tasks

| # | Work | Done when | Status |
|---|--------|--------|--------|
| 1 | ✅ **P0: position age in ms** — both time exits were dead code. Fixed: `time_exit.normalize_open_ts()` (ms→s) is applied in `time_exit.py:37,71` and `auto_short.py:936,955,1013`; tests `tests/test_time_exit_ms.py` (8 passed). | done | ✅ 12.11 |
| 2 | ✅ **Report on 10x strategies** — `docs/REPORT-10x.md`: 8 trades (16–17.06), +$3.87, TP/SL 4/4, liquidations 0, leverage and hold = NULL (telemetry appeared later). Conclusion: no sample, PF cannot be calculated | done | ✅ 31.10 |

**Why P0 was not done immediately:** the fix changes live behavior — the bot will start closing
positions older than 6h at a loss and any positions older than 24h. A backup, “what will close” measurement,
and Alexey's instruction are required.

## Phase history

| Phase | Status | Date |
|------|--------|------|
| Phase 1 — Baseline | ✅ | 07.06.2026 |
| Phase 2 — SQLite + RPC | ✅ | 16.06.2026 |
| Phase 3 — ML scoring | ✅ | 17.06.2026 |
| Phase 4 — Alerts + Dashboard + MTF | ✅ | 18.06.2026 |
| Phase 5 — DSPy + A/B + LSTM + RL | ✅ | 18.06.2026 |
| Phase 6 — Full WebSocket + Risk + Push | ✅ | 21.06.2026 |
| Phase 7 — Graceful shutdown + Heavy cycle opt + Kelly | ✅ | 27.06.2026 |
| Phase 7.1 — Stabilization | ✅ | 28.06.2026 |
| Phase 8.0 — Documentation + Paper Trade + Dashboard | ✅ | 02.08.2026 |
| Phase 8.1 — LSTM World Model + Anti-ludomania | ✅ | 04.08.2026 |
| Phase 8.2 — Systemd Hardening | ✅ | 04.08.2026 |
| Phase 9 — SHORT optimization | ✅ | 08.08.2026 |
| Phase 9.1 — Junk-short risk fix | ✅ | 15.08.2026 |
| Phase 9.2 — FIFO matching fix | ✅ | 20.08.2026 |
| Phase 9.3 — Time-exit + Blacklist + Candle-cache | ✅ | 27.08.2026 |
| Phase 10 — Android application | 🟡 server side in prod, APK not built | — |
| Phase 12 — self-learning: mechanism reduction | ⬜ plan (6 tasks) | 31.10.2026 |
| Phase 13 — second external audit: review + P0 time-exit | ⬜ P0 open | 31.10.2026 |
| Phase 11 — external audit, execution layer | 🟡 P0/P1 closed (#1,3,4,5,6,7,11–15,19,20). Open: #2 SHORT correlation (shadow → enforce after measurement), #8 cluster risk (awaiting measurement), #18 dashboard without Bearer, #9 execution layer, #10 CI lint/types, P2 “backtest report” (SHA/config hash/equity curve) | 25.10.2026 |
| Phase 11.2 — deep code audit (9/10 confirmed) | ✅ P0 11–15 closed (`a6d79cf`, `fed63f5`, `a5c945d`, `96ee1b2`, `15ffcda`); #16 `atr_margin` — docstring aligned with code; #17 — `532c6cb` | 25.10.2026 |

## Principles

1. **Protection first, attack second** — limits and CB are more important than strategies
2. **Tests before deployment** — 16 files / 171 tests (16 in test_smoke.py)
3. **Documentation kept current** — docs/ always matches the code
