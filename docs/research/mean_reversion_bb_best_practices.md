# Mean-reversion на Bollinger Bands: фильтры против «входа в отросшее» (chasing)

Контекст: bybit-ws, Bollinger Grid LONG/SHORT. Детектор: `chasing_momentum [HIGH]` — 21/28 покупок после роста >3%. SHORT убыточен (−$74, WR 68.6%), LONG прибылен (+$249, WR 78.4%).

**Метка доверия:** ✅✅ подтверждено 2+ независимыми источниками · ✅ один надёжный источник · ⚠️ единичное мнение/маркетинг · ❗ не проверено

---

## 0. Главный вывод (сходится у всех)

Твой паттерн — не баг детектора, а классический failure mode: **бот фейдит движение сразу, без подтверждения экстремума и без проверки режима**. Все источники говорят одно:

1. Сам Джон Боллинджер (bollingerbands.com, правило 8): **«Closes outside the Bollinger Bands are initially continuation signals, not reversal signals»**; правило 6 — **«Tags are just tags, not signals»**. Т.е. вход по факту касания/выхода за полосу — против замысла индикатора. ✅→✅✅ (повторено у LuxAlgo и Investopedia)
2. Mean-reversion специально **теряет на трендах** — «wins often but fails hard in trends». ✅✅ (Mubite, LuxAlgo, QuantifiedStrategies, Alchemy)
3. Долгосрочно без regime-фильтра это «death by a thousand cuts» ⚠️ — но факт «fails in trends» не оспаривается никем.

Отсюда приоритет фиксов: **(a) не входить на «растянутом» движении; (b) не входить без подтверждения разворота; (c) резать SHORT-сторону или ставить ей блокирующий режим-фильтр.**

---

## 1. Входные фильтры, которые реально используют

### 1.1 Confirmation candle (свеча-подтверждение) — первый по важности ✅✅
- Правило: цена закрылась за полосой → **ждать следующую свечу**, которая закрылась обратно ВНУТРИ полосы (или хотя бы не дала новый экстремум), и только тогда входить.
- Alchemy Markets: *«Enter when price closes outside the Bollinger Bands, forms a rejection candle, and then moves back inside»*; вход на свече после rejection-свечи, стоп за её low/high. ✅
- LuxAlgo прямо советует протестировать два варианта: «immediate next-bar entry» vs «wait for price to close back inside». ✅
- Bollinger: «W» bottom / «M» top — первая свеча через полосу, вторая — откат, третья — пробой в противоположную сторону, **вход на пробое вторым касанием полосы**. ✅ (bollingerbands правило 5, velotrade/trendspider описывают то же)

**Практический эффект:** это ровно то, что убивает chasing — ты не покупаешь в свече, которая только что сделала +3%, а ждёшь, пока движение остановится. Цена входа хуже, но это осознанный обмен «пусть я войду позже» на «пусть я не вхожу вообще».

### 1.2 RSI / моментум-дивергенция ✅✅
- RSI(14) < 30 для long, > 70 для short — стандарт; но **только в связке с полосой**, сама по себе не сигнал (LuxAlgo: «band condition… RSI… not confirmation that a reversal has started»). ✅✅
- Дивергенция (цена делает новый low, RSI — нет) — популярный фильтр, но подтверждения эффективности в цифрах я не нашёл. ⚠️/❗

### 1.3 Объём как фильтр качества ✅
- LuxAlgo: объём ≥ 1.5× среднего за предыдущие 20 бар (только по *завершённым* барам). ⚠️ единичный источник, но правило воспроизводимое.
- Важно: всплеск объёма на экстремуме = капитуляция/выдох. Монотонный рост объёма *по ходу* движения — наоборот, признак тренда → не входить.

### 1.4 Волатильность-режим (BandWidth) ✅
- Bollinger: BandWidth = 4× coefficient of variation; узкие полосы = сквиз = после него вероятен breakout, а не возврат. Из QuantifiedStrategies: при сжатии полос «band bounce» ненадёжен. ✅
- Практика: измерять `BandWidth / median(BandWidth, 90)`; если <0.5 — сквиз, фейдить нельзя (пусть даже цена у полосы).

### 1.5 Трендовый фильтр ✅✅
- Классика: EMA200 (или 4h EMA200) как долгосрочный тренд. Не фейдить против него: long только если цена не ниже EMA200, short только если не выше. Это стандартный «trend-following vs mean-reversion» комбайн — «develop a trend following/momentum strategy alongside your mean reversion one» (r/algotrading, популярный тезис в двух тредах). ✅ (формулировка — из сниппетов поиска Reddit, полный текст не открывался: ❗ Reddit сейчас отдаёт 403 всем методам)

---

## 2. Как отличать mean-reversion от momentum-режима

### 2.1 ADX — самый практичный фильтр (пороги конкретные) ✅✅
| ADX(14) | Режим | Действие |
|---|---|---|
| < 20–25 | флэт | mean-reversion разрешён |
| 25–40 и растёт | формируется тренд | stand down (не входить) |
| > 40 | сильный тренд | mean-reversion не применять |

Источники: Alchemy («ADX under 20 signaling weak trend strength» для range-bound) и Mubite («ADX below 25: mean reversion viable; above 25 and rising: stand down; above 40: wrong tool»). ✅✅ — порог 20–25 варьируется, но консенсус «<25, а лучше <20».

### 2.2 Hurst / variance ratio — академически красиво, на практике хрупко ⚠️
- Классика: H<0.5 → anti-persistent (MR), H>0.5 → trending, H≈0.5 → random walk (macrosynergy).
- **Главная ловушка (исследование algoseek, 129 акций, 2007–2026):** R/S-оценка Hurst имеет смещение: на *перемешанных* (т.е. гарантированно случайных) рядах она выдаёт H≈0.572, а не 0.5. Т.е. сравнивать с 0.5 нельзя — нужно сравнивать с shuffle-baseline на той же сетке лагов. Плюс Hurst ≡ variance ratio, свёрнутый в одно число, и теряет структуру по горизонтам. ✅ (хорошая методичка, один источник, но с кодом и цифрами)
- Рабочее правило, если всё же использовать (Quantt): окно 250–500 баров, MR включать только при **H < 0.45**, trend — при H > 0.55, зона 0.45–0.55 = stand down. ⚠️

### 2.3 Variance ratio (Lo–MacKinlay) ✅
- VR(q) = Var(q-периодных) / (q·Var(1-периодных)). <1 → возврат к среднему, >1 → тренд. Профиль VR по горизонтам q=2,5,10,20 важнее одного числа. Считать только если есть чем проверить (Newey–West / overlapping SE) — иначе это шум. ✅ (algoseek + quant.SE «Using variance ratios to test for mean reversion»)
- **Практический вывод для бота:** на Bybit 15m/1h VR оценка будет шумной. Не строй на нём гейт в одиночку.

### 2.4 Autocorrelation / half-life (OU) — дешёвый и полезный ⚠️
- lag-1 autocorr цены (не возвратов) на окне 100–200 баров: устойчиво отрицательная ≈ MR. 
- Half-life = ln(2)/λ от AR(1)/OU-фита; если ожидаемое время возврата больше твоего max holding — не торгуй. quant.SE 526 (accepted answer): *«no standard method… prefer the simpler approaches unless complexity can be justified… calibration is context sensitive»*. ✅

### 2.5 HMM/кластеризация ⚠️
- Упоминается как способ regime detection, но конкретных проверенных порогов не нашёл. Не рекомендую как первый шаг.

**Рекомендация:** ADX(14) как основной гейт + EMA200 как направление + автоматически вычисляемый `chasing`-гейт (ниже). Hurst/VR — только в исследовательский скрипт, не в прод.

---

## 3. Борьба с chasing — конкретные правила

Твой сигнал: 75% покупок после роста >3%. Ниже — правила, которые бьют ровно в это.

### 3.1 Жёсткий «anti-chase» гейт по ретроспективной доходности ✅ (логика из LuxAlgo/roc-фильтров; конкретные числа — мои калибровки, ❗ не из исследования)
```
r_1h  = close/close[1h назад]-1
r_4h  = close/close[4h назад]-1
r_24h = close/close[24h назад]-1

LONG  запрещён, если r_4h > +2.0% ИЛИ r_24h > +5.0%   (цена уже отработала движение)
SHORT запрещён, если r_4h < -3.0% ИЛИ r_24h < -8.0%   (не шортить обвал)
```
Сейчас у тебя 3% — порог слишком низкий для крипты с 15m барами (3% — это 1 нормальный бар BTC). Порог должен быть функцией ATR: `r_4h > 1.5 × ATR%(4h×4)` — адаптивно к волатильности.

### 3.2 Проверка «растянутости» в терминах самих полос ✅
```
z = (close - SMA20) / std20        # это ровно %B*2-1
```
- LONG только при `z <= -2.0` **и** `z_prev > z` (расстояние сокращается). Условие «полоса уже отработана» — вход не в первой свече касания, а когда z перестал ухудшаться.
- LONG **запрещён** при `z <= -3.0` (там уже не возврат, а тренд/крах; LuxAlgo сознательно ограничивает диапазон −3…−2, и велит отдельно определять поведение «beyond 3σ»). ✅
- SHORT **запрещён** при `z >= +3.0`.

### 3.3 Ожидание отката, а не входа в импульс ✅✅
Это подтверждается всеми: «close outside is continuation», «enter when price moves back inside». Реализация:
- состояние `ARMED` при |z| ≥ 2 → вход только когда **закрытие снова внутри полосы**, либо `z` вернулся к ±1.8.
- таймаут: если в `ARMED` 3 свечи и откат не наступил → снять выставление (это, как правило, тренд).

### 3.4 Запрет «докупки в уже отросшее» ✅
- Mubite: *«an adverse move can look like a stronger signal, tempting you to add to a loser. Do not add blindly»*; LuxAlgo: не усредняться без явного бюджета риска. ✅
- Если у тебя grid — правило: **следующий уровень сетки открывать только если цена НЕ выросла относительно предыдущего входа** (для LONG) и количество уровней ограничено (2–3). Иначе сетка = 100% chasing по определению.

### 3.5 Anti-chase по времени суток/событиям ⚠️
- Не входить в первые N минут после резкого движения > 2 ATR (импульс/ликвидации). LuxAlgo: «gap cancels the setup».

---

## 4. Почему SHORT хуже — и что с этим делают

Это ключ к твоей −$74 на SHORT при 68.6% WR: **выигрываешь часто, но редкие потери крупнее** (типичный профиль MR: 65–80% WR + fat left tail ✅✅ — Mubite, LuxAlgo с примером 70%×0.5R − 30%×2R = −0.25R ожидания).

Причины асимметрии в крипте:
1. **Long leg >> short leg** ✅ — stephencox.net (обзор литературы): «profitability … heavily skewed toward the long leg; past losers experience massive, rapid, unpredicted rebounds … short-squeezes inflicting catastrophic losses on the short leg». Тот же вывод для momentum — и он применим к контр-стратегиям: шорт «победителей»/отросших — это как раз ловля short-squeeze.
2. **Funding** ✅ — на перпетуалах положительный funding = лонги платят шортам (шорт получает). defirate (11 окт 2026): BTC ~+3.4% годовых weighted, +8.5% simple, положительный на 10 из 11 бирж → фандинг **не** главный убийца SHORT, скорее небольшая помощь. Но при перегретых альткоинах funding взлетает — и это наоборот сигнал, что шорт *переполнен* (короткий сквиз). coinglass: положительный funding = «market bias toward longs», «prolonged high positive funding rates often signal buildup of potential reversal risk».
3. **Структурный дрейф вверх** ⚠️ — крипто в бычьей фазе имеет положительный дрейф; Quantpedia: BTC «trends at the max and bounces at the min» (мостик к тому, что верх — продолжение, низ — возврат). ✅ для BTC 2015–2022.
4. **Асимметрия волатильности** ⚠️ — рост часто вертикальный (ликвидации шортов каскадом → ускоряющийся памп), падение — более ступенчатое. Из stephencox (liquidation cascade → forced buying → ещё выше).

### Что с этим делают (реальные приёмы)
- **Long-only MR** или **асимметричные пороги**: LONG — z≤−2.0, SHORT — z≥+2.5 (или 2.8) и только при ADX<20 и funding выше нормы. ✅ (асимметричные пороги — расхожая практика; конкретно 2.5 — моя рекомендация, ❗)
- **Меньший размер на SHORT**: risk per trade SHORT = 0.5× от LONG. Простой и работает: у тебя SHORT и так убыточен, его надо резать, а не оптимизировать.
- **Профилактика short-squeeze**: запрет шорта при `funding > 0.05%/8h` (перегретый лонг — НЕ значит «шорти»; это часто *продолжение* памп-а), запрет шорта при `ADX(1h) > 30 и растёт`, запрет шорта в первые 48ч после пробоя ATH. ⚠️
- **Стоп для SHORT жёстче**: 1.2×ATR против 2×ATR для LONG. Причина — хвост.
- **Не шортить топ-адвансеров дня**: колонка `funding + % change 24h` — если симв в топ-10 по 24h росту, SHORT запрещён. ⚠️ логика из «don't fade the leader».

---

## 5. Параметры BB, удержание, выход

### 5.1 Период/множитель ✅✅
- База: **SMA20, 2σ**. Bollinger: «defaults are just that, defaults»; для сохранения охвата при SMA10 → 1.9σ, при SMA50 → 2.1σ (правило 11). ✅ (официальный источник)
- **2.0 vs 2.5**: 2.0 — больше сигналов, больше ложных на трендах; 2.5–3.0 — реже, но «крайности» реже = более статистическая аномалия. QuantifiedStrategies: на сквизах и в тренде 2.0 даёт много head-fake, рекомендуют 2.5–3 при высокой волатильности. ⚠️
  - **Практика:** на крипте высокая волатильность → 2.0 на 15m/1h работает только с фильтрами; 2.5 на 4h/D — «чище». Мой выбор для бота: **2.2–2.5 на 1h/4h**, 2.0 только на 15m с confirmation candle.
- **25 vs 20**: 25 в крипте чуть сглаживает; разница небольшая ⚠️.
- Bollinger: **90%, а не 95%** данных внутри 2σ на дефолте (правило 14) — не строй маркетинг «95% времени» на вероятности.

### 5.2 Удержание и выход ✅✅
- **Target = middle band (SMA)** — самая частая цель; часто рекомендуют закрывать **1–1.5σ** (частично) и остаток на mean (LuxAlgo: «exits part of the trade as price returns within 1–1.5 standard deviations and closes the remainder at the mean»). ✅
- **RSI-нормализация**: выход при RSI 40–60 (Mubite). ✅
- **Time stop**: 5–10 бар (Mubite, Quantvero — 10 периодов). Это критично: *«A reversion that does not come quickly often is not coming»*. ✅✅
- **Стоп**: 1–2 ATR за экстремумом входа (Mubite: «1 to 2 ATR beyond the entry extreme»; LuxAlgo: 1.5×ATR — **исследовательский выбор**, пересчитывать сайз под дистанцию). ✅
- **Не тащить stop**: LuxAlgo — расширение стопа при том же qty увеличивает плановый риск.

### 5.3 Ожидание vs win rate ✅
LuxAlgo даёт числовой пример: WR 70%, avg win 0.5R, avg loss 2R → expectancy −0.25R. Твой SHORT: WR 68.6% и минус — **ровно этот профиль**. Проверь у себя `avg_win_R` и `avg_loss_R`; если avg_loss > 2×avg_win — фиксить надо не win rate, а стоп/фильтр входа.

---

## 6. Чек-лист для внедрения (в порядке отдачи/усилий)

**Этап 1 — сразу (малый diff, большой эффект)**
- [ ] `ARMED→CONFIRM` вместо входа по касанию: вход только на закрытии обратно внутри полосы (confirmation candle). → убирает большинство chasing
- [ ] Anti-chase гейт: `LONG blocked if r_4h > 1.5×ATR%(4h)`, `r_24h > 5%`; `SHORT blocked if r_4h < -3%`, `r_24h < -8%` (пороги откалибровать бэктестом)
- [ ] `z` (или %B) порог: LONG только при −2.5 ≤ z ≤ −2.0 и `z_prev > z`; SHORT только 2.0 ≤ z (лучше 2.5)
- [ ] Time stop: 5–10 бар от входа → закрытие. Логировать «отсутствие возврата»
- [ ] Фиксированный риск на сделку; SHORT = 0.5× риска LONG

**Этап 2 — режим-фильтры**
- [ ] ADX(14) на рабочем ТФ: `ADX < 25` разрешает MR; `> 30` и растёт — блок. Особенно жёстко для SHORT (`ADX>40 → SHORT запрещён`)
- [ ] EMA200 направление: не лонговать ниже EMA200, не шортить выше EMA200 (на 4h)
- [ ] SHORT-блоки: `funding > 0.05%/8h` + `symbol in top-10 by 24h change` → нет SHORT (риск сквиза)

**Этап 3 — качество и калибровка**
- [ ] BandWidth percentile: если BandWidth < 30-й перцентиль (сквиз) — bounce не фейдить
- [ ] Объём: фильтр ≥1.5× MA20 на экстремуме (только для подтверждения выдоха)
- [ ] Выход: 50% на 1σ, 50% на mean; либо RSI 40–60
- [ ] Метрики в журнал: `avg_win_R`, `avg_loss_R`, `chasing_flag`, `ADX_at_entry`, `time_in_trade` → сравнить expectancy до/после по каждой группе
- [ ] A/B на истории: baseline vs +anti-chase vs +ADX vs +confirmation (по одному фильтру за раз)

**Не делать**
- [ ] Не строить гейт на Hurst/VR без shuffle-baseline и статистической значимости
- [ ] Не усредняться/докупать без ограниченного бюджета (grid по определению = chasing)
- [ ] Не оптимизировать SHORT до прибыльности — сначала резать/блокировать

---

## 7. Источники

- **Bollinger Band Rules (John Bollinger, официально)** — https://www.bollingerbands.com/bollinger-band-rules — правила 6,7,8,9,11,14 ✅✅
- **LuxAlgo «Mean Reversion Trading: Fading Extremes with Precision»** — https://www.luxalgo.com/blog/mean-reversion-trading-fading-extremes-with-precision/ — BB+RSI+volume, z −3..−2, confirmation, expectancy пример ✅
- **LuxAlgo «Mean Reversion Strategies for Algorithmic Trading»** — https://www.luxalgo.com/blog/mean-reversion-strategies-for-algorithmic-trading/
- **Mubite «Mean Reversion Crypto Strategy»** — https://mubite.com/en/crypto-reports/mean-reversion-strategy — ADX<25, 1–2 ATR stop, time exit 5–10 bars, «do not add blindly» ✅
- **Alchemy Markets «Mean Reversion Explained»** — https://alchemymarkets.com/education/strategies/mean-reversion/ — rejection candle entry, ADX<20 для range, TP на middle band ✅
- **QuantifiedStrategies «12 Bollinger Bands Strategies»** — https://www.quantifiedstrategies.com/bollinger-bands-trading-strategy/ — бэктесты, M/W, band walks ✅
- **Quantvero «Mean Reversion Trading Strategy»** — https://www.quantvero.com/algo-trading/mean-reversion-trading-strategy/ — 10-бар time stop, 3σ stop ✅
- **algoseek «Mean Reversion vs Momentum and How to Identify Regimes»** — https://algoseek.com/blog/mean-reversion-vs-momentum-and-how-to-identify-regimes/ — VR, Hurst bias (shuffle floor 0.572), смысл по горизонтам ✅
- **Quantt «Hurst Exponent Explained»** — https://www.quantt.co.uk/resources/hurst-exponent-explained — окно 250–500, H<0.45 MR / >0.55 trend / 0.45–0.55 stand down ⚠️
- **macrosynergy «Detecting trends and mean reversion with the Hurst exponent»** — https://macrosynergy.com/research/detecting-trends-and-mean-reversion-with-the-hurst-exponent/
- **quant.SE 526** — https://quant.stackexchange.com/questions/526/is-there-a-standard-method-for-quantifying-mean-reversion-for-use-in-directional — нет стандарта, брать простое, калибровка контекстная ✅
- **quant.SE 7666** — https://quant.stackexchange.com/questions/7666/using-variance-ratios-to-test-for-mean-reversion
- **Quantpedia «Trend-following and Mean-reversion in Bitcoin»** — https://quantpedia.com/trend-following-and-mean-reversion-in-bitcoin/ — BTC trends at MAX, reverts at MIN ✅
- **stephencox.net «Dynamics of Cryptocurrency Price Momentum»** — https://stephencox.net/projects/the-dynamics-of-cryptocurrency-price-momentum/ — long leg >> short leg, short-squeeze tail, liquidation cascade ✅
- **defirate BTC funding** — https://defirate.com/perp/bitcoin/ — +3.4% APY weighted, positive на 10/11 бирж (11 окт 2026) ✅
- **coinglass BTC funding** — https://www.coinglass.com/FundingRate/BTC — определение bias ✅

**Не проверено / ограничения:**
- Reddit r/algotrading: все треды (`zy7da8`, `g3st1q`, `1cwsco8`, `ha27wi`) отдают **403** всем методам (curl, .json, old.reddit, r.jina.ai, web_extract). Цитаты по ним взяты только из сниппетов поисковика — **формулировки подтверждены только косвенно** ❗. Если нужно — проверить вручную через браузер с логином.
- Числа порогов anti-chase (2%/5%) — **калибровка, не исследование** ❗. Обязательно гонять A/B на своих данных.
- Статистика «72% WR / 2.11 Sharpe» из lunefi/reddit — маркетинг, источник сомнительный ❗, не использовал в выводах.
