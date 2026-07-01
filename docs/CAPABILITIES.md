# bybit-ws — Bollinger Grid трейдинг-монитор v7.6

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
| Модель 1 | Nemotron (OpenRouter) |
| Модель 2 | DeepSeek (fallback) |
| Таймаут | 5s |
| Обе упали | revise (блок) |
| Judge CB | 3 падения → отключение на 1ч |
| Fallback >1ч | режим без Judge (fail-open), min_score +20% |
| Кэш | 300s TTL |
| Soft timeout | 5 ответов >3s → таймаут 7s |

## 4. Auto-SL — ATR-adaptive

SL = entry ± k × ATR(14), capped -50%/+50% от входа.

| Режим | ATR/Price | k |
|-------|-----------|---|
| high_vol | >5% | 2.5 |
| trending | 3-5% | 2.0 |
| normal | 1-3% | 1.5 |
| low_vol | <1% | 1.3 |

**Trailing SL:** активируется при PnL >15%. Шаг: каждые 1% движения цены.
Distance: 1.0×ATR(14) от тек. цены. Минимум: 0.5% от entry.

## 5. Auto-TP — ATR-based (3 уровня)

| Уровень | k | % объёма | Действие |
|---------|---|----------|---------|
| TP1 | 1.0×ATR | 40% | Закрыть 40%, SL → breakeven |
| TP2 | 2.0×ATR | 35% | Закрыть 35%, SL подтянуть |
| TP3 | 3.0×ATR | 25% | Закрыть остаток |

PERM_SKIP: time-decay 24ч. Флаг: `BYBIT_ATR_TP_ENABLED=1`.

## 6. DCA (Dollar Cost Averaging)

| Параметр | Значение |
|----------|---------|
| Условие | Позиция в убытке >5% + цена на след. уровне BB |
| Volume | vol > SMA(20) × 1.2 |
| Размер | 50% от исходной позиции |
| Максимум | 1 DCA на позицию |
| SL после DCA | Средневзвешенная цена ± 1.5×ATR |

## 7. Pump Detection

| Уровень | Критерий | Действие |
|--------|---------|---------|
| Early warning | Volume spike >5×SMA(20) + price >20%/1h | алерт, не шортить |
| JUNK | +80% за день | SHORT с жёстким SL -15% |
| JUNK CB | 2 закрытия по -15% | отключить JUNK на 24ч |

## 8. Black Swan — многоуровневая защита

| Триггер | Действие |
|---------|---------|
| BTC -3%/15min | Закрыть 50% позиций (худшие по PnL) |
| BTC -5%/30min | Закрыть 80% позиций |
| BTC -8%/1h | Закрыть 100% + пауза (kill switch) |
| PnL > 2× max_daily_loss | Emergency close all |

Опережающий индикатор: BTC ATR(14)/price > порога → повышенная готовность.

## 9. Self-Learning

Каждые 2880 циклов (~24ч):
- journal/analyzer.py — FIFO-матчинг, win rate, P/L ratio
- journal/self_learn.py — адаптация min_score (±30%), SL/TP (±20%)
- post_trade.py — кластерный анализ, блок <40% WR

**Canary-режим (v7.1):**
- Новые параметры → только 10% входов (canary group)
- 48ч окно оценки: WR canary vs baseline
- Падение WR >10% → авто-rollback
- WR canary >= baseline → promote на все входы
- Состояние: `canary_state.json`, лог: `self_learn.jsonl`

## 10. Session Params

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
| Max дневной убыток | -$50 |
| Max маржа | $300 |
| Risk CB | 80% от max_daily_loss |
| Kill Switch | полная блокировка до /resume (без таймаута) |

## 12. SQLite (SSOT)

`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`
Бэкап каждые 6 часов (cron). 7 дней retention.
При старте: `PRAGMA integrity_check`.

## 13. Feature Flags

| Флаг | Prod | План активации |
|------|------|---------------|
| `BYBIT_WS_BB_ENABLED` | 1 | — |
| `BYBIT_ML_ENABLED` | 1 | — |
| `BYBIT_ATR_TP_ENABLED` | 1 | — |
| `BYBIT_DSPY_ENABLED` | 0 | После 500+ сделок |
| `BYBIT_OPTUNA_ENABLED` | 0 | После бэктеста 3 мес. |
| `BYBIT_REGIME_AUTO` | 0 | После валидации LSTM |
| `BYBIT_AB_ENABLED` | 0 | При тесте PPO vs DQN |
| `BYBIT_WS_FULL_ENABLED` | 0 | При миграции на WS |
| `BYBIT_PAPER_ENABLED` | 0 | Paper Trading (без риска) |
| `STRUCTURED_LOGGING` | 0 | JSON-логи в events.jsonl |

## 14. Аварийные эндпоинты

```
# Kill Switch — полная блокировка до /resume
POST /kill_switch  (X-Emergency-Auth)

# Emergency Close — закрыть всё (или symbol)
POST /emergency_close  (X-Emergency-Auth)
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

| Параметр | Значение |
|----------|---------|
| Период | 01.2025–06.2026 (18 мес.) |
| Данные | 1M+ свечей (1h, 4h, D) |
| Сделок | 500+ |
| Win rate | 58% |
| Profit factor | 1.8 |
| Max drawdown | -12% |
| Валидация | Walk-forward 30 дней |
