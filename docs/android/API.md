# bybit-ws Android App — API Reference

> Эндпоинты для мобильного клиента. Версия: 2026-06-28.

## REST API (порт :8766)

**Аутентификация:** `Authorization: Bearer <токен>`

Токен: `state.db` → `kv_store` → `rpc_auth_token`. Ротация: `POST /reset-token`.

**Формат ошибки:**
```json
{"api_version":"v1","error":"...","detail":"...","code":400,"error_code":"invalid_symbol"}
```

### Дашборд

```
GET /rpc/positions
```
→ все позиции с PnL, SL, TP, leverage

```json
// Ответ
{
  "api_version": "v1",
  "positions": {
    "AAVEUSDT": {
      "symbol": "AAVEUSDT", "side": "Sell", "leverage": 10,
      "entryPrice": "98.60", "markPrice": "93.50",
      "unrealisedPnl": "4.18", "stopLoss": "95.00",
      "takeProfit": "90.00", "size": "1"
    }
  },
  "total_unrealised_pnl": "4.18",
  "count": 1
}
```

```
GET /rpc/risk_full
```
→ дневной PnL, маржа, circuit breaker, корреляции

### Управление позицией

```
POST /move_sl
```
```json
// Запрос
{"symbol": "AAVEUSDT", "stop_loss": 95.00, "take_profit": 90.00}
// Ответ
{"api_version":"v1","status":"ok","symbol":"AAVEUSDT","old_sl":"96.60","new_sl":95.00,"new_tp":90.00}
```

```
POST /close
```
```json
// Запрос
{"symbol": "AAVEUSDT"}
// Ответ
{"api_version":"v1","status":"ok","symbol":"AAVEUSDT","closed":true}
```

### Скан SHORT

```
POST /scan
```
```json
// Запрос
{"mode": "short", "interval": "D", "limit": 10}
```
→ список сигналов со score

### Аварийные

```
POST /emergency_close  — закрыть все позиции (требует X-Emergency-Auth)
POST /kill_switch       — закрыть всё + пауза (требует X-Emergency-Auth)
POST /rpc/circuit_breaker {"action":"reset"}  — сброс CB
```

### Прометеус

```
GET /rpc/metrics  — Prometheus-метрики (Bearer auth)
```

## MCP (через Hermes)

| Инструмент | Назначение |
|-----------|-----------|
| `bybit_ws.get_positions()` | Позиции + PnL |
| `bybit_ws.scan_market(mode, interval, limit)` | BB-скан |
| `bybit_ws.get_risk_status()` | Лимиты + CB |
| `bybit_ws.place_entry(symbol, side, qty, sl, tp)` | Вход |
| `bybit_ws.get_journal()` | Журнал сделок |

## WebSocket (Bybit)

Публичные: `wss://stream.bybit.com/v5/public/linear` (kline, тикеры).
Приватные: `wss://stream.bybit.com/v5/private` (HMAC-SHA256, ключи на сервере).

## Безопасность

- Токен в `EncryptedSharedPreferences`
- `X-Emergency-Auth` для критических операций
- HTTPS + nginx rate-limiting перед RPC
- API-ключи Bybit — только на сервере
