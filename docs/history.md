# История разработки bybit-ws

> Вынесено из AGENTS.md для соблюдения лимита 80-200 строк.

## Фаза 1–2 (стабильность + надёжность) ✅

- SQLite миграция (SSOT)
- SHORT-трейлинг (зеркальный LONG)
- Защита SL от перезатирания
- Авто-безубыток (+10%)
- Paper Trading API
- Prometheus /metrics
- main_loop разбит на 3 функции

## Фаза 3 (умный трейдинг) ✅

- ML-скоринг сигналов — RandomForest F1=0.69 → F1=0.921
- Trailing Stop для x10
- Partial TP — динамический сплит 20/80→50/50
- Авто-фандинг-ротация

## Фаза 4 (масштабирование) ✅

- ATR-based риск-сайзинг
- Multi-timeframe конфлюенс (D/W/M, ≥2/3)
- Telegram-алерты
- WebSocket live-цены/BB
- httpx (подготовка к asyncio)
- Дашборд v5.0 (127.0.0.1:9999)

## Фаза 5 (ML) ✅

- RandomForest ML Gate
- LSTM-классификатор рыночного режима (5 классов)
- RL-агент (DQN, Stable-Baselines3)
- Ансамбль RF+LSTM+RL (веса 0.34/0.33/0.33)
- A/B-тест ML Gate vs baseline
- HMAC-подпись всех моделей
- Feature flag `BYBIT_ML_ENABLED=0`

## Аудит 18.06.2026

Полный аудит трёх эшелонов (Source-Driven + Security + Adversarial).  
Исправлено: 23 находки (7 CRITICAL + 12 HIGH + 4 MEDIUM). 24 MEDIUM/LOW осталось.  
HMAC-подпись моделей закрывает RCE-вектор.  
Атомарный деплой с rollback.  
RPC `/rpc/ml_toggle` — быстрый откат ML.  
Watchdog с проверкой зависания цикла.  
Walk-forward валидация ML.
