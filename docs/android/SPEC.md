# bybit-ws Android App — Спецификация

> Версия: 0.1 (черновик)
> Цель: мобильное управление Bollinger Grid монитором с телефона

## MVP (v0.1)

### Экран 1: Дашборд

Список позиций с цветовой индикацией:
- Символ, сторона (LONG🟢/SHORT🔴), PnL ($/%)
- SL / TP / цена входа / марка
- Общий нереализованный PnL в шапке
- Pull-to-refresh → запрос к RPC

**API:** `GET /rpc/positions`

### Экран 2: Детали позиции

По тапу на позицию:
- График PnL (если будет WebSocket)
- Кнопки: Закрыть, Подвинуть SL, Поставить TP
- Поля ввода: новый SL, новый TP

**API:**
- `POST /move_sl {"symbol","stop_loss"}`
- `POST /close {"symbol"}`
- Установка TP: `POST /v5/position/trading-stop` через прокси

### Экран 3: Алерты

Push-уведомления (Firebase FCM):
- 🔴 STOP: TP/SL сработал
- 🚀 PUMP: памп-детект
- ⚡ ENTRY: новый вход
- 🛑 CB: circuit breaker
- 💀 BLACK SWAN: экстренное закрытие

**Механизм:** сервер отправляет alert через ntfy → FCM-бридж или напрямую WebSocket

### Экран 4: Скан SHORT

Кнопка «Сканировать»:
- Выбор интервала: 1h / 4h / D
- Список сигналов с score
- Тап → подтверждение входа

**API:** `POST /scan {"mode":"short","interval":"D"}`

### Экран 5: Настройки

- IP:порт сервера (можно несколько)
- Токен авторизации
- Тема: тёмная/светлая
- Интервал авто-обновления (5с / 15с / 30с / manual)

## Архитектура клиента

```
Android App (Kotlin, Jetpack Compose)
├── Data Layer
│   ├── RpcClient (OkHttp + JSON + JWT)
│   ├── WebSocketClient (real-time updates)
│   └── PushReceiver (Firebase FCM)
├── Domain Layer
│   ├── PositionRepository
│   ├── AlertRepository
│   └── ScanRepository
├── UI Layer (Jetpack Compose)
│   ├── DashboardScreen
│   ├── PositionDetailScreen
│   ├── AlertScreen
│   ├── ScanScreen
│   └── SettingsScreen
└── DI (Hilt)
```

## Безопасность (обязательно перед стартом)

- **HTTPS:** nginx перед RPC :8766 с Let's Encrypt
- **JWT-аутентификация** вместо Bearer-токена
- **Rate limiting:** nginx `limit_req` — 10 запросов/сек
- Токен в EncryptedSharedPreferences
- API-ключи Bybit — только на сервере

## Модели данных

```kotlin
data class Position(
    val symbol: String,
    val side: String,        // "Buy" | "Sell"
    val leverage: Double,
    val entryPrice: Double,
    val markPrice: Double,
    val unrealisedPnl: Double,
    val stopLoss: Double?,
    val takeProfit: Double?
)

data class Alert(
    val type: String,        // "TP" | "SL" | "PUMP" | "ENTRY" | "CB" | "BLACK_SWAN"
    val symbol: String,
    val message: String,
    val timestamp: Long
)

data class ScanResult(
    val symbol: String,
    val side: String,
    val score: Int,
    val interval: String,
    val bbPosition: Double
)
```

## Безопасность

- Токен хранить в EncryptedSharedPreferences
- HTTPS обязательно для внешнего доступа
- API-шлюз (nginx) с rate-limiting перед RPC
- Не хранить API-ключи Bybit в приложении

## Что уже готово на сервере

| Компонент | Статус |
|-----------|--------|
| RPC :8766 | ✅ 15 эндпоинтов |
| MCP bybit-ws | ✅ 6 инструментов |
| WebSocket | ✅ публичный (kline) |
| Grafana | ✅ :8888 |
| Алерты (ntfy) | ✅ |
| Тесты | ✅ 45/45 |

## Что нужно доделать на сервере

| Задача | Приоритет |
|--------|-----------|
| HTTPS прокси перед RPC | 🔴 высокий |
| WebSocket для приватных потоков | 🟡 средний |
| FCM/пуш-сервер | 🟡 средний |
| /rpc/scan эндпоинт (уже есть /scan) | ✅ |
| /rpc/set_tp эндпоинт | 🟡 средний |
| Структурированные JSON-логи | 🟢 низкий |

## Порядок сборки MVP

1. **Kotlin-проект** — пустой проект с Jetpack Compose
2. **RpcClient** — подключение к :8766, получение позиций
3. **DashboardScreen** — список позиций
4. **PositionDetailScreen** — подвинуть SL
5. **Алерты** — Firebase + ntfy-бридж
6. **Скан** — экран сканирования SHORT
7. **Релиз** — Google Play / APK
