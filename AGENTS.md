# AGENTS.md — bybit-ws

> Навигация для AI-агентов. Детали стратегий, параметры, runbook → [OpenWiki](openwiki/quickstart.md).
> Обновлено: 2026-07-12 (v7.9 — Unified SL)

## Что это

Трейдинг-монитор Bybit фьючерсов. Стратегия: **Bollinger Grid** (LONG/SHORT).  
Systemd-сервис `bybit-ws-async`, ~45 MB RAM, SQLite — SSOT.

## Структура (core)

```
bybit-ws/
├── main_async.py         ← Главный цикл (async, 30с)
├── unified_sl.py          ← Unified SL (5→1, приоритет: tight>simple>hard>BE>default)
├── auto_entry.py          ← Авто-вход (MTF + Orderbook + Volume + Entry Judge + Correlation)
├── auto_sl.py             ← ATR-adaptive SL (legacy, заменён unified_sl)
├── auto_tp.py             ← ATR-based TP (1×/2×/3× ATR)
├── risk_manager.py       ← Risk + BlackSwan (3-tier) + emergency_close
├── entry_judge.py        ← Cross-model judge (Nemotron→DeepSeek, fail-closed)
├── rpc.py                ← JSON-RPC (:8766) + /kill_switch + /metrics
├── state_db.py           ← SQLite SSOT (WAL, busy_timeout=5с)
├── journal/              ← Самообучение + Canary mode
├── deploy.sh             ← Атомарный деплой
└── test_smoke.py         ← 52 интеграционных тестов
```

## Запуск

```bash
systemctl --user start bybit-ws-async     # старт
systemctl --user status bybit-ws-async    # статус
bash deploy.sh                             # деплой (smoke-тесты → атомарный swap)
python3 test_smoke.py                      # тесты
```

## Где что лежит

| Данные | Путь |
|--------|------|
| Позиции (SSOT) | `~/.local/share/bybit-ws/state.db` |
| Логи | `~/.local/share/bybit-ws/events.log` |
| Конфиг | `~/.config/bybit-ws/config.yaml` |
| Креды | `~/.config/bybit-ws/env` (chmod 600) |
| RPC | `http://127.0.0.1:8766` |
| Трейды | `~/.local/share/bybit-ws/trades.jsonl` |
| Canary state | `~/.local/share/bybit-ws/canary_state.json` |

## Аварийные ситуации

```bash
# Kill switch — закрыть ВСЕ позиции
curl -X POST http://127.0.0.1:8766/kill_switch -H "Authorization: Bearer TOKEN"

# Emergency close — без паузы
curl -X POST http://127.0.0.1:8766/emergency_close -H "Authorization: Bearer TOKEN"

# Сбросить LLM Circuit Breaker
systemctl --user restart bybit-ws-async
```

## MCP-инструменты

`scan_market` → `get_risk_status` → `get_positions` → `place_entry`

| Инструмент | Назначение |
|-----------|-----------|
| `scan_market(mode, interval)` | Скан Bollinger Grid |
| `get_positions()` | Позиции + PnL |
| `get_metrics()` | TP/SL/входы за день |
| `get_risk_status()` | Лимиты + CB |
| `place_entry(symbol, side, qty)` | Вход |
| `get_journal()` | Журнал (FIFO, bias) |

## Codebase-memory MCP — вместо grep

Проект проиндексирован: 2686 узлов, 8640 рёбер.

| Задача | Инструмент |
|--------|-----------|
| Найти определение | `search_graph(query="canary")` |
| Граф вызовов | `trace_path(function_name="auto_entry_scan")` |
| Хотспоты/O(n²) | `query_graph` → `transitive_loop_depth >= 3` |
| Фрагмент кода | `get_code_snippet(qualified_name="...")` |
| Что сломал коммит | `detect_changes(since="HEAD~1")` |

## Pre-deploy checklist

- [ ] `python3 test_smoke.py` → PASS
- [ ] `sqlite3 ~/.local/share/bybit-ws/state.db "PRAGMA integrity_check"` → ok
- [ ] `curl -s http://127.0.0.1:8766/health` → alive
- [ ] `grep Heartbeat ~/.local/share/bybit-ws/events.log | tail -1` → свежий
- [ ] `bash deploy.sh`

## Детали

Архитектура цикла, параметры стратегий, фильтры входа, Black Swan, сессии,
runbook инцидентов, схема БД, диагностика по логам → [OpenWiki](openwiki/quickstart.md).
