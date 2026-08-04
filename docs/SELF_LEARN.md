# Self-Learning Module — bybit-ws v7

> Автономное самообучение торговой стратегии. Без участия человека.
> Обновлено: 2026-08-04.

## Архитектура

```
main_async.py (event-driven: ≥10 сделок / 6ч или 24ч fallback)
  └─ should_run_self_learn_v6()      ← проверка условий
       └─ mark_self_learn_run()      ← запись времени
            └─ load_from_sqlite()     ← adapter.py: SQLite → analyzer
                 └─ analyze()          ← analyzer.py: профиль + bias
                      └─ apply_journal_insights()  ← коррекция параметров
                           ├─ composite_score       ← multi-objective (v7)
                           ├─ regime params         ← per-regime tuning (v7)
                           ├─ stress testing        ← crash scenarios (v7)
                           ├─ drawdown adjustment   ← risk-aware (v7)
                           ├─ adaptive canary %     ← dynamic (v7)
                           ├─ Bayesian shrinkage    ← cluster mean (v6)
                           ├─ SL time analysis      ← quick vs slow (v6)
                           ├─ walk-forward          ← out-of-sample (v6)
                           ├─ global rollback       ← auto-rollback (v6)
                           ├─ feature importance    ← filter tracking (v6)
                           └─ canary: Bayesian A/B  ← Beta posterior (v6)
```

## V7 — Новое (04.08.2026)

| # | Механика | Описание |
|---|----------|----------|
| 1 | **Composite Score** | WR(30%) + Profit Factor(25%) + Sharpe(20%) + MaxDD(15%) + AvgHold(10%) |
| 2 | **Regime-Specific Params** | Раздельные min_score/sl_pct/tp_mult/max_positions/direction для 6 режимов |
| 3 | **Stress Testing** | 4 crash-сценария: COVID 2020, FTX 2022, China Ban 2021, Luna 2022 |
| 4 | **Drawdown-Based** | DD>10% → conservative (×1.3 min_score, ×0.5 size). Profit>5% → aggressive |
| 5 | **Adaptive Canary %** | WR стабилен → 20%, нестабилен → 5%, default 10% |

### Composite Score

```python
score = 0.30*wr + 0.25*pf_norm + 0.20*sharpe_norm + 0.15*dd_norm + 0.10*hold_norm
# score: 0..1, выше = лучше
# Пороги: score up >5% → promote, down >10% → rollback
# max_dd > $15 → force conservative независимо от WR
```

### Regime-Specific Parameters

```json
// regime_params.json
{
  "TRENDING_UP":    {"min_score":25, "sl_pct":5, "tp_mult":1.5, "direction":"LONG_only"},
  "TRENDING_DOWN":  {"min_score":35, "sl_pct":5, "tp_mult":0.8, "direction":"SHORT_only"},
  "RANGING":        {"min_score":30, "sl_pct":5, "tp_mult":1.0, "direction":"BOTH"},
  "CHOPPY":         {"min_score":40, "sl_pct":4, "tp_mult":0.7, "direction":"NONE"},
  "HIGH_VOL":       {"min_score":22, "sl_pct":6, "tp_mult":1.3, "direction":"BOTH"},
  "LOW_VOL":        {"min_score":28, "sl_pct":4, "tp_mult":0.9, "direction":"BOTH"}
}
```

### Stress Testing

| Сценарий | BTC Drop | Alt Drop | VIX |
|----------|----------|----------|-----|
| COVID Crash (Mar 2020) | -50% | -70% | ×5 |
| FTX Collapse (Nov 2022) | -25% | -40% | ×3 |
| China Ban (May 2021) | -35% | -50% | ×4 |
| Luna Collapse (May 2022) | -30% | -55% | ×4.5 |

## V6 — Bayesian + Shrinkage + Walk-Forward

| # | Механика |
|---|----------|
| 1 | Bayesian A/B testing — P(canary > baseline) через Beta |
| 2 | Bayesian shrinkage — per-symbol → cluster mean |
| 3 | SL time analysis — quick (<30м) vs slow (>4ч) |
| 4 | Event-driven trigger — ≥10 сделок / 6ч или 24ч fallback |
| 5 | Global rollback — WR drop >15% → авто-откат |
| 6 | Feature importance — WR pass/fail по фильтрам |
| 7 | Confidence intervals — Wilson score 95% CI |
| 8 | Walk-forward validation — 70/30 out-of-sample |
| 9 | Human-readable explanations |

## Диагностика

```bash
# Composite score (live)
python3 -c "
from bybit_ws.journal.self_learn import composite_score, get_regime_aware_stats
stats = get_regime_aware_stats()
# Преобразуем в список трейдов для composite_score
print('Regime stats:', stats)
"

# Regime params
cat ~/.local/share/bybit-ws/regime_params.json | python3 -m json.tool

# Stress test
python3 -c "
from bybit_ws.journal.self_learn import stress_test_params, get_params_for_regime
import json
params = get_params_for_regime()
result = stress_test_params(params)
print(json.dumps(result, indent=2))
"

# Drawdown adjustment
python3 -c "
from bybit_ws.journal.self_learn import drawdown_adjustment
print(drawdown_adjustment())
"
```

## Версионирование

| Версия | Дата | Ключевые изменения |
|--------|------|-------------------|
| v7 | 04.08.2026 | Composite score, regime params, stress testing, drawdown, adaptive canary |
| v6 | 04.08.2026 | Bayesian A/B, shrinkage, SL time, event trigger, rollback, feature importance |
| v5 | 04.08.2026 | Wall-clock trigger, regime-aware stats, cluster learning, idle timeout |
| v4 | 01.08.2026 | Per-symbol profiles, exit tracking, session params, streak guard |
