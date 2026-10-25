# Testing & Optimization

This page covers all test files, CI/CD, parameter optimization tools, and the deployment verification pipeline.

---

## Test Inventory

The project has **8 test files** with **~113 individual test functions/assertions** across multiple testing methodologies.

### Smoke Tests (`test_smoke.py`) — 16 tests

The primary integration test file. Focused on trailing SL, auto-SL, and state DB:

| Function | Tests |
|----------|-------|
| `test_long` | LONG trailing SL generates correct SL price |
| `test_short` | SHORT trailing SL generates correct SL price |
| `test_short_low_pnl` | SHORT with PnL < 15% → no trailing SL |
| `test_long_low_bb` | LONG with BB < 20 → no trailing SL |
| `test_short_high_bb` | SHORT with BB > 80 → no trailing SL |
| `test_manual` | Manual positions skipped by trailing SL |
| `test_sl_close` | SL near mark → no trailing SL |
| `test_sl_far` | SL far from mark → trailing SL updates |
| `test_tight_first_activation` | Tight trailing SL: first activation |
| `test_tight_below_threshold` | Tight trailing SL: below threshold |
| `test_tight_continue_trail` | Tight trailing SL: continues trailing |
| `test_tight_short_skip` | Tight trailing SL: SHORT positions skip |
| `test_tight_manual_skip` | Tight trailing SL: manual positions skip |
| `test_state_db` | StateDB CRUD operations |
| `test_auto_sl` | `_get_tiers` logic |
| `test_api` | API integration |

**Run:** `python test_smoke.py`

### Logic Integrity (`test_logic_integrity.py`) — 8 tests

AST-parses `main_async.py` to verify structural invariants:

| Test | Verifies |
|------|----------|
| `test_apply_functions_are_called` | All `apply_*` functions are called in main loop |
| `test_key_strategies_are_called` | All strategies from AGENTS.md are invoked |
| `test_entry_judge_in_package` | `entry_judge.py` exists in package |
| `test_sl_floor_exists` | SL floor protection exists |
| `test_trailing_sl_has_simple_mode` | Simple trailing SL mode |
| `test_concentration_check_exists` | Concentration limit check |
| `test_no_critical_errors_in_logs` | No ERROR in recent logs |
| `test_imports_vs_calls_summary` | Summary of imports vs calls |

**Critical:** This test is part of the `deploy.sh` pipeline. If it fails, deployment is blocked.

**Run:** `python test_logic_integrity.py`

### Regression Shield (`test_regression.py`) — 4 layers

Multi-layer regression prevention:

| Layer | What it checks | Scope |
|-------|---------------|-------|
| **L1** | `py_compile` all `.py` files | Syntax + scoping errors |
| **L2** | Import tests for 13 core modules | Module loading |
| **L3** | Logic smoke tests (mocked) | Entry judge, funding, mean_revert, bb_scalp |
| **L4** | `deploy.sh` dry-run syntax check | Deploy script integrity |
| **Phase 8** | `scan_unpack.py` double-unpacking bug check | `run_in_thread` safety |

**Run:** `python test_regression.py`

### Module Tests (`test_modules.py`) — 5 tests

| Test | What it tests |
|------|---------------|
| `test_health_drawdown` | `health.py` drawdown cooldown logic |
| `test_auto_sl_junk` | `auto_sl.py` JUNK skip logic |
| `test_pump_detect` | `pump_detect.py` data structure |
| `test_score_coin` | Full `score_coin()` function |
| `test_dashboard` | Dashboard file existence |

**Run:** `python test_modules.py`

### Multi-Timeframe Tests (`test_mtf.py`) — 15 tests (pytest)

- `_bb_signal` LONG/SHORT signals, boundary conditions, edge cases
- `check_confluence` real symbols, invalid symbols, multiple symbols
- `format_confluence` strong, filtered, none

**Run:** `pytest test_mtf.py`

### WebSocket Client Tests (`test_ws_client.py`) — 27 tests (unittest)

- BB cache: REST aliases, consumer expectations
- Stale detection: not connected, configurable threshold, zero last_update
- Batch size, kline subscriptions, orderbook args
- BB fallback to REST on WS failure
- Function existence in all WS-related modules
- Stats keys, position data, execution data, wallet data, orderbook data structure

**Run:** `python -m unittest test_ws_client.py`

### ML Smoke Tests (`test_ml_smoke.py`) — ~6 assertions

- Ensemble: `should_enter` return types, weighted_score, confidence range
- RL agent: `should_enter` (bool+str), `_dict_to_features` (shape), `predict` (action 0/1/2)
- HMAC: `_sign_file` + `_verify_file`

**Run:** `python test_ml_smoke.py`

### GridSignal Scanner Tests (`test_scanner_smoke.py`) — ~18 checks

- RSI: uptrend >65, downtrend <35, sideways 35-65, insufficient data → None
- BB: normal returns dict, pos 0-100, width > 0, flat market near 0
- Edge cases: None turnover, flat market scalp, missing keys, `score_short`

**Run:** `python test_scanner_smoke.py`

---

## CI/CD (`test.yml`)

GitHub Actions workflow in `.github/workflows/test.yml`:

```
on: push, pull_request
jobs:
  test:
    - Run test_logic_integrity.py
    - Run test_smoke.py
    - Run test_regression.py
    - Run test_ml_smoke.py
    - Run test_modules.py
    - Run test_scanner_smoke.py (GSC)
```

**Source:** `/.github/workflows/test.yml`

---

## Parameter Optimization

### Optuna Tuner (`optuna_tuner.py`)

Bayesian optimization over strategy parameters:

| Parameter | Range |
|-----------|-------|
| BB period | 10–50 |
| BB std multiplier | 1.5–3.0 |
| SL % | 2–10% |
| TP % | 5–30% |
| Min score | 10–40 |

**Objective:** Maximize (sum PnL × winrate)
**Data:** Bybit REST klines
**Output:** `~/.config/bybit-ws/optuna_params.json`
**Feature flag:** `BYBIT_OPTUNA_ENABLED`

### Walk-Forward Validation (`walk_forward_validate.py`)

Validates ML signals using TimeSeriesSplit:

- **Model:** RandomForestClassifier (100 trees, max_depth=5)
- **Split:** 5-fold walk-forward
- **Metrics:** F1, precision, recall
- **Min data:** 20 trades

**Source:** `/bybit_ws/walk_forward_validate.py`

### Grid Search Optimizer (`optimize_params.py`)

Exhaustive grid search over 4 parameters:

| Parameter | Values |
|-----------|--------|
| BB period | 10, 15, 20, 25, 30 |
| Entry BB threshold | 15, 20, 25, 30 |
| Entry discount | 0.95, 0.97, 0.98 |
| SL discount | 0.90, 0.93, 0.95 |

**Criterion:** max(win_rate × avg_pnl × trades)

### DSPy Optimizer (`dspy_optimizer.py`)

Prompt optimization using DSPy (BootstrapFewShot + MIPROv2):

- **Data:** Trade history from SQLite
- **Model storage:** `~/.local/share/bybit-ws/dspy_program/`
- **Integrity:** HMAC-signed models
- **Feature flag:** `BYBIT_DSPY_ENABLED`
- Results averaged/voted with RF ensemble

### Walk-Forward RF (`scripts/walkforward_rf.py`)

Standalone RandomForest walk-forward validation script for advanced analysis.

---

## Backtesting

Located in `/bybit_ws/backtest.py` (~16k) and `/backtest/` directory.

- Imports historical klines from Bybit
- Simulates entry/exit logic with configurable parameters
- Monte Carlo simulation (10K runs) for risk metrics
- Outputs: Sharpe, Sortino, Calmar ratios

---

## Deployment Pipeline Checklist

Before deploying a code change:

```bash
# 1. Run logic integrity (must pass)
python test_logic_integrity.py

# 2. Run smoke tests
python test_smoke.py

# 3. Run regression shield (all 4 layers)
python test_regression.py

# 4. Run ML smoke tests (if ML code changed)
python test_ml_smoke.py

# 5. Run module tests (if strategy code changed)
python test_modules.py

# 6. Deploy
bash deploy.sh
```

The `deploy.sh` script runs steps 1-3 automatically.

---

## Change Guidance

When adding a new module:
- Add it to `test_regression.py` L2 (import test) and L3 (logic smoke test)
- Add AST checks to `test_logic_integrity.py` if it must always be called from the main loop
- Add integration tests to `test_smoke.py` if it has non-trivial logic

When fixing a bug:
- Add a regression test to `test_smoke.py` or `test_regression.py` first
- The project has a history of subtle bugs: positions dict vs list, missing arguments, NoneType checks
- Check `git log` for similar recent fixes to understand the pattern

When tuning parameters:
- Use `optuna_tuner.py` for global parameter optimization
- Use `optimize_params.py` for per-symbol grid search
- Self-learning (canary mode) handles ongoing adaptation automatically
