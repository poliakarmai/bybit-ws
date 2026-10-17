---
date: 2026-10-17
status: active
tags: [bybit-ws, research, ideas, roadmap]
sources:
  - docs/research/mean_reversion_bb_best_practices.md (поток 2)
  - docs/research/risk-position-sizing-anti-overfit-2026-10-17.md (поток 3)
  - GitHub OSS-анализ (поток 1, freqtrade/hummingbot/nautilus/jesse/OctoBot/passivbot)
---

# Идеи для улучшения bybit-ws (17.10.2026)

Research: 3 параллельных потока (GitHub OSS-боты, Bollinger/mean-reversion практики,
risk + anti-overfit) + собственная верификация по коду репозитория.

**Легенда достоверности:** `[В]` — верифицировано мной по коду/данным в этой сессии ·
`[И]` — из источников research, не перепроверено · `[К]` — калибровка/гипотеза, требует A/B.

---

## 0. Дефекты, найденные в нашем коде (верифицировано `[В]`)

### 0.1. Детектор `chasing_momentum` измеряет не то, что заявляет — ЛОЖНАЯ ТРЕВОГА

`bybit_ws/journal/analyzer.py:379-418`, подтверждено цепочкой
`adapter.py:67-76` (`Trade.price = entry_px`) → `check_chasing`:

```python
sym_prices[b.symbol].append(b.price)     # b.price = ЦЕНА ВХОДА сделки
for i in range(3, len(prices)):
    if prices[i] > prices[i-3] * 1.03:   # вход[i] vs вход[i-3]
        chased += 1
```

Сравниваются **цены входов** в один и тот же символ между разными сделками
(между ними могли пройти дни). Это не «покупка после роста цены >3%».
Метрика косвенная → `[HIGH] 21/28 (75%)` в `/journal` — артефакт метода, не факт.

При этом реальный анти-chasing **работает**: `auto_entry.py:36-39, 768-777`
(`CHASE_PCT=3.0`, `CHASE_LOOKBACK=5` — блок LONG при росте >3% за 5 дневных свечей).

**Действие:** заменить метрику на честную — «рост цены по свечам за N часов
непосредственно ПЕРЕД входом» (данные есть: kline-запрос уже делается в guard).
Оценка: **S**. Смысл: убрать ложный [HIGH], который маскирует реальные проблемы.

### 0.2. Kelly sizing — мёртвый код

`position_sizing.py:333 kelly_margin()`, `:312 kelly_fraction()`,
`:287 load_kelly_stats_from_backtest()`. Grep по всем `.py`: **ноль вызовов**
вне определений. `KELLY_FRACTION=0.25` не влияет ни на одну сделку.

**Действие:** либо удалить (проще), либо подключить по **кластеру** при N≥50
(per-symbol Kelly негоден: 199 сделок / ~30 символов = 6-7 на символ → f* гуляет в 3×).
Оценка: **S**.

### 0.3. `volatility_scale` не уменьшает размер при росте волатильности

`volatility_filter.py`: `clamp(min(1, base/current), ≥0.5)` — **потолок 1.0**,
значит scale ≤ 1 всегда, а фильтр к тому же `enabled: False` по умолчанию
(тест `test_returns_one_when_disabled` подтверждает: возвращает ровно 1.0).
Итог: при 7 позициях размеры **полные в любой волатильности**.

**Действие:** двусторонний vol-targeting `w = σ_target/σ̂` с полом И потолком,
включить после A/B. Оценка: **M**. `[И]` классика vol targeting.

### 0.4. Корреляционный гейт — бинарный по одной паре, без кластерного бюджета

`correlation.py:66-74` docstring обещает «max_corr>0.7 → размер ×0.5»,
но `risk_manager.check_new_entry_correlation` (`:262-280`, вызов `:647`) этого
**не делает** — только бинарный блок при r>0.8 с **одной** позицией.

Живой замер `[В]` (поток 3, по 7 текущим LONG, 7 дней): средняя часовая корреляция
**ρ=0.80** (min 0.65 HYPE, max 0.94 XRP-SOL). Равновзвешенный портфель
σ = 0.911 от суммы → **7 позиций ≈ 1.1 независимой**. HYPE — единственный
частично независимый кластер.

### 0.5. Нет purged CV / embargo

`grep purge|embargo|PurgedKFold` в `bybit_ws/experiments/` → **0**.
`validation.py` (DSR/PBO/permutation) корректен, но triple-barrier метки
перекрываются во времени → оценка meta-label завышена. Оценка: **M** (~40 строк).

### 0.6. Funding — только алерт

`funding_tracker` шлёт алерт при экстремуме, на размер и EV сделки **не влияет**.

---

## 1. Что у нас УЖЕ есть — не переносить

| Механика | Где |
|---|---|
| HMM + BOCPD, triple-barrier, Thompson bandit | `bybit_ws/experiments/{regime,triple_barrier,bandit}.py` |
| DSR / PBO / permutation | `experiments/validation.py` |
| LSTM-режим (5 классов), LLM entry-judge | `lstm_regime.py`, `entry_judge.py` |
| Unified SL (5 стратегий, приоритет), 4 trailing, time-exit | `unified_sl.py`, `trailing_sl.py` |
| Anti-chasing guard | `bybit_ws/chase_guard.py` + `auto_entry.py:768` |
| Cooldown после SL/TP | `config.py:51-52` (4ч / 1ч) |
| Position sizing (% депо, max_share), max_positions, dynamic margin | `position_sizing.py`, `config.py` |
| Корреляционная матрица (порог 0.8) | `correlation.py`, `risk_manager.py:647` |
| Pump/squeeze детект, funding tracker/rotation | `pump_detect.py`, `squeeze.py`, `funding_*.py` |
| Health-checks, Telegram/ntfy с дедупом, Prometheus, kill_switch | `health.py`, `rpc.py`, `/metrics` |

---

## 2. Идеи по приоритету

### P1 — делать первыми (высокий ROI, низкий риск)

| # | Идея | Почему | Усилие | Источник |
|---|---|---|---|---|
| 1 | **Кластерный риск-кап**: считать коррелированные позиции (ρ>0.7) одной; лимит `R_cluster ≤ 2R`, а не per-trade | 7 LONG = 1.1 позиции. Сейчас каждая проходит лимиты отдельно → фактическое плечо на рынок в 2-3× выше задуманного | M | `[В]` замер ρ=0.80 |
| 2 | **Починить детектор chasing** (§0.1) | Ложный [HIGH] маскирует реальные проблемы | S | `[В]` |
| 3 | **Protections с TTL** вместо перманентного `symbol_blacklist`: серия SL по паре → пауза 24ч; просадка аккаунта → пауза всех входов | у freqtrade `StoplossGuard`/`LowProfitPairs`/`MaxDrawdown`; у нас — только вечный бан | S | `[И]` freqtrade |
| 4 | **Подтверждающая свеча на входе** (цена вернулась внутрь полосы) вместо входа по касанию | По правилу Боллинджера закрытие за полосой = сигнал **продолжения**; вход сразу = фейд импульса = chasing по определению | M | `[И]` + `[К]` (порог подобрать A/B) |
| 5 | **postOnly-входы + market-fallback** | Экономия на комиссии (maker vs taker) на каждом входе | M | `[И]` hummingbot `GridExecutor` |

### P2 — после P1

| # | Идея | Почему | Усилие | Источник |
|---|---|---|---|---|
| 6 | Двусторонний vol-targeting (включить `volatility_filter`) | §0.3 — сейчас размер не сжимается | M | `[В]`+`[И]` |
| 7 | Reconciliation раз в N циклов: `fetch_positions()` vs `state.db` по qty/avg-px с допуском → alert + adopt | рестарты/ручные сделки на бирже → расхождение молча | M | `[И]` nautilus |
| 8 | Purged K-fold + embargo в `validation.py` | §0.5 — устранить утечку у triple-barrier | M | `[И]` LdP |
| 9 | ADX(14)-фильтр режима для входов: <25 разрешить, 25-40 при росте стоп, >40 запретить | проверенный практикой фильтр trend/range; у нас есть LSTM-режим, но ADX дешевле и независим | S | `[И]` Mubite/Alchemy |
| 10 | Funding-aware размер/EV (не только алерт) | §0.6; funding >0.1%/8ч = >100% годовых — съедает эдж удержания | M | `[И]`+`[В]` |
| 11 | lookahead-аудит для `experiments/engine.py`: `assert calc_bb(closes[:i+1])` не зависит от будущего | дешёвый анти-оверфит, у нас нет | S | `[И]` freqtrade |

### P3 — косметика / когда будет время

| # | Идея | Усилие |
|---|---|---|
| 12 | Удалить мёртвый Kelly (§0.2) | S |
| 13 | MAE/MFE + slippage + fee drag в `lp_report` — покажет, торгуем ли эдж или платим комиссии | M |
| 14 | DI-гейт для LSTM: не торговать вне обучающего распределения (Dissimilarity Index) | M |
| 15 | Exit taxonomy enum вместо строковых типов закрытия | S |
| 16 | Полная библиотека метрик (Omega, Ulcer, CVaR) | S |

---

## 3. Стратегическая находка (важнее любой отдельной фичи)

**Закрытие за полосой Боллинджера — сигнал ПРОДОЛЖЕНИЯ движения, а не разворота.**
Это правило самого Боллинджера. Значит вход «сразу по касанию полосы» — это
не mean-reversion, а контртрендовый фейд импульса, т.е. структурный chasing.

Отсюда: наши `BB ≤ 25%` входы могут системно ловить падающие ножи (LONG) —
что согласуется с убыточной SHORT-стороной (−$74, WR 68.6%: высокий WR при
минусе = мелкие профиты + редкие большие лоси, классический хвост).

**Проверять в первую очередь:** A/B подтверждающей свечи (идея #4) на
`experiments/` стенде, а не сразу в live.

---

## 4. Чего НЕ делать (проверено, неприменимо)

- Event-driven ядро / Controller-Executor / плагины-tentacles — D-интервал, latency не узкое место.
- Auto-unstuck (passivbot) — ломает TP/SL/time-exit логику Grid.
- Ещё один ML-пайплайн — LSTM + meta_label + DSPy + RL уже есть.
- per-symbol Kelly — данных мало (6-7 сделок/символ).

## 5. Открытые вопросы (нужна проверка)

- `[К]` Пороги anti-chase для часовых данных (поток 2 предлагал 2%/4ч, 5%/24ч) — калибровка, не исследование. A/B.
- Hurst exponent: корректная оценка на случайных данных даёт ~0.572, не 0.5 → использовать только в research-скрипте, не как live-гейт.
- Число «Sharpe 2.11 / WR 72%» из lunefi — маркетинг, в выводы не брал.
- Liquidations feed, портфельные аллокации nautilus, walk-forward параметров в self_learn — не проверялись.
