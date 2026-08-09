# Changelog

Все заметные изменения в проекте **bybit-ws** — трейдинг-монитор для Bybit фьючерсов на стратегии Bollinger Grid.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

---

## [11.0] — 2026-08-08

### Фаза 9 — SHORT-оптимизация
- **World Model**: точность 22.3% → 33.1% (+48%), добавлен в SHORT-скоринг
- **SHORT ML-фильтр**: BB% порог 95→100, WM score порог ≥3, MTF-скидка TRENDING_DOWN
- **SHORT перекос BB%**: <30% = 0 баллов (защита от входа на падении)
- **Android MVP**: `/set-tp` эндпоинт + `/generate-jwt` для серверной авторизации

### SHORT-результаты (paper trade)
- BTC/ETH top-40, 3 месяца: WR 40% → 52%, PF 0.56 → 0.92, Sharpe -0.6 → -0.1
- SOL/AVAX (волатильные): WR 43%, PF 1.12, Sharpe +0.27

### Техническое
- JWT auth для Android RPC-клиента
- `gsc_audit` интеграция (pre-commit + CI)
- gitignore: `.repowise/`, `.claude/`, `.mcp.json`

---

## [10.0] — 2026-08-04

### Self-Learning v10 (20+ механик)
- **Thompson Sampling**: Dynamic Bandit — авто-прунинг + генерация рук, Uncertainty-aware selection
- **Ensemble Learning**: отдельный bandit для каждого режима рынка (6 режимов), Coordinated transition handover
- **Drift Detector**: per-regime окна (HIGH_VOL=30, RANGING=100) с EMA baseline, Causal inference
- **Stress Testing**: 4 исторических backtest-сценария + 1000 Pareto Monte Carlo (α=2.5, heavy-tail)
- **Composite Score**: adaptive per-regime веса + Exponential decay (200 дней → 37% веса)
- **Micro-updates**: обучение после каждой сделки с outlier-защитой (>3σ skip)
- **Robust bandit updates**: >2σ → damped weight 0.5, >3σ → outlier skip
- **Canary mode**: Bayesian A/B, адаптивный 5-20%, idle timeout 3ч
- **Param versioning**: Git-like snapshots (`params_history/v*.json`)

### Fixed
- **Критично: PF=0.75** — корень в SHORT-позиции STGUSDT (730ч удержания, $−167). Без неё SHORT: WR=71%, PnL=+$138
- **Time exit** перенесён в основной цикл (каждый 30с, не только heavy)
- `TIME_EXIT_MAX_HOURS` 48→24ч — позиции-зомби закрываются за ≤24ч
- **Composite score** брал пустые данные из `roundtrips_sample` → загрузка сырых трейдов из SQLite
- **DEFAULT_REGIME_PARAMS** унифицированы: 6 режимов (TRENDING_UP/DOWN, RANGING, HIGH/LOW_VOL, CHOPPY)
- **Self-learn interval** 6ч→24ч (cooldown, меньше overfitting)
- **Exponential decay** 0.01→0.005 (200 дней → 37% вместо 100)
- feature flags задокументированы: self-learn не зависит от `BYBIT_AB_ENABLED`, `BYBIT_REGIME_AUTO`

### Changed
- AGENTS.md: v10 с текущими метриками (123 trades, PF=0.75, SHORT diagnostic)
- SELF_LEARN.md: v10 с полной эволюцией v4→v10, реальными цифрами, диагностикой
- README.md: phase 8.2→10, обновлённые результаты, self-learning секция
- LICENSE: MIT → AGPL-3.0 (подтверждено в v8.0)

## [9.0] — 2026-08-04

### Added
- **Dynamic ParameterBandit**: авто-прунинг худших рук + генерация вариаций лучшей каждые 24ч
- **Pareto-calibrated Monte Carlo**: heavy-tail распределение (α=2.5) вместо uniform
- **Regime-aware Drift Detector**: per-regime окна (HIGH_VOL=30, RANGING=100) + EMA baseline
- **ParameterEnsemble**: отдельный Dynamic Bandit для каждого из 6 режимов
- **Online micro-updates**: `on_trade_closed()` — bandit posterior + drift + symbol profile после каждой сделки

## [8.0] — 2026-08-01 / 2026-08-02

### Added
- **Paper Trading** (`paper_trade.py`): бэктест Bollinger Grid на исторических свечах
- **Веб-дашборд** (публичный): `/dashboard`, `/rpc/dash/all`, `/rpc/dash/risk`, `/rpc/dash/signals`
- **SVG-дашборд**: генерация `dashboard.svg` каждые 5 мин (cron)
- **Продуктовая документация:** README.md, ONBOARDING.md, AGENTS.md для AI-агентов
- Graphify-driven рефакторинг: граф кода → чистка циклов импортов и дедупликация
- **Thompson Sampling** (v8): 3-рукий bandit с Beta posterior
- **Monte Carlo stress test**: 1000 синтетических crash-симуляций
- **ConceptDriftDetector**: ADWIN-based, окно 100, порог 5%
- **Anomaly detection**: IQR-based outlier filter (3×IQR)
- **Adaptive composite weights**: 6 режимов × свои веса composite_score
- **Parameter versioning**: Git-like snapshots

### Changed
- Лицензия: MIT → AGPL-3.0
- Тесты: 45 → 52 smoke-тестов

## [8.1] — 2026-08-01 / 2026-08-04

### Added
- **LSTM World Model** (`lstm_world_model.py`): multi-task OHLCV prediction + entry scoring (0-5 баллов)
- World Model в `auto_entry.py` для LONG и `auto_short.py` для SHORT
- **Fix exit_reason:** детект SL/TP по движению цены вместо полей Bybit API (которые всегда null)
- **Anti-ludomania:** 3 убытка за час → блок авто-входов на 30 минут
- **LSTM-режим блокирует входы:** RANGING/CHOPPY → LONG=OFF, SHORT=OFF. `BYBIT_REGIME_AUTO=1`
- **Адаптивный TP/SL по LSTM-режиму:** RANGING→TP ближе/SL ближе, TRENDING→TP дальше/SL дальше
- **BlackSwan v2:** корреляционный алерт без авто-закрытия (порог -$150, только уведомление)
- **MTF-дыра закрыта:** без данных D-TF входы блокируются (раньше пропускались)
- `references/world-model-debugging.md` — документация отладки World Model

### Fixed
- REGIME_AUTO: `'method-wrapper' object has no attribute REGIME_LONG_ENABLED` (импорт модуля вместо `from . import __init__`)
- SL floor: 2% → 5% для LONG (не выбивает шумом при ×10 плече)
- LSTM FeatureScaler pickle compatibility при `python3 -m bybit_ws.lstm_regime`
- BlackSwan v1 закрывал позиции без ведома пользователя → откачено
- Time-stop по createdTime откачен — createdTime ≠ время открытия позиции

### Changed
- Тяжёлый цикл: параллельные pump/funding/overbought проверки через `asyncio.gather` (56-95с → 44-58с)
- Таймауты pump/funding: 25с → 10с

## [7.9] — 2026-07-12

### Added
- **Unified SL Manager** (`unified_sl.py`): 5 механизмов SL объединены в один
  с приоритетом tight_trail > simple_trail > hard_trail > breakeven > default.
  Сокращает API-вызовы к Bybit на ~75% (16/цикл → 4/цикл).

### Fixed
- `unified_sl.py` не копировался в `bybit_ws/` при деплое → ModuleNotFoundError
- Спам «SL НЕ встал: not modified» в auto_sl.py — обрабатывается как no-op

### Changed
- smoke-тесты: 11 → 52

## [7.7] — 2026-07-04

### Changed
- README обновлён до v7.8
- AGENTS.md сжат по HumanLayer harness engineering

## [7.6] — 2026-07-01

### Added
- **Защита 4 стратегий от positions=list:** guard `isinstance(positions, dict)` в `check_funding_signals`, `check_funding_rotation`, `check_mean_revert`, `check_scalp_signals` — 57 ошибок/час устранены

### Fixed
- **RPC `_OLD_TOKENS` NameError:** handler крашился на каждом запросе — `self.` prefix восстановлен
- **`send_summary` missing 'label':** аргумент не передавался из `should_send_summary()`
- **`check_profit_triggers` missing 'positions':** краш каждые 30с тяжёлого цикла с 30 июня
- **ntfy push:** title с pipe-символом заменён на dash (RFC 7230 compliance)

---

## [7.5] — 2026-06-30

### Added
- **Post-trade features:** `save_trade_features()` при импорте закрытых сделок Bybit — сбор данных для self-learning
- **Traceback логирование:** `run_in_thread()` теперь логирует полный traceback, не «unhandled: ...»

### Fixed
- **12 критических багов:** аудит 30.06 — см. `00cdf9c`
- **Traceback truncation:** полные стеки ошибок в событиях
- **SL guard:** mark-based вместо entry-based (ATR-adaptive)

---

## [7.1] — 2026-06-28

### Added
- **BlackSwan multi-tier:** 3 уровня защиты: Tier 1 (BTC -3%/15min → close 50%), Tier 2 (BTC -5%/30min → close 80%), Tier 3 (BTC -8%/1h или PnL 2x → close 100%). Сортировка по PnL: худшие позиции закрываются первыми.
- **Canary mode для self-learning:** 10% входов используют новые параметры, 48ч окно оценки, авто-rollback при падении WR >10%, promote при WR >= baseline. Состояние в `canary_state.json`.
- **Volume Confirmation filter:** проверка объёма (vol vs SMA) перед входом, fail-open.
- **BB batch-префетчер:** параллельная загрузка BB для вотчлиста, кеш 5 мин.
- **Session Params:** адаптация BB/SL/TP/max_pos под NY/Asia/Weekend.
- **Post-trade кластерный анализ:** блокировка кластеров с WR <40%.

### Changed
- `check_black_swan()`: возвращает `(tier, reason, btc_drop)` вместо `(bool, reason)`
- `emergency_close_all()`: делегирует в `_emergency_close_pct()` с close_pct=1.0
- `apply_journal_insights()`: параметры идут в canary, а не применяются глобально
- `auto_entry.py`: canary-проверка min_score перед режимной/глобальной

### Fixed
- auto_tp: orders.values() list/dict fix
- ab_status: NoneType check
- Self-learning в main loop (2880 циклов)
- pump_state авто-очистка
- gridsignal-bot: ALTER TABLE crash
- DSPy → DeepSeek миграция
- pip-audit: per-package fix_versions

---

## [7.0] — 2026-06-27 (Фаза 7 завершена 29.06)

### Added
- **Paper Trading:** `paper_trading.py` — интеграция PaperExchange в main loop
  - Feature flag: `BYBIT_PAPER_ENABLED=1`
  - RPC: `/paper/balance`, `/paper/positions`, `/paper/summary`
  - Mark-цена обновляется из WS-кеша, PnL в реальном времени
  - Отдельная БД `paper_state.db` (не пересекается с реальными позициями)
- **Structured Logging:** `structured_log.py` — JSON-логи в `events.jsonl`
  - Feature flag: `STRUCTURED_LOGGING=1`
  - log_info/warn/error/critical + log_cycle
  - Ротация при 50 MB, совместимость с Grafana Loki
- **RPC paper endpoints:** 3 новых эндпоинта для paper-торговли
- **Graceful shutdown:** `while not SHUTDOWN` + SIGTERM/SIGINT обработка
- **Heavy cycle оптимизация:** 77s → 29.73s (asyncio.gather, 62% ускорение)
- **Monte Carlo бэктестинг:** 10K симуляций, Sharpe/Sortino/Calmar ratios
- **Kelly sizing:** f* = (p×b−q)/b, fractional 25%, per-symbol stats
- **Grafana dashboard:** 8 панелей (позиции, PnL, циклы, аптайм)
- **Entry Judge (LLM gate):** Nemotron → DeepSeek, 5s таймаут, fail-closed + CB
- **ATR-based TP (3 уровня):** 1.0×/2.0×/3.0× ATR (40/35/25% объёма)
- **ATR-adaptive SL v2:** 4 режима волатильности, k=1.3–2.5, capped ±50%
- **7 фильтров входа:** MTF + Orderbook + Volume + Entry Judge + Correlation + Post-trade + Risk

### Changed
- main_async.py: асинхронный главный цикл (30с)
- main_async.py: paper-блок (mark-цены + сводка каждые 10 циклов)
- rpc.py: +3 paper handlers
- config.py: `logging.structured` field
- Конфиг: feature flags (BYBIT_ATR_TP_ENABLED, BYBIT_DSPY_ENABLED, etc.)
- RPC: /kill_switch, /emergency_close, /circuit_breaker эндпоинты
- CAPABILITIES.md: +BYBIT_PAPER_ENABLED, +STRUCTURED_LOGGING flags
- ROADMAP.md: Фаза 7 закрыта (все ✅)

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
- **`/rpc/dashboard`** — HTML/SVG-дашборд с winrate, фандингом, маржой, режимом, корреляциями.
- **ATR hedge** (`hedge.py`) — защита от расширения ATR с авто-частичным хеджем.
- **TradingView webhook** (`webhook_handler.py`) — Flask-сервер на :9999, приём /webhook.
- **Cross-application sync** — единый `restart.sh` и `deploy.sh`.
- **MCP specs в AGENTS.md** — полные сигнатуры MCP-инструментов.
- **VPS-runbook** — CHECKLIST.md для деплоя на новый сервер.
- **Post-mortem bybit-ws баги(29.05)** — документированы и разобраны.

### Changed
- **position_sizing.py**: `margin_for_strategy()` — ATR-based, устаревшие стратегии удалены.
- **README.md**: переработан (таблицы, roadmap, архитектурные диаграммы).
- **MONITOR.md → AGENTS.md**: вся навигация для AI-агентов перенесена в AGENTS.md.

### Fixed
- **RPC `/positions`** — `list` response вместо `dict` (агенты падали с `.values()`).
- **Positions cache** — race condition WS/REST.
- **`/rpc/metrics`** — PnL=0 для SHORT (неправильный знак).
- **Funding rate alignment** — не плавать в минус на фандинге.

---

## [3.11] — 2026-06-15

### Added
- Circuit breaker: `max_daily_loss` / `max_total_margin` с принудительным кулдауном 24ч.
- **`force_instant_sl`**: SL на основе Immediate-or-Cancel (мгновенный отклик).
- **partial_tp.py**: 3 уровня (+7.5%, +15%, +22.5%) по 33%.
- **RPC `/metrics`**: Prometheus-совместимые метрики (total_pnl, win_rate, active_positions).
- **RPC `/enter`**: Market/Limit ордер + SL/TP в одном вызове.
- **position_sizing.py**: `margin_for_strategy()` — унифицированный расчёт маржи.
- **pump_detect.py**: индикатор перекупленности (>95% BB) для RSI-дивергенций.
- **`/archive`**: перемещает завершённые позиции в архив.

### Changed
- **state_db.py**: WAL mode + busy_timeout 5000ms.
- **main_async.py**: 6–10s таймаут для REST-снапшота.
- **rpc.py**: `/enter` — добавлены LiqPrice и CumRealisedPnl в ответ.

### Fixed
- **BICO zero-win cluster** — периодическая разблокировка (24ч).

---

## [3.10.1] — 2026-06-13

### Fixed
- **`instant_tp_symbols` хардкод:** удалён `NEARUSDT` из дефолтного конфига — мгновенно закрывал позицию при любом профите, делая x10 вход в NEAR невозможным.

---

## [3.10.0] — 2026-06-13

### Added
- **Junk Trail TP (`junk_trail.py`):** автоматическая фиксация прибыли JUNK-шортов. Профит >15% → TP подтягивается (70% фиксации), >30% → затягивается (85%).
- **DCA limit + cooldown** — 30 мин между DCA, дневной лимит, защита от перекупа.
- **`/auto_sl` override** auto-управление SL можно отключить.

### Changed
- **Partial TP**: 3 уровня → 3.0x ATR TP, переписана на едином `calc_partial_levels()`.

### Fixed
- **state_db**: race condition на 30+ таблицах.
- **Partial TP**: key collision.
- **Duplicate SL/TP ордера**: проверка перед `/trading-stop`.

---

## [3.9.1] — 2026-06-09

### Fixed
- **False «Лимитка сработала»:** `snapshot.py` различает заполнение и отмену по `cumExecQty`.

---

## [3.9.0] — 2026-06-09

### Added
- **RSI-дивергенции** — детектор дивергенций с кулдауном 24ч.
- **Sector Overlap** — блокировка дублирующихся секторов.
- **`/history` endpoint** — сводка за N дней.

### Changed
- **Risk-лимиты**: per-position max $50 margin, total $300.
- **refill режим**: маржа восполняется только при PnL > 0.

---

## [3.8.0] — 2026-06-09

### Added
- **Position Sizing v3.8:** динамическая маржа = депозит × risk_pct / max_positions × score_multiplier.
- **Correlation matrix** — парные корреляции на 1H свечах.
- **Emergency close** — закрытие всех позиций + 30 мин кулдаун.

---

## [3.7.0] — 2026-06-09

### Added
- **Dashboard v3.7:** SVG с winrate, funding, margin, regime, correlations.
- **Correlation tightening** — SL сужается при высокой корреляции.
- **Market regime** — Bollinger Bands Keltner (trending/ranging).

---

## [3.6.0] — 2026-06-08

### Added
- **X10 Strategy Pack:** BB Scalping M5, Mean Reversion Extreme, Funding Rate Momentum.
- **Funding Rotation** — поиск невыгодного фандинга.
- **Auto SHORT** — entry через funding + overbought + correlation.
- **Overbought filter** — те, кто >95% BB не шортятся.
- **Pump detection** — +5% за 15 мин → +8% за 30 мин.

---

## [3.5.0] — 2026-06-08

### Added
- DCA-лимиты: max_margin_per_symbol=80, max_dca_count=2.
- **Breakeven SL** — перенос SL в безубыток при +10%.

---

## [3.4.0] — 2026-06-08

### Added
- RPC auth: Bearer token, rate limiting (60 req/min).
- **Time-based exit:** >6ч/ >48ч.
- **Self-learning v2:** LSTM + XP boosting.

---

## [3.3.0] — 2026-06-08

### Added
- YAML config (`~/.config/bybit-ws/config.yaml`) с `${ENV}` подстановкой.
- **Banned clusters** — блокировка проигрышных паттернов.

---

## [3.0.0] — 2026-06-07

### Added
- **Bollinger Grid Monitor** — ядро системы, 30-секундные циклы.
- **ATR-adaptive SL** — SL = entry ± k×ATR(14) с 4 режимами.
- **Auto TP** — трёхуровневый take-profit.
- **Trailing SL** — подтяжка при движении в +.
- **Auto LONG** — Bollinger Grid Entry.
- **Circuit breaker** — защита от потерь.
- **RPC** (JSON-RPC на :8766).
- **SQLite SSOT** — state.db, trades, orders.
- **Telegram/ntfy алерты** — push-уведомления.
- **MTF Confluence** — D+W+M подтверждение.
- **Orderbook filter** — bid/ask ratio.
- **Volume confirmation** — vol/SMA(20) фильтр.
- **Entry Judge** — LLM gate (Nemotron → DeepSeek).
- **Post-trade cluster analysis** — блокировка кластеров с WR <40%.
- **Self-learning** — canary mode + авто-коррекция.
- **Paper Trading** — изолированная симуляция.

---

## [2.1] — 2026-06-09 (ретроспективно)

### Added
- **ML-скоринг сигналов** (`ml_scorer.py`) — RandomForest F1=0.69, 70/30 вес.
- **Walk-forward валидация** (`walkforward_rf.py`) — rolling window 30/7 дней, крафт 30 сплитов.

---

## [2.0] — 2026-06-08 (ретроспективно)

### Added
- **SQLite SSOT** — миграция с JSON на SQLite (WAL, 8 таблиц).
- **Journal + Self-learning** — анализ сделок и авто-коррекция.
- **Trailing SL** — подтяжка SL при движении.
- **Breakeven SL** — перенос в безубыток.
- **DCA** — частичный докут при -5% и -10%.
- **RL-оптимизация (SB3)** — PPO/DQN/SAC.
- **CI/CD** — GitHub Actions, gh-pages, авто-тесты.

---

## [1.0] — 2026-06-07 (ретроспективно)

### Added
- **Первая рабочая версия** Bollinger Grid монитора.
- REST API, позиции, SL, простые алерты.
- SQLite хранение.
- Начальная структура проекта.
