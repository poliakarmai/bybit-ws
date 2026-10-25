# Карта документации bybit-ws

> Обновлено 2026-10-25. Версия продукта — **11.5.0** (`pyproject.toml`, `CHANGELOG.md` §11.5.0).
> Правило: **если файл противоречит коду — прав код.** Живые числа: `state.db`, RPC `/metrics`/`/positions`, `verify.sh`.

## Читать (актуальное)

| Файл | О чём | Статус |
|------|-------|--------|
| [../AGENTS.md](../AGENTS.md) | Навигация для AI-агентов: структура, снимок метрик, RPC, тесты, ловушки | ✅ сверено 25.10 |
| [../README.md](../README.md) | Продуктовое описание для людей | ✅ 25.10 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Как устроено: цикл, модули, data flow, флаги | ✅ 25.10 |
| [API.md](API.md) | Реальные REST-маршруты из `rpc.py`, auth, WS-порт | ✅ переписан 25.10 |
| [CAPABILITIES.md](CAPABILITIES.md) | Функциональность: вход, SL/TP, JUNK, BlackSwan, флаги | ✅ 25.10 |
| [ROADMAP.md](ROADMAP.md) | План и статус фаз | ✅ 25.10 |
| [SECURITY.md](SECURITY.md) | Секреты, модель угроз, аудит | ⚠️ 14.10, проверить перед изменением security-контура |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 60+ причин поломок | ⚠️ 02.08, часть путей устарела — сверять с AGENTS.md |
| [SELF_LEARN.md](SELF_LEARN.md) | Самообучение v10 | ⚠️ 04.08, механики те же, числа/триггеры сверять с AGENTS.md |
| [history.md](history.md) | История фаз 1–8 | 📜 историческое |
| [PRD-one-click.md](PRD-one-click.md) | One-click торговля | 📜 ТЗ, не отчёт о реализации |
| [experiment-tree-design.md](experiment-tree-design.md), [experiment-tree-TZ.md](experiment-tree-TZ.md) | Experiment Tree: дизайн и ТЗ | ✅ соответствует `bybit_ws/experiments/` |
| [research/](research/) | A/B-эксперименты с датой и вердиктом | 📜 отчёты, не инструкции |
| [TZ-audit-bes-2026-10-23.md](TZ-audit-bes-2026-10-23.md) | ТЗ внешнего аудита (Фаза 11) | 📜 вход в Фазу 11 |
| [../REVIEW.md](../REVIEW.md) | Правила ревью (что считается поломкой) | ✅ 20.10 |
| [../CHANGELOG.md](../CHANGELOG.md) | История версий | ✅ 25.10 |
| [../ONBOARDING.md](../ONBOARDING.md) | Пошагово с нуля | ⚠️ 04.08 |

## Архив (не инструкция)

`docs/archive/` — файлы, описывающие удалённое поведение. Помечены баннером; читать только как историю:
`README-v4.md` (онбординг v4.x), `bybit-ws-full-2026-08.md` (v7.x), `openwiki/*` (English, v7.7),
`DESIGN-STRATEGIES-v3.12.md`. В корне исторические: [../DESIGN.md](../DESIGN.md) (v3.11.1),
[../DESIGN-STRATEGIES.md](../DESIGN-STRATEGIES.md).

## Инварианты документации

1. Цифры (сделки, PnL, WR, PF, комиссии) — только со снимком и датой; «на 25.10» без даты = не число.
2. Трендовые числа берутся из `trade_history` с `strategy='auto' AND manual=0`; `side` там — сторона **закрытия** (`Buy`=закрыл SHORT).
3. Деньги аккаунта — `transaction-log.change` (net) / `cashFlow` (gross), не `Σ trade_history.pnl`.
4. Эндпоинты — только те, что есть в `do_GET`/`do_POST` в `rpc.py`.
5. Флаги — «прод» = значение из `~/.config/bybit-ws/env`, «дефолт» = значение в коде; не смешивать.
6. Тесты: `python3 test_smoke.py` → 52; `python3 -m pytest -q --timeout=90` → 372 (25.10).
