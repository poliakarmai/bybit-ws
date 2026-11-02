# Self-Learning Module — bybit-ws v10

**English** · [Русский](SELF_LEARN.md)

> Autonomous self-learning of the trading strategy. No human involvement.
> Updated: 2026-08-04. Quality numbers (trades, WR, drawdown) are not stored in docs — they go stale
> within a day; get them with a command:
> `sqlite3 ~/.local/share/bybit-ws/state.db "SELECT count(*), round(100.0*sum(pnl>0)/count(*),1) FROM trade_history WHERE strategy='auto' AND manual=0"`.

## How It Works

The bot trades 24/7. After each trade — micro-learning. Full analysis runs when **≥10 trades AND ≥6h** since the last run; if few trades — fallback after **24h** (`should_run_self_learn_v6`, `main_async.py`). The module evolved from simple analysis (v4) to a production-grade adaptive system with 20+ mechanics (v10).

### Micro-Learning (After Each Trade)

| Action | Purpose | v |
|----------|-------|---|
| Bandit posterior update | Thompson Sampling learns which parameters perform better | v9 |
| Outlier protection (>3σ) | A flash crash does not break learning | v10 |
| Regime-aware drift detector | Check: has the strategy broken? | v9 |
| Streak protection | 3 losses → half size | v4 |
| Symbol profile update | Statistics for each coin | v4 |

### Full Analysis (Every 6 Hours)

**1. Composite Score** — one number (0-1): “how well I trade”

| Metric | Weight | Current value |
|---------|-----|-----------------|
| Win Rate | 30% | 57% (weighted, decay) |
| Profit Factor | 25% | 0.75 |
| Sharpe | 20% | -0.045 |
| Max Drawdown | 15% | $284 |
| Avg Hold | 10% | 12h |

> Weights are adaptive: WR is more important in a trend, while MaxDD is more important in a crisis/choppy market (30-40%).
> Exponential decay: older trades have less weight (100 days = 37% of the weight).

**2. The Market Regime Changes the Settings**

| Regime | min_score | SL | TP | Direction |
|-------|-----------|-----|------|-------------|
| Uptrend | 25 | 5% | ×1.5 | LONG |
| Downtrend | 35 | 5% | ×0.8 | SHORT |
| Sideways | 30 | 5% | ×1.0 | BOTH |
| Choppy | 40 | 4% | ×0.7 | NONE |

**3. Thompson Sampling** — 5 arms with different parameters. Dynamic Bandit: automatic pruning of the worst arms and generation of variations of the best arm every 24h. Uncertainty-aware: explores when variance is high.

**4. Drift Detector** — per-regime windows (HIGH_VOL=30, RANGING=100). EMA baseline. Causal inference: if BTC fell + WR fell → MARKET, if BTC is stable → PARAMETERS.

**5. Stress Tests** — 4 historical scenarios + 1000 Pareto MC (α=2.5, heavy-tail). Threshold: prob_ruin < 5%.

**6. Canary** — 10% of entries (adaptive 5-20%). Bayesian A/B test. Idle timeout 3h.
> ⚠️ **Disabled in production:** `CANARY_ENABLED = False`, `CANARY_ENTRY_PCT = 0.0` (`self_learn.py:55-57`). The code is live and covered by tests, but is not applied to live entries.

## Architecture

```
main_async.py (event-driven: ≥10 trades AND ≥6h, else 24h fallback)
  └─ should_run_self_learn_v6()
       └─ load_from_sqlite() → analyze()
            └─ apply_journal_insights()
                 │
                 ├─ ON EACH TRADE (micro):
                 │    robust_bandit_update()          ← outlier 3σ (v10)
                 │    RegimeAwareDriftDetector         ← per-regime (v9)
                 │    symbol_profiles update           ← incremental (v9)
                 │
                 └─ EVERY 6H (batch):
                      weighted_composite_score()       ← decay (v10)
                      composite_score_v8()             ← adaptive weights (v8)
                      causal_analysis()                ← market vs params (v10)
                      detect_anomalous_trades()        ← IQR filter (v8)
                      monte_carlo_stress_test_v9()     ← Pareto MC (v9)
                      stress_test_params()             ← 4 crash scenarios (v7)
                      DynamicParameterBandit           ← auto-prune (v9)
                      CoordinatedEnsemble              ← regime handover (v10)
                      select_arm_with_uncertainty()    ← explore/exploit (v10)
                      walk_forward_validation()        ← out-of-sample (v6)
                      bayesian_ab_test()               ← Beta posterior (v6)
                      canary_*                        ← ⚠️ CANARY_ENABLED=False in prod
```

## Files

| File | Purpose |
|------|-----------|
| `bybit_ws/journal/self_learn.py` | v10 core: Dynamic Bandit, Pareto MC, Drift, Ensemble, Causal |
| `bybit_ws/journal/analyzer.py` | Profile + 4 bias diagnostics |
| `bybit_ws/journal/adapter.py` | SQLite → normalized trades |
| `canary_state.json` | Canary experiment state |
| `self_learn.jsonl` | Log of all adjustments (98 records) |
| `symbol_profiles.json` | Per-symbol statistics (in the data directory; created during the first learning run) |
| `exit_stats.jsonl` | Exit reasons (86 records) |
| `parameter_ensemble.json` | State of 6 bandits |
| `params_history/v*.json` | Git-like parameter versions |
| `global_params_log.json` | Snapshots for rollback |

## Diagnostics

```bash
# All self-learn logs
grep -E "Self-learn|Composite|Bandit|MC-v9|Causal|Anomaly|Decay" \
  ~/.local/share/bybit-ws/events.log | tail -10

# Ensemble state (all 6 bandits)
python3 -c "
from bybit_ws.journal.self_learn import load_ensemble
for r, b in load_ensemble().get_all_best().items():
    if b.get('trades', 0) > 0:
        print(f'{r}: {b[\"params\"]} WR={b[\"wr\"]:.0%} trades={b[\"trades\"]}')
"

# Drift detector (per-regime)
python3 -c "
from bybit_ws.journal.self_learn import get_regime_drift_detector
import json; print(json.dumps(get_regime_drift_detector().get_status(), indent=2))
"

# Pareto Monte Carlo
python3 -c "
from bybit_ws.journal.self_learn import monte_carlo_stress_test_v9, get_params_for_regime
import json; print(json.dumps(monte_carlo_stress_test_v9(get_params_for_regime(), 500), indent=2))
"

# Causal analysis
python3 -c "
from bybit_ws.journal.self_learn import causal_analysis
import sqlite3
db='/home/openclaw/.local/share/bybit-ws/state.db'
conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
trades=[dict(r) for r in conn.execute('SELECT pnl,closed_at FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
conn.close()
print(causal_analysis(trades))
"

# Exponential decay WR
python3 -c "
from bybit_ws.journal.self_learn import weighted_wr, weighted_composite_score
import sqlite3
db='/home/openclaw/.local/share/bybit-ws/state.db'
conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
trades=[dict(r) for r in conn.execute('SELECT pnl,closed_at,hold_hours FROM trade_history WHERE closed_at IS NOT NULL ORDER BY entry_at')]
conn.close()
print(f'Weighted WR: {weighted_wr(trades):.1%}')
cs=weighted_composite_score(trades)
print(f'Composite: score={cs[\"score\"]} WR={cs[\"wr\"]:.1%} PF={cs[\"pf\"]} DD=\${cs[\"max_dd\"]:.0f}')
"

# Manual canary reset
echo '{"active":false}' > ~/.local/share/bybit-ws/canary_state.json
```

## Version Evolution

| v | Date | Key mechanisms | Lines of code |
|---|------|-------------------|-----------|
| v4 | 01.08 | Per-symbol profiles, exit tracking, streak guard | ~650 |
| v5 | 04.08 | Wall-clock trigger, regime stats, cluster | +200 |
| v6 | 04.08 | Bayesian A/B, shrinkage, SL time, walk-forward, feature importance | +300 |
| v7 | 04.08 | Composite score, regime params, stress test, drawdown, adaptive canary | +250 |
| v8 | 04.08 | Thompson Sampling, Monte Carlo, Drift, Anomaly, adaptive weights, versioning | +430 |
| v9 | 04.08 | Dynamic Bandit, Pareto MC, Regime Drift, Ensemble, Micro-updates | +470 |
| v10 | 04.08 | Robust updates, Exponential decay, Uncertainty Thompson, Coordinated, Causal | +200 |

## Mechanism Summary Table

| Mechanism | v4 | v5 | v6 | v7 | v8 | v9 | v10 |
|---|---|---|---|---|---|---|---|
| A/B test | — | — | Bayesian | Bayesian | Thompson | Thompson | +Uncertainty |
| Canary | 10% | 10% | 10% | Adaptive | Bandit | Dynamic | Dynamic |
| Stress test | — | — | — | 4 hist | 1000 MC | Pareto MC | Pareto MC |
| Drift | — | — | — | — | ADWIN | Regime | +Causal |
| Score | — | — | WR+CI | Fixed w | Adaptive | Adaptive | +Decay |
| Regime | — | Stats | Stats | Params | Weights | Ensemble | +Coordinated |
| Updates | Batch | Batch | Batch | Batch | Batch | Micro | +Robust |
