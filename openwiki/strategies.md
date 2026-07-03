# Strategies & Trading Domain

This page documents all trading strategies, the entry pipeline, and the supporting systems that make real-time trading decisions.

---

## Tier System (Coin Classification)

Symbols are classified into tiers for risk-based position sizing and entry thresholds. Defined in `config.example.yaml` under the `tiers` section.

| Tier | BB Threshold (LONG) | SHORT Allowed | Characteristics |
|------|--------------------|---------------|-----------------|
| **S** | Custom (lowest) | Yes | High-cap, low-risk (BTC, ETH) |
| **A** | <15% | Yes | Major altcoins |
| **B** | <25% | Yes | Mid-cap altcoins |
| **C** | <40% | Yes (JUNK only) | Lower liquidity |
| **D** | <65% | Yes (JUNK only) | Low-cap, high risk |
| **one_way** | N/A | **No** | No-short exceptions |

**Source:** `/bybit_ws/config.py` and `config.example.yaml` (tiers section)

---

## LONG Strategy (Bollinger Grid)

The primary strategy. Buys at Lower Bollinger Band with 3x leverage, targets Middle and Upper Bands.

### Entry Conditions

Triggered in the **heavy cycle** (every 5 min) via `auto_entry_scan()`:

1. BB Daily % < tier threshold (S: custom, A: 15%, B: 25%, C: 40%, D: 65%)
2. Score ≥ minimum score (configurable per tier)
3. Score formula: BB position (0-40) + MTF confluence (0-20) + Volume (0-15) + Orderbook (0-15) + Regime bonus (0-10) = **0-100**
4. 7-filter pipeline must pass (see Architecture page)

### Position Parameters

| Parameter | Value | Source |
|-----------|-------|--------|
| Leverage | 3x | `strategy.long.leverage` |
| Margin | Dynamic: deposit × 20% / max_pos × score_mult | `position_sizing.py` |
| Max positions | 12 (configurable) | `strategy.long.max_positions` |
| Cooldown after SL | Configurable hours | `strategy.long.cooldown_after_sl_hours` |
| Cooldown after TP | Configurable hours | `strategy.long.cooldown_after_tp_hours` |

### Take Profit (auto_tp.py)

**ATR-based TP with 3 levels** (v7.0+):
| Level | Volume | Target |
|-------|--------|--------|
| 1.0× ATR | 40% | 1.0× ATR from entry |
| 2.0× ATR | 35% | 2.0× ATR from entry |
| 3.0× ATR | 25% | 3.0× ATR from entry |

Legacy split TP (pre-v7.0): 20% at Middle BB + 80% at Upper BB (limit reduceOnly orders).

**Source:** `/bybit_ws/auto_tp.py` (~12k)

### Stop Loss (auto_sl.py)

**ATR-adaptive SL v2** — checks every 30s:
- `SL = entry_price - (k × ATR(14))` for LONG
- `k` varies by volatility regime: 1.3 (high_vol) to 2.5 (low_vol)
- Capped at ±50% to prevent extreme values
- Floor protection: never below 2% from entry
- SHORT SL: tier-based (+5% A/B, +7% C/D), corrected if SL > entry price

**Volatility regimes** (from `regime.py` + `lstm_regime.py`):
| Regime | k | When |
|--------|---|------|
| High Volatility | 1.3 | High ATR |
| Trending | 1.5 | Strong trend |
| Normal | 2.0 | Default |
| Low Volatility | 2.5 | BB squeeze |

**Source:** `/bybit_ws/auto_sl.py` (~15k), `/bybit_ws/regime.py` (~11k)

### Trailing SL (trailing_sl.py)

Updates SL as price moves favorably:
- **LONG activation**: BB Weekly > 75% AND profit > 15%
- **SHORT activation**: BB Weekly < 25% AND profit > 15%
- SL trails at a distance based on volatility
- **Tight trailing** (v7.7): more aggressive trailing for better capture
- Manual positions are skipped

**Source:** `/bybit_ws/trailing_sl.py` (~12k)

### DCA (dca.py)

Dollar-Cost Averaging for LONG positions when price drops:

| Level | Drop from Entry | Position Increase |
|-------|----------------|-------------------|
| 1 | -5% | ×1 multiplier |
| 2 | -10% | ×1 multiplier |
| 3 | -15% | ×1 multiplier (max 2 additional entries) |

DCA uses the same scoring/filter pipeline as the initial entry.

**Source:** `/bybit_ws/dca.py` (~8k)

### SL Re-entry (sl_reentry.py)

After a stop loss is hit:
- **Simple mode**: one re-entry at Lower BB
- **Ladder mode**: 3 levels (0.95, 0.90, 0.85 of entry)
- Max re-entries: 2 (configurable)
- Requires regime filter (not in volatile regime)
- 4h cooldown between re-entries

**Source:** `/bybit_ws/sl_reentry.py` (~13k)

---

## SHORT Strategy

### Vanilla SHORT (auto_short.py)

| Parameter | Value |
|-----------|-------|
| Trigger | BB Daily > 85% |
| Leverage | 3x |
| Max positions | 3 (shared with JUNK) |
| SL | +5% (Tier A/B), +7% (Tier C/D) |
| TP | Middle BB |
| Dry Spell Throttle | Limits entries after consecutive losses |

### JUNK SHORT (Pump-shorts)

Entered on pump-detected coins (Tier C/D only):

| Parameter | Value |
|-----------|-------|
| Trigger | 24h gain ≥ 80% + BB Daily > 70% |
| SL | **None** (entry-based emergency close) |
| TP | Via `junk_trail.py` — fixed trailing: 70% at +15%, 85% at +30% |
| Max positions | 3 (shared with vanilla SHORT) |

### Junk Trail (junk_trail.py)

Trailing TP for JUNK shorts: book profits at fixed intervals (70% at +15%, 85% at +30%).

**Source:** `/bybit_ws/junk_trail.py` (~5k)

### Correlation Sizing

- **r > 0.85**: Entry blocked for the correlated symbol
- **r > 0.70**: Position sized at 50%
- Correlation matrix computed every heavy cycle

**Source:** `/bybit_ws/correlation.py` (~14k)

---

## X10 Strategies (High Leverage)

Three companion strategies running at **10x leverage** with separate risk limits. Triggered every **20 cycles (10 min)**.

### BB Scalp (`bb_scalp.py`)

Scalps on M5 timeframe when price touches the outer BB:
- Entry: First candle outside BB + volume confirmation
- SL: Opposite BB or ATR-based
- Max daily losses: tracked by `x10_limits.py`

### Mean Reversion (`mean_revert.py`)

Enters when RSI is oversold (<25)/overbought (>75) with BB confirmation:
- LONG: RSI < 25 + price near Lower BB
- SHORT: RSI > 75 + price near Upper BB
- ATR-based position sizing

### Funding Momentum (`funding_entry.py`)

Trades based on extreme funding rates:
- LONG: Negative funding (shorts paying) + oversold
- SHORT: Positive funding (longs paying) + overbought
- Funding rate threshold configurable

### X10 Risk Limits (`x10_limits.py`)

- Daily loss limit: stops all X10 strategies if exceeded
- Per-symbol limits
- Global X10 max positions: configurable

**Source:** `/bybit_ws/bb_scalp.py` (~8k), `/bybit_ws/mean_revert.py` (~7k), `/bybit_ws/funding_entry.py` (~9k), `/bybit_ws/x10_limits.py` (~5k), `/bybit_ws/atr_sizer.py` (~6k)

---

## Market Analysis Modules

### Pump Detection (`pump_detect.py`)

Two-tier detection:
- **24h pump**: ≥80% gain in 24h → alert + JUNK short candidate
- **Weekly pump**: ≥230% gain in 7d → alert
- State tracked in `state_db.pump_state`

### Overbought Detection (`overbought.py`)

Rotates watchlist to remove overheated coins (BB > 90% on multiple timeframes).

### Funding Rotation (`funding_rotation.py`)

Tracks extreme funding rates across the watchlist and rotates SHORT entries toward coins with the most positive funding.

### Regime Detection (`regime.py`, `lstm_regime.py`)

Two modes:
- **Rule-based** (always active): Classifies as TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL based on BB width and ATR
- **LSTM** (feature flag `regime_auto`): Neural network trained on historical klines for market regime classification

### Entry Filters

| Filter | File | Behavior | Fail Mode |
|--------|------|----------|-----------|
| MTF Confirmation | `mtf_confirmation.py` | Checks confluence of Daily+Weekly+Monthly BB signals | fail-open (-10%) |
| Orderbook Imbalance | `orderbook_filter.py` | bid/ask ratio check | fail-open (-10%) |
| Volume Confirmation | `volume_filter.py` | Current volume vs SMA volume | fail-open (-10%) |
| Entry Judge | `entry_judge.py` | LLM (Nemotron→DeepSeek), 5s timeout | fail-closed (block) |
| Correlation | `correlation.py` | Cross-symbol correlation matrix | fail-open (-10%) |
| Post-trade Clusters | `post_trade.py` | Blocks symbols in WR<40% clusters | fail-open (-10%) |
| Risk Manager | `risk_manager.py` | CB, margin, max pos, banned symbols | fail-closed (block) |

---

## Self-Learning System (`journal/`)

### Canary Mode (self_learn.py)

- **10%** of entries use new parameters
- **48h** evaluation window
- Auto-promote if canary WR ≥ baseline
- Auto-rollback if canary WR drops >10% below baseline
- State file: `~/.local/share/bybit-ws/canary_state.json`

### Trade Journal Analyzer (analyzer.py)

Diagnoses behavior biases:
1. **Disposition effect** — holding losers too long, winning too short
2. **Overtrading** — excessive frequency
3. **Chasing** — entries after large moves
4. **Anchoring** — fixating on entry prices

### Post-Trade Cluster Analysis (post_trade.py)

Analyzes win rate by entry-cluster:
- Groups trades by entry conditions (BB%, volume, time of day, etc.)
- Blocks clusters with WR < 40%
- Runs daily (every 2880 cycles)

**Source:** `/bybit_ws/journal/self_learn.py`, `/bybit_ws/journal/analyzer.py`, `/bybit_ws/post_trade.py`

---

## Session-Adaptive Parameters (`session_params.py`)

Parameters automatically adjust based on market session:

| Session | Hours (UTC) | BB Threshold | SL | TP | Max Positions |
|---------|------------|-------------|-----|-----|---------------|
| NY | 13:00-22:00 | Normal | Normal | Normal | Full |
| Asia | 22:00-08:00 | Tighter | Tighter | Wider | Reduced |
| Weekend | All day Sat-Sun | Tightest | Tightest | Wider | Minimal |

**Source:** `/bybit_ws/session_params.py` (~3k)

---

## Partial TP (`partial_tp.py`)

Checks for partial take-profit conditions on existing positions. Complements the main ATR-based TP by closing portions of positions at defined profit levels.

**Source:** `/bybit_ws/partial_tp.py` (~9k)

---

## ML Pipeline

| Component | File | Status | Notes |
|-----------|------|--------|-------|
| RandomForest Scorer | `ml_scorer.py` | **Production** | Feature-based signal scoring |
| LSTM Regime | `lstm_regime.py` | Feature flag | Market regime classification |
| Ensemble | `ensemble.py` | **Production** | Weighted voting: RF(0.34) + LSTM(0.33) + RL(0.33) |
| RL Agent (DQN) | `rl_agent.py` | Experimental | Stable-Baselines3 DQN, 3 actions |
| RL Environment | `rl_env.py` | Experimental | Gymnasium, PnL reward |
| DSPy Optimizer | `dspy_optimizer.py` | Feature flag | BootstrapFewShot + MIPROv2 |

---

## Change Guidance

When adding a new strategy or entry filter:
1. Create the module in `/bybit_ws/` following the existing pattern (sync function, accepts `positions` dict)
2. Add the call in `main_async.py` heavy cycle (not light cycle — I/O is expensive)
3. Add a feature flag in `feature_flags.py` if the feature should be toggleable
4. Add AST checks to `test_logic_integrity.py` if the function must always be called
5. Add test cases to the relevant test file

When modifying SL/TP logic:
- `auto_sl.py` and `auto_tp.py` run every light cycle (30s) — must be fast
- Always test with `test_smoke.py` (16 integration tests focused on SL/TP)
- Check the `test_tight_*` functions in `test_smoke.py` for trailing SL edge cases

When changing position sizing (`position_sizing.py`):
- The function is called during entry — affects margin allocated per position
- Verify against `risk_manager.py` limits (max positions per tier, max total margin)

When modifying the entry pipeline:
- Each filter in `auto_entry.py` is independently testable
- The overall score function `score_coin()` in `test_modules.py` has coverage
