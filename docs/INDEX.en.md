# bybit-ws documentation map

**English** · [Русский](INDEX.md)

> Updated 2026-10-27. Product version — **11.5.0** (`pyproject.toml`, `CHANGELOG.md` §11.5.0).
> Rule: **if a file contradicts the code, the code is right.** Live numbers: `state.db`, RPC `/metrics`/`/positions`, `verify.sh`.

## Read (current)

| File | About | Status |
|------|-------|--------|
| [../AGENTS.md](../AGENTS.md) | Navigation for AI agents: structure, metrics snapshot, RPC, tests, pitfalls | ✅ verified 25.10 |
| [../README.md](../README.md) | Product description for humans | ✅ 25.10 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How it works: loop, modules, data flow, flags | ✅ 25.10 |
| [API.md](API.md) | Actual REST routes from `rpc.py`, auth, WS port | ✅ rewritten 25.10 |
| [CAPABILITIES.md](CAPABILITIES.md) | Functionality: entry, SL/TP, JUNK, BlackSwan, flags | ✅ 25.10 |
| [ROADMAP.md](ROADMAP.md) | Phase plan and status | ✅ 25.10 |
| [SECURITY.md](SECURITY.md) | Secrets, threat model, audit | ⚠️ 14.10, review before changing the security perimeter |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 60+ failure causes | ⚠️ 02.08, some paths outdated — cross-check with AGENTS.md |
| [SELF_LEARN.md](SELF_LEARN.md) | Self-learning v10 | ⚠️ 04.08, mechanics unchanged; verify numbers/triggers against AGENTS.md |
| [history.md](history.md) | History of phases 1–8 | 📜 historical |
| [PRD-one-click.md](PRD-one-click.md) | One-click trading | 📜 spec, not an implementation report |
| [experiment-tree-design.md](experiment-tree-design.md), [experiment-tree-TZ.md](experiment-tree-TZ.md) | Experiment Tree: design and spec | ✅ matches `bybit_ws/experiments/` |
| [experiment-sweep-results.md](experiment-sweep-results.md) | Parameter sweep 16.09: 10 tickers × 15 variants — conclusion "edge is in symbol selection" | 📜 report, numbers dated 16.09 |
| [WEBHOOKS.md](WEBHOOKS.md) | Payload structure of alert events (ENTRY/SL/TP/…) for external consumers | ✅ 26.10 |
| [research/](research/) | A/B experiments with date and verdict | 📜 reports, not instructions |
| [TZ-audit-bes-2026-10-23.md](TZ-audit-bes-2026-10-23.md) | External audit spec (Phase 11) | 📜 input to Phase 11 |
| [../REVIEW.md](../REVIEW.md) | Review rules (what counts as broken) | ✅ 20.10 |
| [../CHANGELOG.md](../CHANGELOG.md) | Version history | ✅ 25.10 |
| [../ONBOARDING.md](../ONBOARDING.md) | Step by step from scratch | ⚠️ 04.08 |

## Archive (not instructions)

`docs/archive/` — files describing removed behavior. Marked with a banner; read only as history:
`README-v4.md` (v4.x onboarding), `bybit-ws-full-2026-08.md` (v7.x), `openwiki/*` (English, v7.7),
`DESIGN-STRATEGIES-v3.12.md`. Historical files in the root: [../DESIGN.md](../DESIGN.md) (v3.11.1),
[../DESIGN-STRATEGIES.md](../DESIGN-STRATEGIES.md).

## Documentation invariants

1. Numbers (trades, PnL, WR, PF, fees) — only with a snapshot and a date; "as of 25.10" without a date = not a number.
2. Trend numbers come from `trade_history` with `strategy='auto' AND manual=0`; `side` there is the **closing** side (`Buy` = closed a SHORT).
3. Account money — `transaction-log.change` (net) / `cashFlow` (gross), not `Σ trade_history.pnl`.
4. Endpoints — only those present in `do_GET`/`do_POST` in `rpc.py`.
5. Flags — "prod" = value from `~/.config/bybit-ws/env`, "default" = value in code; do not mix.
6. Tests: `python3 test_smoke.py` → 52/52; `python3 -m pytest -q` → all green (count not pinned, see run output).