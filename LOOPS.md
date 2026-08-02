# Project loops

## Self-improving trading parameter loop

Адаптация [The self-improving champion loop #023](https://signals.forwardfuture.com/loop-library/loops/self-improving-champion-loop/). Самообучающийся подбор параметров Bollinger Grid: champion/challenger с holdout-проверкой через canary-режим.

Saved: 2026-07-03

Prompt:
> Раз в сутки (каждые 2880 циклов монитора) читай журнал сделок из SQLite через `load_from_sqlite()`. Если canary не активен и WR < 0.40 — подними min_score на 30% и запусти canary: 10% входов с новыми параметрами. Сравнивай WR canary-сделок с baseline WR. Если canary WR упал больше чем на 10% — откати. Если не хуже — промоути. Стоп через 48ч или после 10 canary-сделок. Не меняй параметры глобально без canary-проверки. Пиши решение в `self_learn.jsonl`.
