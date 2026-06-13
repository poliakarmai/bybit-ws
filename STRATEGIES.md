# Bybit Bollinger Grid Monitor — Стратегии и Roadmap

> **Версия:** 3.10 | **Дата:** 13.06.2026 | **Автор:** Alexey Polyakov
>
> Полный свод всех торговых стратегий bybit-ws. От базового Bollinger Grid 3x до экстремальных x10.
> Этот документ — для AI-агентов и людей: описание, параметры, условия входа, риск-менеджмент.

---

## 1. Обзор стратегий

```
СТРАТЕГИЯ              ПЛЕЧО   ТФ      ВХОД                      SL       TP         ПОЗИЦИЙ
──────────────────────────────────────────────────────────────────────────────────────────
Bollinger Grid LONG     3x     Daily   Lower BB −3%             −7%      Middle+Upper 12
Bollinger Grid SHORT    3x     Daily   Upper BB +2%, BB>85%     +5-7%    Middle BB     3
Junk-шорт               3x     Daily   Рост ≥80%, BB>70%        15% loss Middle BB     2
SL Re-entry             3x     Daily   Lower BB после SL        −7%      Middle BB     на монету
DCA (лесенка)           3x     —       −5/−10/−15% от входа     общий    общий         2 добавки
BB Scalping M5      ⚡ 10x     M5      Касание BB + RSI         3%       Middle BB     3
Mean Reversion Ext   ⚡ 10x     Daily   BB% <5% / >95%           5%       Middle BB     5
Funding Momentum     ⚡ 10x     Daily   Фондинг ±0.1% + BB+тренд 4%       Middle BB     3
```

⚡ = стратегии с плечом x10 — высокий риск, высокий потенциал.

---

## 2. Bollinger Grid LONG (основная, 3x)

### Параметры

| Параметр | Значение |
|----------|---------|
| **Плечо** | 3x |
|| **Маржа** | Динамическая (% депозита, position_sizing v3.8) |
| **Вход** | Лимитный ордер Buy на −3% ниже Lower BB Daily |
| **TP** | 20% позиции на Middle BB, 80% на Upper BB |
| **SL** | −7% от Lower BB |
| **Макс позиций** | 12 (risk.max_long_positions) |
| **Cooldown после SL** | 4 часа |
| **Ограничения** | M5/M3 BB width > 100% → вход блокируется |

### Scoring (0-10)

```
Метрики:
  Tier-бонус ×2.0     S=10, A=7, B=4, C/D=1
  BB% положение ×1.5  0% = нижняя полоса, идеал <30%
  Объём 24ч ×1.0      log-нормализация
  Дней падения ×1.0    до 7 дней подряд
  Недельный BB% ×1.0
  Месячный BB% ×1.0
  Фандинг ×0.5         отрицательный = бонус
  RSI(14) ×1.0         <30 = 10, >70 = 0
  BB Squeeze ×1.0

Мин порог: 5.5/10
```

---

## 3. Bollinger Grid SHORT (хедж, 3x)

| Параметр | Значение |
|----------|---------|
| **Плечо** | 3x |
| **Маржа** | $10 |
| **Вход** | Лимитный Sell на +2% выше рынка |
| **TP** | Middle BB (через takeProfit в trading-stop) + трейлинг-TP для JUNK (junk_trail.py) |
| **SL** | +5% (Tier A/B), +7% (Tier C/D). JUNK: без SL |
| **Порог BB** | >85% Daily |
| **Макс позиций** | 3 |
| **Макс удержание** | 72 часа |
| **Доля шортов** | ≤20% от всех позиций |
| **ONE_WAY исключения** | XRP, ONDO, WLFI, ENJ, ESPORTS, AVAX, APT, SUI |

---

## 4. Шлак-шорт (Junk Mode, 3x) — v3.10

**Новое в v3.10:** трейлинг-TP (junk_trail.py), недельные пампы (≥230% за 7д). Из v3.7: hard stop, авто-закрытие.

| Параметр | Значение |
|----------|---------|
| **Плечо** | 3x |
| **Маржа** | $10 |
| **Триггер** | Дневной рост ≥80% **И** BB Daily >70% |
| **Вход** | Лимитный Sell на +2% выше рынка |
| **Hard stop** | **−15% убытка по марже** — market-close |
| **Max hold** | **48 часов** — авто-закрытие |
| **DCA-уровни** | +100% и +120% от входа |
| **Макс позиций** | 2 |
| **Трейлинг-TP** | junk_trail.py: фиксация 70% профита при +15%, 85% при +30% |
| **Недельный памп** | Рост ≥230% за 7д + оборот ≥$1M → market SHORT, без SL/TP, макс 2 |

### Конфиг

```yaml
strategy:
  junk:
    enabled: false           # выключен по умолчанию
    min_pump_pct: 80
    dca_levels: [1.0, 1.2]
    max_loss_pct: 15         # NEW: hard stop
    max_hold_hours: 48       # NEW: авто-закрытие
    max_positions: 2
```

---

## 5. SL Re-entry (лесенка)

| Параметр | Значение |
|----------|---------|
| **Триггер** | SL + score ≥6 |
| **Задержка** | 4 часа |
| **Вход** | Текущий Lower BB Daily |
| **Маржа** | ×0.5 от предыдущей |
| **Максимум** | 2 re-entry за 24 часа |

---

## 6. DCA (лесенка усреднения)

| Уровень | От входа | Множитель маржи |
|---------|----------|-----------------|
| 1 | −5% | $10 |
| 2 | −10% | $20 |
| 3 | −15% | $40 |

- max_margin_per_symbol: $80
- max_dca_count: 2 добавки

---

## 7. ⚡ BB Scalping M5 (x10) — v3.7

**Новое в v3.7:** correlation check — не более 2 связанных позиций. X10 дневной лимит убытков.

| Параметр | Значение |
|----------|---------|
| **Плечо** | 10x |
| **Маржа** | $10 |
| **Таймфрейм** | M5 |
| **LONG** | Цена у Lower BB M5 + RSI(14) < 35 |
| **SHORT** | Цена у Upper BB M5 + RSI(14) > 65 |
| **SL** | 3% от входа |
| **TP** | Middle BB M5 |
| **Макс позиций** | 3 |
| **Кулдаун** | 1 час |
| **Корреляция** | Блокируется при ≥2 связанных позиций |

---

## 8. ⚡ Mean Reversion Extreme (x10) — v3.7

| Параметр | Значение |
|----------|---------|
| **Плечо** | 10x |
| **Маржа** | $10 |
| **Таймфрейм** | Daily |
| **LONG** | BB% < 5% |
| **SHORT** | BB% > 95% (кроме ONE_WAY) |
| **SL** | 5% |
| **TP** | Middle BB Daily |
| **Макс позиций** | 5 |
| **Корреляция** | Блокируется при ≥2 связанных позиций |

---

## 9. ⚡ Funding Rate Momentum (x10) — v3.7

**Новое в v3.7:** тренд-фильтр — для SHORT цена должна падать 3 дня.

| Параметр | Значение |
|----------|---------|
| **Плечо** | 10x |
| **Маржа** | $10 |
| **LONG** | Фондинг < −0.1% + BB% < 15% |
| **SHORT** | Фондинг > +0.1% + BB% > 85% + **тренд падает 3 дня** |
| **SL** | 4% |
| **TP** | Middle BB Daily |
| **Макс позиций** | 3 |
| **Кулдаун** | 4 часа |

---

## 10. X10 Risk Limits (v3.7 — НОВОЕ)

Защита от каскадных потерь на высоком плече:

| Параметр | Значение |
|----------|---------|
| **max_daily_losses** | 3 — стоп ВСЕХ x10 после 3 убытков |
| **cooldown_after_stop_hours** | 24 — пауза на сутки |
| **require_atr_validation** | true — обязательная ATR-проверка |
| **max_position_risk_pct** | 2.0% — макс риск на позицию |

### Как работает
1. Каждое x10-закрытие записывается в `x10_limits.json`
2. При 3 убыточных сделках → ВСЕ x10-стратегии блокируются
3. Через 24 часа → авто-разблокировка
4. Новый день → счётчик сбрасывается

---

## 11. ATR Risk Sizing (защитный слой)

```
ATR = Average True Range(14) на 15-минутных свечах
SL_distance = 1.5 × ATR
SL_distance_pct = SL_distance / price
Risk_USDT = Balance × 1%
Max_Margin = Risk_USDT / (SL_distance_pct × Leverage)
Qty = Max_Margin × Leverage / Price
```

- `validate_entry()` — блокирует вход если маржа > 1.5× безопасной
- `check_position_risk()` — алерт если риск > 2% от баланса

---

## 12. Интеграция в главный цикл (v3.7)

```
Каждые 30 сек (лёгкий цикл):
  fetch_positions / fetch_orders → detect_changes → SL/TP/DCA/риски

Каждые 5 мин (HEAVY_CYCLE):
  overbought → auto_short → sl_reentry → junk_dca (+max_loss +max_hold)
  → correlation (+dedup 24ч) → pump_detect → squeeze → rsi → regime

Каждые 10 мин (X10 стратегии):
  x10_entry_allowed? → баланс → correlation snapshot
  ├── scalp_signals → correlation_filter → validate → execute → track_x10
  ├── mean_revert   → correlation_filter → validate → execute → track_x10
  └── funding_signals → correlation_filter → validate → execute → track_x10
  + check_position_risk (все позиции)
```

### Correlation check для x10 (НОВОЕ)
Перед каждым x10-входом проверяется корреляционная матрица.
Если у символа уже ≥2 связанных позиций (r > ±0.8) → вход блокируется.

### X10 трекинг (НОВОЕ)
- При входе: `track_x10_entry(sym, strategy)` → `x10_positions.json`
- При закрытии: `get_x10_strategy(sym)` → `record_x10_trade(strat, pnl)`
- Стратегия пишется в трейд-журнал (trades.md + trades.jsonl)

---

## 13. Риск-менеджмент (общий)

| Параметр | Значение |
|----------|---------|
| max_drawdown_pct | 15% от пика |
| max_total_margin | $500 |
| max_daily_loss | $50 |
| max_long_positions | 12 |
| max_per_sector | 3 |
| emergency_close_all | true |

### Защиты
- **Каскадная ликвидация:** mark ближе к liq чем к SL ×0.5 → market-close
- **Лимит шортов:** ≤20% от всех позиций
- **Correlation stop:** LONG >80% → LONG-входы блокируются
- **X10 стоп:** 3 убытка → пауза 24ч (НОВОЕ)
- **Junk hard stop:** −15% маржи → market-close (НОВОЕ)

---

## 14. Трейд-журнал (v3.7)

Все закрытые сделки пишутся в:
- `trades.md` — markdown-таблица с колонкой **Стратегия**
- `trades.jsonl` — JSONL с полем `strategy`

### Формат
```
| Дата | Монета | Сторона | Вход | Выход | PnL | Причина | Стратегия |
```
Стратегии: `bb_long`, `bb_short`, `junk_short`, `scalp`, `mean_revert`, `funding_momentum`, `dca`

---

## 15. Дашборд и мониторинг

### RPC эндпоинты (порт 8766)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | /health | Статус, циклы, аптайм |
| GET | /positions | Позиции + PnL |
| GET | /orders | Активные ордера |
| GET | /metrics | SL/TP/entries за день |
| GET | /signals | LONG + SHORT кандидаты |
| GET | /report?period=daily | Сводка PnL + комиссии + фандинг |
| POST | /scan | Скан рынка |
| POST | /enter | Вход (confirm: false для превью) |
| POST | /close | Закрыть позицию |
| POST | /pause | Пауза авто-входов |
| POST | /resume | Возобновить |

---

## 16. Стейт-файлы (v3.7)

```
~/.local/share/bybit-ws/
├── events.log              — основной лог (ротация 50MB × 7)
├── trades.jsonl            — журнал сделок (+strategy поле)
├── x10_limits.json         — дневной лимит x10 убытков
├── x10_positions.json      — трекинг x10 позиций (символ → стратегия)
├── scalp_state.json        — кулдауны скальп-входов
├── mean_revert_state.json  — кулдауны mean-revert
├── funding_entry_state.json — кулдауны funding
├── atr_cache.json          — кеш ATR (30 мин)
├── corr_dedup.json         — дедупликация корреляций (24ч)
├── short_positions.json    — стейт шлак-шортов
└── ...
```

---

## 17. Roadmap

### 🔴 Высокий приоритет (v4.0)

| # | Фича | Статус | Обоснование |
|---|------|--------|-------------|
| 1 | **Бэктестинг на исторических данных** | План | Валидация стратегий без риска |
| 2 | **Мульти-аккаунт** | План | Разделение LONG/SHORT по субаккаунтам |
| 3 | **Веб-дашборд** (Streamlit/Grafana) | План | Визуализация PnL, позиций, метрик |
| 4 | **ML-скор** — адаптивные веса | План | Рынок меняется, фиксированные веса устаревают |
| 5 | **WebSocket вместо REST polling** | План | Мгновенные данные, меньше rate limits |
| 6 | **Trailing Stop для x10** | План | Защита прибыли при волатильности |
| 7 | **Поддержка нескольких бирж** | План | Bybit + Binance + OKX |
| 8 | **asyncio вместо потоков** | Отложено (Manus) | Меньше оверхеда, чище код |
| 9 | **SQLite для состояния** | Отложено (Manus) | Атомарность, консистентность вместо JSON |
| 10 | **Модульные тесты** | Отложено (Manus) | Стабильность при изменениях |

### 🟡 Средний приоритет (v4.1)

| # | Фича | Статус | Обоснование |
|---|------|--------|-------------|
| 8 | **Авто-фандинг-ротация** | План | Flip LONG↔SHORT при смене ставки |
| 9 | **Prometheus-метрики** `/metrics` | План | Интеграция с Grafana |
| 10 | **Partial TP** (50% Middle, 50% Upper) | План | Гибкий выход |
| 11 | **Paper-trading режим** | План | Тестирование без реальных денег |
| 12 | **Уведомления в Discord/Slack** | План | Не только Telegram |

### 🟢 Низкий приоритет (v4.2+)

| # | Фича | Статус |
|---|------|--------|
| 13 | Spot-поддержка | Идея |
| 14 | Мобильное PWA-приложение | Идея |
| 15 | Copy-trading для AI-агентов | Идея |
| 16 | Интеграция с TradingView | Идея |
| 17 | Авто-оптимизация через Optuna | Идея |

### ✅ Сделано (v3.6 → v3.7)

| # | Фича | Версия |
|---|------|--------|
| ✓ | Модульная архитектура, авто-SL/TP/DCA | v3.0 |
| ✓ | YAML-конфиг + REST API + Docker + SDK | v3.3 |
| ✓ | RPC auth, risk limits, graceful shutdown | v3.4 |
| ✓ | DCA-лимиты, каскадные ликвидации, LONG cooldown | v3.5 |
| ✓ | MCP Server для AI-агентов | v3.6 |
| ✓ | Шлак-режим SHORT | v3.6 |
| ✓ | x10 Strategy Pack (4 стратегии) | v3.6 |
| ✓ | Корреляционная матрица + dedup 24ч | v3.6 |
| ✓ | x10 daily loss limit + cooldown 24ч | **v3.7** |
| ✓ | Junk hard stop (15% loss) + max_hold 48h | **v3.7** |
| ✓ | Correlation check для x10 стратегий | **v3.7** |
| ✓ | Funding trend filter (3-дневный тренд) | **v3.7** |
| ✓ | Strategy tag в трейд-журнале | **v3.7** |
| ✓ | Thread stack 2MB (экономия памяти) | v3.6 |

---

## 18. Ссылки

- **DESIGN.md** — архитектура, API, конфиг, деплой, безопасность
- **SKILL.md** (bybit-trading) — полный гайд для Hermes с pitfalls
- **openapi.yaml** — OpenAPI 3.0 схема REST API
- **bybit_ws_sdk.py** — Python SDK
