# Changelog

Все заметные изменения в проекте **bybit-ws** — трейдинг-монитор для Bybit фьючерсов на стратегии Bollinger Grid.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

---

## [4.1] — 2026-06-18

### Added
- **`/rpc/paths`** — эндпоинт авто-обнаружения путей для AI-агентов (state_db, events_log, config_file, repo, install_dir, команды синхронизации и рестарта). Без авторизации.
- **SL при корреляции** — автоматический стоп-лосс при обнаружении корреляции >0.8 между позициями в одном секторе.
- **Volume-check для пампов** — проверка объёма перед входом в памп-шорт, фильтрация низколиквидных монет.
- **Коды ошибок RPC** — стандартизированные JSON-RPC коды ошибок (-32000..-32099).
- **Backup перед опасными операциями** — автоматический бэкап state.db через `hermes-backup` skill.
- **Расшифровка фандинга в partial TP алертах** — ставка фандинга и прогнозируемая ротация показываются в уведомлениях.
- **Защита SL от опускания** — SL на LONG-позициях больше не может быть понижен авто-SL механикой.
- **MONITOR.md v2** — полная документация: JSON-схемы, коды ошибок, глоссарий, lifecycle, риски.
- **MCP-инструменты в AGENTS.md** — сигнатуры `place_entry`, `scan_market`, `get_positions`, `get_risk_status`, `get_metrics`; типовые воркфлоу для AI-агентов.
- **Comprehensive README** — установка, работа, возможности, ошибки, логирование, проверки.

### Changed
- **RPC `/enter`** — поддержка Limit-ордеров (`order_type` + `price`), не только Market.
- **Формат SL/TP алертов** — entry→exit, PnL$, PnL%, size×lev, side.

### Fixed
- **404-флуд** на эндпоинтах Bybit API — правильный retry с exponential backoff.
- **positionIdx 0/1/2** — полная поддержка хедж-режима, перебор позиций по всем индексам.
- **SL re-entry ладдеры** — устранены ложные срабатывания на SHORT-позициях.
- **partial_tp** — баг `openTime` (мс→с) + отображение дробных центов в алертах.
- **Inline query** — MarkdownV2 → HTML (unescaped dash bug в Telegram).

---

## [4.0] — 2026-06-16

### Added
- **ATR-based риск-сайзинг** (`position_sizing.atr_margin()`) — расчёт маржи на основе ATR(14) с кешем на 4 часа. Фаза 4.1.
- **Веб-дашборд** с прокси-сервером (порт 8765) — позиции, PnL, риск в реальном времени.
- **9-метричный скоринг** — Tier, BB%, объём, RSI, фандинг, корреляция, волатильность, тренд, ATR.
- **Защита ручных позиций** — флаг `manual` в pumps.json, монитор не трогает ручные позиции.
- **Paper Trading API** (`PaperExchange`) — симулятор биржи для бэктеста: проскальзывание 0.05%, комиссия taker 0.06%, ликвидация ±10%.
- **Разбивка main_loop** на 3 функции: `_run_heavy_cycle` (120с), `_run_x10_cycle` (240с), `_run_safety_checks`.
- **Daily PnL alert** — ежедневный отчёт о прибыли/убытке в Telegram.
- **Авто-безубыток** — при профите >10% ставится SL = entry × 1.01 (LONG) или entry × 0.99 (SHORT).
- **Защита SL от перезатирания** — запрет изменения SL, если SL уже на стороне прибыли (SL > entry для LONG).
- **Smoke-тесты 45/45** — интеграционные тесты: trailing_sl (8), state_db (20), auto_sl (5), api (12).
- **SQLite SSOT** (`state_db.py`, WAL-режим, 8 таблиц) — positions, trades, alerts, short_positions, pumps, x10_limits, paper_positions, paper_trades.
- **Alert dedup через SQLite** — персистентная дедупликация алертов между перезапусками.
- **RPC без subprocess** — JSON-RPC сервер встроен в основной процесс, порт 8766.
- **Trailing SL для SHORT** — зеркальная логика LONG: BB < 25%, PnL > 15%, SL ползёт вниз.
- **Circuit breaker** — `max_daily_loss` и `max_total_margin` с принудительным кулдауном.
- **File locking** (filelock) на все JSON-стейты.

### Changed
- **api.py** — миграция с subprocess на requests, добавлен filelock.
- **Конфигурация** — внешний YAML (`~/.config/bybit-ws/config.yaml`) с подстановкой `${ENV}`.

### Fixed
- **5 stability fixes (Phase 1)** — DCA bypass, дедупликация, file lock, персистентное состояние.
- **7 CRITICAL fixes (аудит)** — circuit breaker, hallucinated params, watchdog, x10_limits.
- **Bybit v3 POST signature** — `json.dumps` separators fix.

### Security
- **RPC-авторизация** — Bearer UUID-токен обязателен для всех вызовов (кроме `/rpc/paths`).
- **Никаких ключей в коде** — все секреты из `.env` / переменных окружения.
- **bare except:pass** → логирование во всех модулях (42 случая исправлены).

---

## [3.11] — 2026-06-15

### Added
- Circuit breaker: `max_daily_loss` / `max_total_margin` с принудительным кулдауном 24ч.
- File locking (filelock) на все JSON-стейты.
- Exponential backoff при HTTP 429.
- MCP `get_risk_status` + `/rpc/risk`.

### Fixed
- `json.dumps` separators для Bybit v3 POST signature.
- `auto_sl` — расширенная проверка JUNK: pump_detect tracking, daily_pump, manual.

---

## [3.10.1] — 2026-06-13

### Fixed
- **`instant_tp_symbols` хардкод:** удалён `NEARUSDT` из дефолтного конфига — мгновенно закрывал позицию при любом профите, делая x10 вход в NEAR невозможным.
- **`auto_sl.py`:** не перезатирать SL на прибыльных позициях + проверка пустого `stopLoss` (строка `""` или `"0"`).
- **`main.py` `ALERTS`:** импорт отсутствовал → `NameError`.
- **`main.py` `tp_hit_syms` guard:** добавлена проверка `sym not in new_positions` по аналогии с `sl_hit_syms`.
- **Trade journal:** поле `strategy` заполняется для всех типов (GRID_LONG/SHORT/JUNK/x10), `reason` не всегда SL.
- **Брендинг:** все упоминания `@GridSignalBot` заменены на `@Gridbolbot`.

---

## [3.10.0] — 2026-06-13

### Added
- **Junk Trail TP (`junk_trail.py`):** автоматическая фиксация прибыли JUNK-шортов. Профит >15% → TP подтягивается (70% фиксации), >30% → затягивается (85%).
- **Недельный памп-детект:** `check_weekly_pumps()`. Рост ≥230% за 7д + оборот ≥$1M → market SHORT, без SL/TP, макс 2 позиции.
- **Pipeline Trace v2:** классификация ордеров (SL/TP/LIMIT_ENTRY), дедупликация SL, детект зависших лимиток >48ч.

### Fixed
- **pump_detect KeyError 'alerts':** запись `peak_price` до блока `if not prev` делала пустой dict непустым → KeyError.
- **SL re-entry только для LONG:** `notify_sl_hit()` вызывался без проверки `side`, создавая ложную очередь на SHORT.
- **auto_sl.py пропускает JUNK-шорты:** проверка `pumps.json` — если символ помечен как памп-шорт, SL не ставится.
- **VPN watch — ложные тревоги при idle:** critical только сервис + порт, нулевой трафик без клиентов = предупреждение.

---

## [3.9.1] — 2026-06-09

### Fixed
- **False «Лимитка сработала»:** `snapshot.py` различает заполнение и отмену по `cumExecQty`.
- **Time budget в `check_auto_short`:** deadline 20с, early exit при исчерпании — устранены таймауты `_timed_call`.

---

## [3.9.0] — 2026-06-09

### Added
- **RSI-дивергенции** — детектор дивергенций с кулдауном 24ч.
- **Код-ревью Manus AI** — retry POST, BB-based SL, CORS fix, конфигурируемые DCA/sl_reentry, `utils.py`.

### Changed
- `api.py:fetch_orders()` сохраняет `cumExecQty` в снапшот ордера.
- Репозиторий переименован: `poliakarm` → `poliakarmai`.

---

## [3.8.0] — 2026-06-09

### Added
- **Position Sizing v3.8:** динамическая маржа = депозит × risk_pct / max_positions × score_multiplier.
- `position_sizing.py`: `get_deposit()`, `calculate_margin()`, `margin_for_strategy()`.
- Risk budgets per strategy: LONG 20%, x10 5%, DCA 10%, pump 6%.
- Score multipliers: 8.5+→1.4, 7.5+→1.15, 6.5+→1.0, 5.5+→0.75.
- Floor: $5 minimum, cap: max(MIN_MARGIN, 40% risk_budget).
- Интегрирован во все 7 entry-модулей.

### Fixed
- Position sizing cap bug: floor ($5) переопределялся cap на маленьких депозитах.

---

## [3.7.0] — 2026-06-09

### Added
- **X10 Strategy Pack:** BB Scalping M5, Mean Reversion Extreme, Funding Rate Momentum.
- **ATR Risk Sizing:** валидация размера позиции против ATR(14).
- **X10 Risk Limits:** daily loss stop (3 trades), 24h cooldown, correlation check.
- **Junk short hard stop:** max_loss_pct=15%, max_hold_hours=48.
- **Funding trend filter:** SHORT только когда funding >0.1% + BB >85% + 3-дневное падение цены.
- **Strategy tag в trade journal:** `trades.md` и `trades.jsonl` включают имя стратегии.
- **Correlation dedup на x10 entries:** блокировка входа при ≥2 коррелированных позициях.
- **Banned symbols:** config-driven permanent ban (`risk.banned_symbols`).

---

## [3.6.0] — 2026-06-08

### Added
- **Dashboard v3.7:** SVG с winrate, funding, margin, regime, correlations.
- **Funding tracker:** экстремальные алерты по ставке фандинга (>0.1% / <−0.05%).
- **Margin alerts:** >80% ⚠️, >95% 🚨, >100% 🆘.
- **Market regime classifier:** TRENDING_UP/DOWN, CHOPPY, HIGH/LOW_VOL (BTC+ETH).
- **Correlation matrix:** детект пар >0.8, алерты концентрационного риска.
- **SHORT TP via trading-stop:** TP bundled with SL в одном API-вызове.
- **Шлак-режим для auto_short:** дневной рост ≥80%, без SL, DCA-лесенка +100%/+120%.

### Fixed
- trades.jsonl dedup: 681 дубликатов → 59 реальных сделок.
- Thread memory leak: stack_size 8MB → 2MB (×4 экономии).
- GridSignal bot: необработанные исключения теперь логируются.
- LONG cooldown после SL: 4ч пауза предотвращает петлю ре-входов.
- Cascade liquidation protection: market-close если цена в 2× ближе к ликвидации чем к SL.

---

## [3.5.0] — 2026-06-08

### Added
- DCA-лимиты: max_margin_per_symbol=80, max_dca_count=2.
- Каскадные ликвидации: защита от цепных ликвидаций.
- LONG cooldown: 4ч пауза после SL.
- SHORT max_hold: 72ч максимальное удержание.
- TP trading-stop: TP и SL в одном API-вызове.
- Дедупликация short-алертов.
- Секторные лимиты.

---

## [3.4.0] — 2026-06-08

### Added
- RPC auth: Bearer token, rate limiting (60 req/min).
- Notification format v3.4: cause + PnL (`🔴 SYM SL −$X.XX (entry $Y)`).
- OpenAPI 3.0 схема (`openapi.yaml`) + Python SDK для AI-агентов.
- Webhook handler.
- Docker support: Dockerfile + docker-compose.yml.
- DESIGN.md: полная архитектурная документация.
- Graceful shutdown (SIGTERM): save positions, fix SL, exit cleanly.
- Log rotation: events.log при 50MB, 7 файлов.

### Fixed
- SHORT block: 4 бага в auto_short (BB keys, TP direction, positionIdx, lower<=0).
- Watchdog spam: heavy checks skip when cycle >90s.
- positionIdx inconsistency: always try 0 first, 1 on error 10001.

---

## [3.3.0] — 2026-06-08

### Added
- YAML config (`~/.config/bybit-ws/config.yaml`) с `${ENV}` подстановкой.
- `_timed_call`: вызовы с таймаутом (25с по умолчанию) в отдельных потоках.
- `/pause`, `/resume`, `/reload-config` endpoints.
- `GET /logs`, `paused` в `/health`.

---

## [3.0.0] — 2026-06-07

### Added
- **Bollinger Grid Monitor** — ядро системы, 30-секундные циклы.
- **LONG auto-entry:** вход при BB% < 25% со скорингом.
- **SHORT auto-entry:** вход при BB% > 85% (перегретые активы).
- **Auto SL/TP:** trading-stop интеграция.
- **RPC сервер:** порт 8766, REST API.
- **SL re-entry лесенка:** −5%, −10%, −15% после стопа.
- **Pump detection:** DCA-шорты на >120% дневных пампах.
- **GridSignal Bot:** Telegram-бот с `/scan`, LONG/SHORT сигналами.
- **Динамический SL:** +5% Tier A/B, +7% для шлака (C/D).
- **Лимитный вход:** +2% выше рынка вместо Market для авто-шортов.

### Fixed
- **auto_short: 4 критические бага** — автошорт не работал с момента создания.

---

## [2.1] — 2026-06-09 (ретроспективно)

### Added
- **ML-скоринг сигналов** (`ml_scorer.py`) — RandomForest F1=0.69, 70/30 вес.
- **Walk-forward бэктест** Bollinger Grid на исторических klines (REST API, 262 сигнала).
- **Partial TP** — динамический сплит 20/80→50/50 на разгоне, без numpy.
- **Trailing Stop для x10** — HEAVY_CYCLE, фильтр leverage≥10.
- **Авто-фандинг-ротация** — check + execute + алерты.
- **Трёхэшелонный аудит** — CRITICAL + HIGH + MEDIUM исправлены (14 находок).
- **CVE-патчи** — обновление requests.

### Fixed
- partial_tp imports в standalone-режиме.
- /scan rotation — absolute imports fix.
- inline_query → asyncio.to_thread + timeout 40→15с.

---

## [2.0] — 2026-06-08 (ретроспективно)

### Added
- **SQLite SSOT** — миграция с JSON на SQLite (WAL, 8 таблиц).
- **StateDB** — `state_db.py`: positions, trades, alerts, short_positions, pumps, x10_limits, paper_positions, paper_trades.
- **RPC сервер** — JSON-RPC без subprocess, порт 8766.
- **Alert dedup через SQLite** — персистентная между перезапусками.
- **Trade history audit** — полная история сделок в SQLite.
- **SHORT trailing** — зеркальная логика LONG (BB <25%, PnL >15%, SL ползёт вниз).
- **Smoke-тесты** — 45 интеграционных проверок.
- **Auto-TP для SHORT** — автоматическая фиксация прибыли.
- **Prometheus /metrics** — `bybit_ws_active_positions`, `bybit_ws_daily_pnl`, `bybit_ws_cycle_duration_seconds`.

---

## [1.0] — 2026-06-07 (ретроспективно)

### Added
- **Первая рабочая версия** Bollinger Grid монитора.
- **api.py** — Bybit v5 REST API клиент (6 endpoints, HMAC-SHA256).
- **main.py** — главный цикл с базовыми проверками.
- **auto_sl.py** — автоматические стоп-лоссы на основе BB-полос.
- **auto_short.py** — авто-шорты при перегреве (>85% BB).
- **pump_detect.py** — детектор пампов (>120% за 24ч).
- **sl_reentry.py** — лесенка ре-входов после стопа.
- **gridsignal-bot.py** — Telegram-бот для сигналов.
- **Telegram-алерты** — уведомления о SL/TP/входах.
- **Базовая конфигурация** — `.env` и хардкод-параметры.

---

## Легенда версий

| Версия | Фаза | Ключевая фича |
|--------|------|---------------|
| **v1.x** | Фаза 1 — Базовая | Bollinger Grid, auto-SL/TP, pump detect, Telegram-бот |
| **v2.x** | Фаза 2 — SQLite+RPC | SQLite SSOT, JSON-RPC сервер, Prometheus, smoke-тесты, SHORT-трейлинг |
| **v3.x** | Фаза 3 — ML-скоринг | RandomForest F1=0.69, Partial TP, бэктест, фандинг-ротация, x10 стратегии |
| **v4.0** | Фаза 4.1 — ATR + дашборд | ATR-based риск-сайзинг, веб-дашборд, 9-метричный скоринг, Paper Trading |
| **v4.1** | Фаза 4.2 — Интеграция | /rpc/paths для AI-агентов, SL при корреляции, volume-check, MCP-инструменты |
