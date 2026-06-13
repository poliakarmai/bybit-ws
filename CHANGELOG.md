# Changelog

All notable changes to bybit-ws.

---

## [3.10.0] — 2026-06-13

### Added
- **Junk Trail TP (`junk_trail.py`):** автоматическая фиксация прибыли JUNK-шортов. Профит >15% → TP подтягивается (70% фиксации), >30% → затягивается (85%). Вызывается каждые 5 циклов (2.5 мин).
- **Недельный памп-детект:** `check_weekly_pumps()` в `pump_detect.py`. Рост ≥230% за 7д + оборот ≥$1M → market SHORT, без SL/TP, макс 2 позиции.
- **Pipeline Trace v2:** классификация ордеров (SL/TP/LIMIT_ENTRY), дедупликация SL, детект зависших лимиток >48ч.
- **Дашборд — разделение ордеров:** 📥 Лимитки / 🎯 TP / 🛑 SL (3 виджета вместо одного)

### Fixed
- **pump_detect KeyError 'alerts':** запись `peak_price` до блока `if not prev` делала пустой dict непустым → KeyError при `prev['alerts'].append()`.
- **SL re-entry только для LONG:** `notify_sl_hit()` вызывался без проверки `side`, создавая ложную очередь на SHORT-позиции.
- **auto_sl.py пропускает JUNK-шорты:** проверка `pumps.json` — если символ помечен как памп-шорт, SL не ставится.
- **GridSignal бот — спам старыми SL:** SL-исходы пишутся в БД молча, уведомления только для TP.
- **VPN watch — ложные тревоги при idle:** critical только сервис + порт, нулевой трафик без клиентов = предупреждение.
- **Cancel-all сносит position-level SL:** фикс: отмена ордеров по одному через `orderId`.

### Changed
- **Формат JUNK/SHORT уведомлений:** компактный (вход/лимит/qty/плечо/памп%/TP/DCA в одной строке через `|`).
- **Кулдаун RSI-дивергенций:** 86400 (24ч) — устранён шум.
- **Конфиг:** `weekly_pump_threshold: 2.30` в секции `junk`.

---

## [3.9.1] — 2026-06-09

### Fixed
- **False «Лимитка сработала»:** `snapshot.py` теперь различает заполнение и отмену по `cumExecQty`. Отменённые без исполнения → «🗑️ Лимитка отменена»
- **Time budget в `check_auto_short`:** deadline 20с, early exit при исчерпании — устранены таймауты `_timed_call`

### Changed
- `api.py:fetch_orders()` сохраняет `cumExecQty` в снапшот ордера

## [3.8.0] — 2026-06-09

### Added
- **Position Sizing v3.8:** dynamic margin = deposit × risk_pct / max_positions × score_multiplier
- `position_sizing.py` module: `get_deposit()`, `calculate_margin()`, `margin_for_strategy()`
- Risk budgets per strategy: LONG 20%, x10 5%, DCA 10%, pump 6%
- Score multipliers: 8.5+→1.4, 7.5+→1.15, 6.5+→1.0, 5.5+→0.75
- Floor: $5 minimum, cap: max(MIN_MARGIN, 40% risk_budget)
- Integrated into all 7 entry modules (auto_entry, auto_short, sl_reentry, bb_scalp, mean_revert, funding_entry, pump_detect)

### Changed
- Fixed margins ($15/$10/$5) replaced with dynamic %-based calculation
- Config: added `position_sizing` section

### Fixed
- Position sizing cap bug: floor ($5) was overridden by cap on small deposits

---

## [3.7.0] — 2026-06-09

### Added
- **X10 Strategy Pack:** BB Scalping M5, Mean Reversion Extreme, Funding Rate Momentum
- **ATR Risk Sizing:** validates position size against ATR(14)
- **X10 Risk Limits:** daily loss stop (3 trades), 24h cooldown, correlation check
- **Junk short hard stop:** max_loss_pct=15%, max_hold_hours=48
- **Funding trend filter:** SHORT only when funding >0.1% + BB >85% + 3-day price decline
- **Strategy tag in trade journal:** `trades.md` and `trades.jsonl` include strategy name
- **Correlation dedup on x10 entries:** block entry if ≥2 correlated positions
- **Banned symbols:** config-driven permanent ban (`risk.banned_symbols`)

### Changed
- DESIGN.md and STRATEGIES.md updated to v3.7
- GridSignal Bot v4.1: `/scan scalp`, `/scan mean`, `/scan funding` with x10 scoring
- Correlation dedup: 24h TTL, pair-only hash

---

## [3.6.0] — 2026-06-08

### Added
- **Dashboard v3.7:** SVG with winrate, funding, margin, regime, correlations
- **Funding tracker:** extreme funding rate alerts (>0.1% / <−0.05%)
- **Margin alerts:** >80% ⚠️, >95% 🚨, >100% 🆘
- **Market regime classifier:** TRENDING_UP/DOWN, CHOPPY, HIGH/LOW_VOL (BTC+ETH)
- **Correlation matrix:** pair detection >0.8, concentration risk alerts
- **SHORT TP via trading-stop:** TP bundled with SL in single API call

### Fixed
- trades.jsonl dedup: 681 duplicates → 59 real trades
- Thread memory leak: stack_size 8MB → 2MB (x4 savings)
- GridSignal bot: unhandled exceptions now logged instead of swallowed
- LONG cooldown after SL: 4h pause prevents re-entry loop
- Cascade liquidation protection: market-close if price 2x closer to liquidation than SL

---

## [3.5.0] — 2026-06-08

### Added
- Graceful shutdown (SIGTERM): save positions, fix SL, exit cleanly
- Log rotation: events.log at 50MB, 7 files
- DCA limits: max_margin_per_symbol=80, max_dca_count=2

---

## [3.4.0] — 2026-06-08

### Added
- RPC auth: Bearer token, rate limiting (60 req/min)
- Notification format v3.4: cause + PnL (`🔴 SYM SL −$X.XX (entry $Y)`)
- SL re-entry fix: positionIdx loop (0→1) for AAVE/JTO
- bb_width filter removed for D/W/M (GridSignal)

### Fixed
- SHORT block: 4 bugs in auto_short (BB keys, TP direction, positionIdx, lower<=0)
- Watchdog spam: heavy checks skip when cycle >90s
- positionIdx inconsistency: always try 0 first, 1 on 10001

---

## [3.3.0] — 2026-06-08

### Added
- YAML config (`~/.config/bybit-ws/config.yaml`) with ${ENV} substitution
- _timed_call: timeout-based function calls (25s default)
- Docker support: Dockerfile + docker-compose.yml
- OpenAPI 3.0 schema (`openapi.yaml`)
- Python SDK (`bybit_ws_sdk.py`): Monitor + WebhookHandler
- DESIGN.md: full architecture documentation for AI agents

---

## [3.0.0] — 2026-06-07

### Added
- bybit-ws: Bollinger Grid monitor with 30s cycles
- LONG auto-entry: BB < 25% scoring
- SHORT auto-entry: BB > 85% overbought detection
- Auto SL/TP: trading-stop integration
- RPC server: port 8766, REST API
- SL re-entry ladder: -5%, -10%, -15% after stop
- Pump detection: DCA shorts on >120% daily pumps
- GridSignal Bot: Telegram bot with /scan, LONG/SHORT signals
