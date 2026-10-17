# Продвинутый risk management / sizing / anti-overfit для bybit-ws

Аудит кода + литература. Всё, что помечено **[В]** — проверено в этом репо/API сейчас; **[И]** — из
литературы/индустрии, ссылка дана, но не воспроизводил; **не проверено** — явно указано.

---

## 0. Что у нас уже есть (по факту кода, не по описанию)

| Механика | Факт в коде | Вывод |
|---|---|---|
| Корреляционный блок | `risk_manager.check_new_entry_correlation` блокирует вход, если r>0.8 с **одной** существующей позицией того же направления, по snapshot из `correlation.json` (**24h, только часы, только проценты**; `abs()>thr` внутри `max_corr_with_open`) | Бинарный гейт не видит **кластерный** риск |
| Размер при r>0.7 | `max_corr_with_open` docstring обещает «размер ×0.5», но в `check_new_entry_correlation` этого **нет** — либо блок, либо полный размер | Градации нет |
| Kelly | `position_sizing.kelly_fraction` (f*=(pb−q)/b, KELLY_FRACTION=0.25) — **нигде не вызывается** (`kelly_margin` ни один прод-модуль не импортирует) | Мёртвый код |
| Volatility targeting | `volatility_filter.volatility_scale = clamp(min(1, base/current), ≥0.5)`, результат **зажат сверху 1.0** и полом 0.5 | Работает только в «тихой» половине —**при росте волатильности позиция не уменьшается никогда**; фактически бинарный гейт при 2×ATR |
| Плечо | 5x, **изолированная** маржа (ликвидация ≈ при падении цены на ~20% и меньше) | Стопы 8–15% от цены: ликвидация в 1.3–2.5× дальше стопа — считается нормально, но без буфера на гэп |
| Funding | `funding_tracker` — только **алерт** при экстремуме, не влияет ни на вход, ни на размер | Не учитывается в EV сделки |
| Anti-overfit | `validation.py`: DSR, CSCV-PBO, sign-flip permutation | Нет purged CV / embargo |
| Метрики | `lp_report` Sortino/Calmar/expectancy; `backtest` Sharpe/Sortino/Calmar; **нет MAE/MFE, нет slippage, нет VaR** | Диагностика неполная |

**Живой замер сейчас [В]:** 7 LONG (AAVE, XLM, LTC, HYPE, XRP, DOGE, SOL), маржа $104.27, daily PnL −$15.03,
лимит −$50 (использовано 30%). **Корреляция часовых доходностей за 7 дней:**

```
        AAVE    XLM    LTC   HYPE    XRP   DOGE    SOL
AAVE    1.00   0.77   0.79   0.65   0.81   0.82   0.82
XLM     0.77   1.00   0.82   0.71   0.88   0.85   0.87
LTC     0.79   0.82   1.00   0.67   0.83   0.85   0.85
HYPE    0.65   0.71   0.67   1.00   0.72   0.67   0.73
XRP     0.81   0.88   0.83   0.72   1.00   0.89   0.94
DOGE    0.82   0.85   0.85   0.67   0.89   1.00   0.88
SOL     0.82   0.87   0.85   0.73   0.94   0.88   1.00
```
Средняя попарная ρ = **0.80** (min 0.65 HYPE, max 0.94 XRP–SOL). Для равновзвешенного портфеля
σ_p = √(wᵀΣw) даёт **0.911** от наивной суммы σ_i/N. То есть диверсификации ~нет: **7 позиций ≈ 1.1 позиции**.
Проверить структуру: мелкие альты сидят на одном бета-факторе (логично — BTC-бета), HYPE — единственный
частично независимый кластер.

Вывод для всего документа: при ρ≈0.8 **любой per-symbol sizing бесполезен**, работает только
портфельный. Ниже — приоритизация от максимального эффекта.

---

## 1. PRIORITY 1 — портфельный риск вместо per-symbol (даст больше всего)

### 1.1 «Portfolio heat» — одна ставка, а не семь

Формула риска кластера (работает без ковариационной матрицы):

$$R_{cluster} = \sum_{i \in C} risk_i \quad\text{или лучше}\quad R_{cluster} = \sqrt{\sum_i \sigma_i^2 w_i^2 + 2\sum_{i\ne j}\rho_{ij}\, w_i w_j \sigma_i \sigma_j}$$

Кластеры строить по знаку **и** ρ: `C = {позиции с direction=+1, avg ρ между ними > 0.5}`.
Правило: **R_cluster ≤ 2R (2 «единицы риска»)**, иначе масштаб $s = 2R/R_{cluster}$ на все новые входы.
Индустрия формулирует то же проще: «коррелирующие позиции считать одной позицией» при ρ>0.7;
два 1R-трейда при ρ=0.85 дают ~1.9R риска (не 2R и не 1R). → [И] fortraders.com/blog/trading-with-correlations…,
gainium.io/tools/position-sizing. Подтверждено двумя источниками.

**Что добавить в код:** функция `cluster_risk(positions) -> {cluster_id: R}` рядом с
`check_correlation_panics` в `risk_manager.py`; gate `R_cluster_new > MAX_R → вход блокируется **или** размер режется
до остатка бюджета кластера` (лучше второе — блокирует при 7 позициях всё, убивает торговлю).

### 1.2 Budget-распределение вместо блокировки

Текущее поведение — «r>0.8 → нет». Правильнее — **делить бюджет кластера на N**:

$$w_i = \frac{R_{cluster\_budget}}{N_i} \quad\text{или risk-parity:}\quad w_i = \frac{1/\sigma_i}{\sum_j 1/\sigma_j}$$

Equally-weighted risk contribution упрощается, если σ_i одинаковы — тогда ERC = 1/N по риску.
При ρ=0.8 и N=7 суммарный доход/риск растёт от добавления позиций почти линейно (σ_p≈Σ), а это **удвоение плеча**, а не
диверсификация — значит размер каждой новой позиции должен падать как ~1/√N при независимых и как ~1/N при ρ=1.
Практическая аппроксимация: $scale_N = 1/\sqrt{N(1+(N-1)\bar\rho)}\cdot N/(1+(N-1)\bar\rho)$ → для N=7, ρ=0.8: ≈0.33.
Т.е. при 7 позициях стандартную маржу надо резать втрое **[расчёт мой по формуле, не из публикации]**.

### 1.3 Historical VaR / CVaR портфеля (1 день, 99%)

Без нормального предположения (крипта хвостатая):

$$VaR_{99\%} = -\,Q_{1\%}\big(\text{P&L}_{1d,port}\big),\quad CVaR_{99\%} = -\,\mathbb{E}[\,P\&L_{1d}\mid P\&L_{1d} < -VaR_{99\%}\,]$$

где $P\&L_{1d,port} = \sum_i exposure_i \cdot r_{i,1d}^{(t)}$ на историч. дневных доходностях (обычно 250–1000 дней).
CVaR (Expected Shortfall) subadditive, VaR — нет; для риск-лимитов BASEL использует ES/FHS. → [И] Basel FRTB
(doc "Minimum capital requirements for market risk", пересмотренная методика: 97.5% ES вместо 99% VaR);
quantifiedstrategies.com — MVaR для position sizing.
Алерт: если CVaR_99 портфеля > 1.5× daily loss limit — резать или закрывать **самую коррелирующую** ногу.

### 1.4 Liq-at-risk — специфика изолированной маржи ×5

На изолированной марже с плечом 5 крупнейший убыток на позицию — не стоп, а **ликвидация = −100% маржи**.
Сейчас daily-loss лимит $50 считается от **реализованного** PnL; у нас открытых uPnL −$15, и при общем
дампе −10% (при ρ=0.8 такое возможно за часы) 7×$104/7 маржи ×5 плечо × −10% ≈ **−$52** — лимит пробит за один бар,
без единой закрытой сделки.

Ввести: `LiqATR = (avg entry price − liq price)/ATR`; алерт, если < 3 ATR.
`get_daily_pnl` **проверен [В]**: строка 177 — «unrealized + realized за сегодня», `total_pnl = unrealized + realized`,
uPnL берётся из `positions.json` (поле `upnl`). Значит daily-лимит **уже** видит открытые позиции — это правильно,
но uPnL из локального `positions.json` может отставать от биржи; брать `upnl` из WS-снапшота, а не из кэша.

---

## 2. Position sizing: что оставить, что добавить

### 2.1 Kelly — только фракция, и только на кластер

$$f^* = \frac{p b - q}{b} = p - \frac{q}{b},\quad \text{risk} = c\cdot f^*,\quad c\in[0.25,0.5]$$

Групповой размер (несколько сигналов на один фактор): $f^*_{group} = \frac{\mu^\top \Sigma^{-1} \mathbf{1}}{1^\top\Sigma^{-1}\mathbf{1}}$
(optimal growth для нормальных доходностей), на практике берут `c·f*` с отсечкой. → [И] Thorp,
«The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market» (pdf на edwardothorp.com) — **не проверял pdf**,
но это де-факто первоисточник.
Из 4 независимых источников: Half-Kelly сохраняет ~75% логарифмического роста при вдвое меньшей дисперсии
(приближение $g(c)/g^* \approx 2c-c^2$ — отсюда 0.75 при c=0.5). Согласовано: journalplus.co,
altrady, tradesearcher, deriv insights. → **половина/четверть Kelly — стандарт**.

У нас: `KELLY_FRACTION=0.25`, но не подключено. **Рекомендация:** не включать per-symbol Kelly
(199 сделок на ~30 символов = 6-7 сделок на символ, WR ±10 п.п. → Kelly гуляет в 3×. → [И] journalplus),
а считать одиночный Kelly **по кластеру** (все LONG вместе) при N≥50.

### 2.2 Volatility targeting с двусторонней зажатостью

Классика: $w_t = \frac{\sigma_{target}}{\hat\sigma_t}$, с полом **и потолком**:
$w_t = \text{clip}(\sigma^*/\hat\sigma_t,\; 0.5, 1.5)$. У нас `min(1, …)` — верхняя сторона мертва.
Минимальная правка: `scale = clip(target/realized, min_scale, 1.0)` где realized = EWMA(σ, λ≈0.94) дневных доходностей
(риск-метрика RiskMetrics, стандарт). Даже без Реализованного таргета — заменить
`1/ratio` на `min(1, (σ_baseline/σ_now)^0.5)` и **не привязывать к ATR ratio 14/20** (ATR — proxy волатильности, но
ratio ATR уже перекрывает стопы; double-counting через same ATR).

### 2.3 Учёт funding в EV сделки (фьючерс-специфика)

Для удержания T часов при плече 5x:

$$EV_{net} = EV_{gross} - \underbrace{\frac{T}{8} f \cdot \text{notional}}_{funding} - 2\cdot\frac{\text{taker fee}}{100} \cdot \text{notional}$$

Funding 0.01%/8ч при 5x = 0.05% маржи/8ч → ~$0.015 на $104 за 8ч — мало. Но 0.1%/8ч (перегрев) при плече 5x
= 0.5% маржи/8ч → ~$0.52/позицию/8ч, 7 позиций за сутки = **~$11/день**, т.е. 22% дневного лимита. → [В] расчёт,
формула стандартна [И]. По Kraken: 0.10%/8ч = >100% годовых, исторически предшествует коррекциям, но
«strong trends can hold elevated funding for weeks» — использовать как **модификатор размера**, не как триггер разворота.
Исследование: perpetuals торгуются выше no-arbitrage на 60–90% годовых (abs deviation), AR(6) SR 1.8 при высоких
издержках — He, Manela, Ross, von Wachter, «Fundamentals of Perpetual Futures», arXiv:2212.06888. → [И]

Правка: `funding_tracker` уже знает экстремумы → пусть отдаёт `sizing_hint = 1 - min(0.5, max(0, (my_side_pays_rate-0.02)/0.1))`
(на 0.02%/8ч — без изменений, на 0.12% — размер ×0.5). Дёшево: используется уже существующие данные.

---

## 3. Anti-overfit: чего нет у нас (DSR/PBO уже есть и реализованы корректно)

`DSR` в `validation.py` соответствует Bailey & López de Prado (2014): `v_sr = (1 - γ3·SR + (γ4-1)/4·SR²)/(T-1)`,
`E[max SR] = √v·((1-γ)·z1 + γ·z2)`, `z1 = Φ⁻¹(1-1/N)`, `z2 = Φ⁻¹(1-1/(N·e))`. **[В]** формулы совпадают с
оригиналом (единственная оговорка: `sr` домножен на √annualization **до** подстановки в v_sr — как в оригинальной
формуле сделано для SR на том же масштабе, проверьте, что annualization и `T` согласованы: если `sr` годовой,
а `T` — число баров, v_sr недооценивается... **в оригинале** `SR` в v_sr — неаннуализированный; **это баг-кандидат,
проверить у нас: `sr` уже ×√365 → `g3*sr` доминирует, `v_sr` завышен → DSR консервативнее. Не строго «баг»,
а несогласованный масштаб**).

**Пробел 1 — purged K-fold + embargo.** Прямой путь: ни DSR, ни PBO не спасают от **утечки через overlapping labels**
(triple-barrier с горизонтом H на 15m/1h барах — метки соседних баром перекрываются). Нужно:

- purge: удалить train-наблюдения, чей интервал метки `[t0_i, t1_i]` пересекает тестовый интервал;
- embargo: дополнительно выкинуть h баров после теста (h ≈ горизонту метки / autocorr lag).
López de Prado, «Advances in Financial Machine Learning», ch. 7; sklearn-совместимые реализации:
`mlfinlab` (на платной лицензии), `skfolio` (open, `skfolio.model_selection.CombinatorialPurgedCV`). **[И]**
CPCV: N групп, k тестовых → $\binom{N}{k}$ сплитов, даёт **распределение** OOS-Sharpe, а не точку — PBO на нём считается
стабильнее. [И] arXiv:2407.17645 (Hopfield Networks, используется CPCV для оценки аллокации).

**Пробел 2 — DSR на все эксперименты, а не только на промоушен.** arXiv:2608.27734v1 («What survives honest
evaluation?», 2026) показывает: система с look-ahead leak'ом с SR 35 **проходит** DSR и PBO. Т.е. баг в данных
не ловится статистикой — нужна защита на уровне фич (registry-валидированные фичи без будущего). У нас эксперименты
идут через `backtest_runner` с кэшем свечей — **это и есть потенциальный leak-point** (кэш по `(symbol, interval, start, end)`,
если ключ не учитывает `--end-date` в engine, а только запрос. Не проверял реализацию — см. `experiments/engine.py`).

**Пробел 3 — «все скоры и trials записаны»**. DSR требует **честное** `n_trials=585`; если optuna/parallel_sandbox
запускался мимо логгера, DSR занижает threshold (и мы переоцениваем эдж). Собрать `ExperimentStore` count/день.

**Пробел 4 — walk-forward со статичным окном.** У нас `walk-forward validation` в self_learn — но по описанию
это, вероятно, просто rolling-сравнение ресурсов. Правильный WFO: fit на [t0,t1], test [t1+embargo,t2], сдвиг.
Плюс **early-stopping bias**: если на каждом фолде мы тюним параметры, итоговый OOS-скор — это уже выбор.

Не проверено: не открывал `experiments/engine.py` на предмет leak; это стоит сделать в отдельной задаче.

---

## 4. Adaptive параметры: как не переобучиться онлайн

У нас уже: Thompson, Dynamic Bandit, Drift Detector, Pareto MC, Bayesian canary. Риск — **over-adaptation**:
параметрическая нестабильность (параметры пляшут от шума) и бандит, который учится на 10 сделках.

Правила, которые обычно ставят у границы (индустрия, обобщение):
1. **Frozen core / adaptive shell.** Никогда не адаптировать размер и max_risk; адаптировать только score-веса и SL/TP
   множители, с шагом ≤ ±10% за обновление, а не «победил в бандите → переключился».
2. **Скорость обучения = f(sample size).** Posterior bandit с приором и лапласовским сглаживанием;
   при N<30 β-prior Beta(2,2), при N>100 — Beta(1,1). Стабильность важнее скорости.
3. **Гистерезис / minimal change.** Переключать параметр, только если uplift > порог (например 95% CI границы или
   Δ score > 0.05): уже есть Bayesian canary — но он про A/B, а не про гистерезис; добавить «deadband»:
   если старый и новый вариант не отличаются значимо — оставить старый (меньше churn).
4. **Правило Стоппы для адаптации**: адаптация должна наблюдаться во времени (temporal reversal / forward test), а
   не только в историческом окне. — [И] Gama et al., «A Survey on Concept Drift Adaptation», CSUR 2014
   (обзор: ADWIN, DDM, Page-Hinkley — Page-Hinkley streaming detector). У нас BOCPD/Drift похожи, но
   BOCPD стоит держать на 1 — сигналы с неустоявшимся `r` ранжируются как шум.

5. **Ограничение на частоту адаптации** — ≤ 1 раз в N сделок и не чаще раз в 6ч (у us «event-driven ≥10 сделок / 6ч» — ок).
   Дополнительно: **max 1 смена per-regime за сутки**.

6. **Regime-dependent sizing**, а не regime-dependent параметры сигнала: если LSTM уверен в «trend down», уменьшить
   risk per cluster; параметры Bollinger оставить. При 7 LONG — это прямо снижает dangerous concentration.

---

## 5. Метрики и мониторинг: добавить дешёвые и важные

Уже есть: Sharpe, Sortino, Calmar, PF, expectancy, MaxDD, WR. **Нет:**
- **MAE/MFE** — сколько в среднем уходит против позиции до разворота vs сколько забирает TP. Если MAE близка к SL
  и MFE стабильно меньше TP — SL слишком широкий/TP слишком далёкий. Формула: $MAE_{avg} = \frac{1}{N}\sum \max_j (Entry - P_j)_+$ для LONG
  (в R-единицах: MAE_R = MAE/ (Entry−SL)); $MFE_R$ аналогично. Правило: **MFE_R < 1.0 в >50% сделок → TP недостижим**.
  Источник: позиции — классическая практика (это стандарт у трейдеров, публикаций нет) → [И, общее знание].
- **Slippage** = (fill − signal price) по стороне, в bp + по времени: `time-to-fill`. При авто-входе market — не ноль.
- **Realized vs expected edge**: фактический WR против предсказанного meta-label (calibration curve).
- **Длительность удержания** (уже есть в composite score), **turnover** и **fees/PnL**.
- **Brier score** для LSTM 82.3% «точности» — accuracy по 5 классам бесполезна, если классы несбалансированы;
  использовать macro-F1 + Brier/log-loss по предсказанным вероятностям режима.
- **Portfolio heat / cluster risk** — ежедневно, как метрика.
- **Funding drag** — сумма funding-платежей/день (у нас есть трекер — выводить в `/metrics`).

Алерты (пороги, примерные, подкрутить): `MAE_MFE` down → авто-настройка SL/TP? **нет**, только отчёт; 7 дней
`CVaR_99 > 1.5×daily_limit` → alerts; `cluster_risk > 2R` → alerts; `slippage_p95 > 20bp` → alerts;
`WR_7d < WR_90d − 2σ` → алерт на дрейф стратегии (а не только на bandit-параметры).

---

## 6. Фьючерс-специфика: что используем/не используем

| Сигнал | У нас | Что делать |
|---|---|---|
| Funding | только алерт при >0.1%/8ч (`HIGH_THRESHOLD`) или <−0.05%/8ч; ранжирования/размера не меняет | модификатор размера (§2.3) + EV для удержания T>24ч |
| Mark vs Last / basis | не используется | basis = (last−mark)/mark; при basis > 0.1% перед funding — не входить (платим за перекос) |
| Open interest | нет | OI + price: OI↑ цена↑ — тренд; OI↓ цена↑ — short squeeze, риск разворота. Data: Bybit /v5/market/open-interest |
| Liquidation levels | используем SL, но не смотрим на «статистику ликвидаций» | Bybit /v5/market/liq — не API, есть только public stream `liquidation`. Если можем — читать поток: всплеск ликвидаций лонгов = стоп-каскад, ожидать бид в SL |
| Funding-arb (long spot / short perp) | нет, и не надо для этого бота | спред по funding между биржами — отдельная стратегия, отмечена в Kraken; на Bybit не проверил |

Одно конкретное: **не вводить LONG в ожидании funding ≥ 0.1%/8ч** при плане держать >1 день: 0.3%/день
торгуется против позиции; при плече 5x это −1.5% маржи/день.

---

## 7. План внедрения по ROI (для 7 коррелированных LONG)

| # | Что | Эффект | Сложность | Файлы |
|---|---|---|---|---|
| **P1** | Cluster risk + portfolio heat (`R_cluster ≤ 2R`), пересчёт при **каждом** входе не по snapshot, а по живому 7д-окну | Убирает главный риск: 7 позиций = 1 ставка; оценка: текущий дефолт 7×$15 = $105 маржи, при кластере даст ~$50-60 | M | `risk_manager.py` (+ `correlation.py` живые данные); 1 быстрый тест |
| **P1** | Two-sided vol sizing: `clip(target/realized, 0.5, 1.0)` (сегодня верх мёртв, сейчас на 7 позициях размеры полные) | Прямо режет экспозицию в стрессе | S | `volatility_filter.py` + тест на `scale<1 при realized>baseline` |
| **P2** | Funding-aware sizing: `hint` от `funding_tracker` → multiplier | Снижает carry drag | S | `funding_tracker.py`, `auto_entry.py` (1 строка) |
| **P2** | Portfolio VaR/CVaR 99% (historical) ежедневно + алерт | Даёт реальный (а не per-symbol) риск-метрик | M | новый `portfolio_risk.py` + `/metrics` |
| **P2** | Purged K-fold + embargo в `validation.py` | Устраняет утечку у triple-barrier при оценке meta-label | M | `experiments/validation.py` (новый `purged_cv()`), sklearn `TimeSeriesSplit`+purge вручную — 40 строк |
| **P3** | MAE/MFE, slippage, fee drag в `lp_report` | Показывает, торгуем ли мы эдж или платим комиссии | S | `lp_report.py` (уже есть trade_history) |
| **P3** | Убрать/подключить Kelly | Сейчас мёртвый код вводит в заблуждение. Проще **удалить** до уровня N≥50 кластера | S | `position_sizing.py` |
| **P3** | Funding/OI/basis в entry judge (не в сигнал) | Даёт фильтр на перегрев | M | `auto_entry.py` |

---

## Ссылки (проверенные через arXiv API / web)

- Bailey, López de Prado (2014), *The Deflated Sharpe Ratio* — J. Portfolio Management; в репо — `validation.py` **[В]**.
- Bailey, Borwein, López de Prado, Zhu (2017), *The Probability of Backtest Overfitting* — CSCV; в репо **[В]**.
- López de Prado (2018), *Advances in Financial ML*, ch. 7 (purged K-fold, embargo) **[И]**.
- arXiv:2608.27734v1 — *What survives honest evaluation?* (2026): DSR+PBO **не** спасают от look-ahead leak; SR 35 leak проходит. **[И]**
- arXiv:2608.23808v2 — *MinervaScore* — статистический «грейд» робастности бэктеста. **[И]**
- arXiv:2407.17645v1 — CPCV в аллокации (Hopfield). **[И]**
- arXiv:2212.06888v5 — *Fundamentals of Perpetual Futures*: funding, no-arbitrage, 60–90%/год отклонения, AR(6) SR 1.8. **[И]**
- Thorp, *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market* — f* =(pb−q)/b; fractional Kelly. **[И]**
- Kraken Learn, *Funding rates as a trading signal* — 0.10%/8ч = >100% годовых, коррекции, но тренды держат weeks. **[И]**
- fortraders / gainium — коррелирующие позиции считать одной; 2×1R при ρ=0.85 ≈ 1.9R. **[И]**
- journalplus / altrady / tradesearcher — half-Kelly ≈ 75% роста при половинном размере; формула (2c−c²). **[И]**
- Basel FRTB — ES 97.5% вместо VaR 99% (обоснование CVaR для лимитов) **[И, не открывал документ]**.

**Не проверено:** leak в `experiments/engine.py`, Bybit liquidation-стрим, смысловое соответствие
`DSR`-annualization, авторские pdf Thorp/FRTB.
**Проверено по коду:** `funding_tracker` пороги `HIGH_THRESHOLD=0.1%`, `LOW_THRESHOLD=-0.05%` (8ч) — алерт only,
размер не меняют → §2.3 актуален.
