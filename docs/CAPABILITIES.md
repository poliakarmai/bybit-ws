# bybit-ws — Bollinger Grid трейдинг-монитор v11.5.x

> ✅ Сверено с кодом 2026-10-25 (`entry_judge.py`, `config.py`, env сервиса, `rpc.py`).
> Что именно изменилось — `CHANGELOG.md` §11.5.0. Карта документации — [INDEX.md](INDEX.md).

Автоматическая торговля фьючерсами Bybit. 24/7 systemd-сервис. SQLite WAL (SSOT).
Документация: [ARCHITECTURE.md](ARCHITECTURE.md) (внутреннее устройство), [API.md](API.md) (эндпоинты), [ROADMAP.md](ROADMAP.md) (план).

## 1. Стратегия

Bollinger Grid: LONG при цене ниже нижней BB-полосы, SHORT при цене выше верхней.
`BB(20, 2.0)` по умолчанию, адаптация под сессию.

## 2. Decision Tree входа

```
BB-сигнал (score ≥ min_score)
 ├─ MTF Confluence (D+W+M)     → fail-open: skip filter, -10% score
 ├─ Orderbook Imbalance         → fail-open: skip filter, -10% score
 ├─ Volume Confirmation         → fail-open: skip filter, -10% score
 ├─ Entry Judge (LLM)           → fail-closed: БЛОК
 ├─ Correlation                 → fail-open: skip filter, -10% score
 ├─ Post-trade cluster          → fail-open: skip filter, -10% score
 └─ Risk Manager (CB/margin)    → fail-closed: БЛОК
                                  ↓
                               ВХОД
```

## 3. Entry Judge — hard gate

| Параметр | Значение |
|----------|---------|
| Цепочка | **одна модель**: прямой `api.deepseek.com` через `~/.hermes/scripts/cross-model-judge.py` (`DEEPSEEK_JUDGE_MODEL`, по умолчанию `deepseek-chat`). Qwen/Nemotron/OpenRouter убраны 17.10 |
| Режим | fail-closed; ответ читается как `content or reasoning_content` |
| Таймаут | **20s** (`JUDGE_TIMEOUT`; 5s резало живые вызовы) |
| Prompt-mode | `--mode signal` (оценивает качество входа, а не «полноту артефакта»), `pass` разрешён явно |
| Все упали | revise (блок) |
| Judge CB | 3 падения → отключение на 1ч |
| Кэш | 300s TTL, ключ включает `sl_price:tp_price` |
| Включён | `BYBIT_ENTRY_JUDGE_ENABLED=1` (прод) |

## 4. Auto-SL — ATR-adaptive

Живой контур: `main_async.py` → `unified_sl.manage_sl` (5 модулей слиты, приоритет
tight > simple > hard > BE > auto > default). `auto_sl.py` — **legacy**, подключён только к
`main.py`, сервисом не используется.

**Default SL:** 1.5×ATR(14), расстояние clamp **3–10%** от входа, fallback −10% (`unified_sl.py:215-233`).

**Trailing (hard trail):** по недельному BB — LONG при `bb_pos > 75`, SHORT при `bb_pos < 25`;
SL = `entry ± 0.15 × (mark − entry)` (`TRAIL_SL_PERCENT = 0.15`), двигается только в сторону прибыли.

## 5. Auto-TP — ATR-adaptive (3 ноги)

| Нога | % размера | Действие |
|------|-----------|---------|
| Leg 1 | 40% | Закрыть 40%, SL → breakeven |
| Leg 2 | 35% | Закрыть 35%, SL подтянуть |
| Leg 3 | 25% | Закрыть остаток |

Сплит `ATR_TP_SPLITS = [0.40, 0.35, 0.25]` (3 ноги, % от размера), множители ATR масштабируются по режиму рынка. BB-сплит 20%/80% — fallback, когда ATR недоступен (guard `not bb_used`). `BYBIT_ATR_TP_ENABLED` по умолчанию ON (`'1'`). PERM_SKIP: time-decay 24ч.

## 6. DCA (Dollar Cost Averaging)

| Параметр | Значение |
|----------|---------|
| Условие | Просадка до уровня: **−10% / −20% / −30%**, множитель маржи **1.5 / 2.0 / 2.5** (`dca.py:35-44`) |
| Максимум | **2 DCA** на позицию (`max_dca_count`) |
| Маржа | база $10, `max_pos_value` $40, `max_margin_per_symbol` $80 |
| Volume-фильтр | **нет** в коде (проверено 26.10: ни `vol`, ни `sma` в `dca.py`) |
| SL после DCA | **не ставится** в `dca.py` |

## 7. Pump Detection

| Уровень | Критерий | Действие |
|--------|---------|---------|
| Early warning | Volume spike >5×SMA(20) + price >20%/1h | алерт, не шортить |
| JUNK | +80% за день (`PUMP_THRESHOLD = 0.80`) | SHORT с SL 5% (`sl_tier_cd`), `max_loss_pct` 15%, `max_hold_hours` 48 |

JUNK-circuit-breaker («2 закрытия по SL → off на 24ч») **в коде отсутствует** (проверено 26.10).
Похожий по смыслу механизм — `should_disable_short(window=30, min_pf=1.0)` в `auto_short.py:341`,
но это фильтр по profit factor, а не JUNK-CB.

> `check_junk_dca` — legacy (DCA-логика шлака), не вызывается из live-цикла.

## 8. Black Swan — alert-only (без авто-закрытия)

BlackSwan **только оповещает**, позиции сам не закрывает.

| Триггер | Действие |
|---------|---------|
| ≥5 красных позиций **И** суммарный uPnL < −$150 | алерт с вопросом «Закрыть красные позиции?» (ждёт ответа пользователя) |

Авто-закрытие — только через RPC `/emergency_close` или `/kill_switch` (`emergency_close_all()` вызывается только оттуда). Пороги: `BLACKSWAN_MIN_RED_POSITIONS = 5`, `BLACKSWAN_TOTAL_UPNL_THRESHOLD = -150` (комментарий «не закрытие!»).

## 9. Self-Learning

Event-driven: ≥10 сделок **и** ≥6ч с прошлого прогона, иначе fallback через 24ч (`should_run_self_learn_v6`, min_trades=10, min_hours=6, fallback_hours=24):
- journal/analyzer.py — FIFO-матчинг, win rate, P/L ratio
- journal/self_learn.py — адаптация min_score (±30%), SL/TP (±20%)
- post_trade.py — кластерный анализ, блок <40% WR

**Canary — в проде ВЫКЛЮЧЕН (проверено 26.10.2026).** `CANARY_ENABLED = False`,
`CANARY_ENTRY_PCT = 0.0`, `CANARY_WINDOW_HOURS = 6` (`bybit_ws/journal/self_learn.py:55-57`);
в env-файле сервиса слова `CANARY` нет вообще. Код канарейки живой и покрыт тестом
(`tests/test_self_learn_attribution.py:95` фиксирует `False`), но к live-входам не применяется:
промоушена параметров через canary сейчас нет.

## 10. Session Params

> ⚠️ **Мёртвый модуль.** `bybit_ws/session_params.py` никем не импортируется — `grep -rn SESSION_PARAMS`
> находит только сам файл (проверено 26.10.2026). Таблица ниже историческая, к live-входам не применяется.
> Живой сессионный модификатор — `journal/self_learn.get_session_modifier`: множители `min_score`
> (US 0.85 → порог ниже) и `tp_mult` (US 1.2 → TP шире); лимитов позиций и SL-множителей там нет.

Max позиций = **min(сессионный_лимит, high_vol_лимит)**.

| Сессия | BB adj | SL mult | TP mult | Max pos | Bonus |
|--------|--------|---------|---------|---------|-------|
| NY open | +5 | 0.7 | 1.2 | 5 | +10 |
| Asia | -5 | 1.3 | 1.0 | 10 | -5 |
| Weekend | +10 | 0.8 | 0.8 | 3 | +15 |
| Normal | 0 | 1.0 | 1.0 | 8 | 0 |

high_vol: max 5 позиций (приоритет над сессионным).

## 11. Risk Manager

| Параметр | Значение |
|----------|---------|
| Max позиций | min(session, high_vol) |
| Max дневной убыток | **−$200** (`risk.max_daily_loss` в `Config()`) |
| Max маржа | **$300** (`risk.max_total_margin`) |
| Risk CB | 80% от max_daily_loss → **$160** |
| DCA-маржа на символ | $80 (`strategy.dca.max_margin_per_symbol`) |
| Kill Switch | полная блокировка до /resume (без таймаута) |

## 12. SQLite (SSOT)

`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`
Бэкап каждые 6 часов (cron). 7 дней retention.
При старте: `PRAGMA integrity_check`.

## 13. Feature Flags

| Флаг | Prod | План активации |
|------|------|---------------|
| Флаг | Прод (env) | Дефолт в коде | Что делает |
|------|-----------|---------------|-----------|
| `BYBIT_ENTRY_JUDGE_ENABLED` | **1** | 0 | LLM-судья входа |
| `BYBIT_REGIME_AUTO` | **1** | 0 | LSTM-режим → гейт сторон (+`BYBIT_REGIME_MIN_CONFIDENCE`, дефолт 60) |
| `BYBIT_WORLD_MODEL` | **1** | 0 | World-model score в SHORT-скоринге |
| `BYBIT_AB_ENABLED` | **1** | 0 | A/B в `auto_entry` |
| `BYBIT_DSPY_ENABLED` | **1** | 0 | DSPy-гейт |
| `BYBIT_ML_ENABLED` | **0** | 0 | Random-Forest ML Gate |
| `BYBIT_OPTUNA_ENABLED` | **0** | 0 | Optuna-тюнинг |
| `BYBIT_WS_BB_ENABLED` | — (дефолт) | 1 | WS-кеш свечей для BB |
| `BYBIT_WS_FULL_ENABLED` | — (дефолт) | 0 | Позиции/ордера через приватный WS |
| `BYBIT_ATR_TP_ENABLED` | — (дефолт) | 1 | ATR-based TP |
| `BYBIT_PAPER_ENABLED` | — (дефолт) | 0 | Paper Trading |
| `STRUCTURED_LOGGING` | — (дефолт) | 0 | JSON-логи в events.jsonl |

> Значения получены из `~/.config/bybit-ws/env` 25.10. `BYBIT_AB_ENABLED=1` и `BYBIT_DSPY_ENABLED=1`
> в проде — не «план», как писалось раньше; `BYBIT_ML_ENABLED` наоборот выключен.

## 14. Аварийные эндпоинты

Все write-эндпоинты требуют Bearer-токен. Аварийные эндпоинты дополнительно требуют `X-Emergency-Auth` (`EMERGENCY_TOKEN`):

```
# Kill Switch — полная блокировка до /resume
POST /kill_switch  (Bearer + X-Emergency-Auth)

# Emergency Close — закрыть всё (или symbol)
POST /emergency_close  (Bearer + X-Emergency-Auth)
{"symbol": "LINKUSDT"}  // опционально

# Сброс CB
POST /circuit_breaker {"action":"reset"}
```

## 15. Glossary

| Термин | Расшифровка |
|--------|------------|
| BB | Bollinger Bands — SMA(20) ± 2×STD |
| ATR | Average True Range — ср. волатильность за 14 периодов |
| MTF | Multi-Timeframe — анализ D+W+M |
| CB | Circuit Breaker — предохранитель |
| DCA | Dollar Cost Averaging — усреднение |
| WR | Win Rate — % прибыльных сделок |
| PnL | Profit and Loss |
| SSOT | Single Source of Truth — state.db |
| JUNK | Шлак-режим — высокорисковые шорты |

## 16. Backtesting

Живой инструмент — `bybit_ws/experiments/backtest_runner.py` (детерминированный движок, кэш свечей,
run-манифест) и `paper_trade.py`. **Готовых «результатов бэктеста» в этой таблице больше нет:**
прежние «500+ сделок / WR 58% / PF 1.8 / DD −12%» не воспроизводимы ни одним артефактом в репо —
цифры удалены, чтобы не выдавать их за факт.

Воспроизвести честно:
```bash
python3 -m bybit_ws.experiments.backtest_runner --symbol SOLUSDT --days 180 --no-db
python3 -m bybit_ws.paper_trade SOLUSDT --days 90
```
Анти-overfit гейт: `experiments/validation.py` (DSR + PBO + permutation test).
