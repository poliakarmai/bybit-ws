"""SMA200-режимный гейт: split-half walk-forward эксперимент.

Проверяемая гипотеза: «вход только по направлению BTC относительно SMA200»
(BULL → только LONG, BEAR → только SHORT). На 346 реальных сделках гейт дал
+$254 против +$140 факта, но BULL-период — один эпизод 9 недель, BEAR — один
эпизод 3.5 месяца. Два эпизода — не выборка, поэтому здесь та же идея гоняется
на воспроизводимых симулированных сделках (engine.simulate) с проверкой
устойчивости на двух половинах окна.

Порядок: качаем свечи через engine.fetch_klines (дисковый кэш → прогон
воспроизводим), симулируем Bollinger Grid по DEFAULT_PARAMS, каждую сделку
мапим в режим BTC на её entry_time, сравниваем ungated vs gated, затем режем
книгу пополам по entry_time и смотрим, улучшает ли гейт PnL в ОБЕИХ половинах.

Боевой state.db, live-код и сервис не трогаются. Разовый эксперимент:
    python3 -m bybit_ws.experiments.sma200_walkforward
"""
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bybit_ws.experiments import engine  # noqa: E402

SYMBOLS = ["STXUSDT", "DOTUSDT", "ONDOUSDT", "SOLUSDT", "ETHUSDT", "BTCUSDT"]
REGIME_SYMBOL = "BTCUSDT"
DAYS = 540
INTERVAL = "D"
INITIAL_BALANCE = 1000.0
SMA_PERIOD = 200
GATE_RULES: dict[str, set[str]] = {"Buy": {"BULL"}, "Sell": {"BEAR"}}  # Buy = LONG, Sell = SHORT


# ── чистые функции (без сети/движка — их проверяет tests/) ─────

def sma(values: list[float], period: int = 200) -> list[float | None]:
    """Скользящее среднее той же длины, что input; None, пока j + 1 < period."""
    out: list[float | None] = []
    running = 0.0
    for j, v in enumerate(values):
        running += v
        if j >= period:
            running -= values[j - period]
        out.append(running / period if j + 1 >= period else None)
    return out


def classify_regime(btc_close: float, btc_sma: float | None) -> str:
    """BULL если цена выше SMA, BEAR если на уровне или ниже, иначе UNKNOWN."""
    if btc_sma is None:
        return "UNKNOWN"
    return "BULL" if btc_close > btc_sma else "BEAR"


def bucket(trades: list[dict], regime_at: dict[int, str]) -> dict[tuple[str, str], dict]:
    """Разложить сделки по (side, regime); gross_loss положительный.

    Сделка с entry_time, которого нет в regime_at, попадает в UNKNOWN.
    """
    out: dict[tuple[str, str], dict] = {}
    for t in trades:
        side = str(t.get("side"))
        entry_time = t.get("entry_time")
        regime = "UNKNOWN" if entry_time is None else regime_at.get(int(entry_time), "UNKNOWN")
        b = out.setdefault((side, regime), {"n": 0, "pnl": 0.0, "wins": 0,
                                            "gross_win": 0.0, "gross_loss": 0.0})
        pnl = float(t.get("pnl", 0.0))
        b["n"] += 1
        b["pnl"] += pnl
        if pnl > 0:
            b["wins"] += 1
            b["gross_win"] += pnl
        else:
            b["gross_loss"] += -pnl
    return out


def gate(trades: list[dict], regime_at: dict[int, str],
         allowed: dict[str, set[str]]) -> list[dict]:
    """Оставить только сделки, чей (side, regime) разрешён. Нет entry_time → дроп."""
    kept: list[dict] = []
    for t in trades:
        entry_time = t.get("entry_time")
        if entry_time is None:
            continue
        side = str(t.get("side"))
        regime = regime_at.get(int(entry_time), "UNKNOWN")
        if regime in allowed.get(side, set()):
            kept.append(t)
    return kept


# ── метрики/вывод ─────────────────────────────────────────────

def _metrics(trades: list[dict]) -> dict:
    """n / PnL / WR / PF.

    WR и PF берём из engine.compute_metrics (пустая equity-кривая: drawdown и
    sharpe там meaningless), а PnL считаем суммой по сделкам — при пустой
    кривой compute_metrics возвращает final_equity = initial_balance, т.е.
    total_pnl всегда 0.
    """
    m = engine.compute_metrics(trades, [], INITIAL_BALANCE, INTERVAL)
    return {"n": m["n_trades"], "pnl": sum(float(t["pnl"]) for t in trades),
            "wr": m["win_rate"], "pf": m["profit_factor"]}


def _fmt(num: float) -> str:
    """Pf == None в compute_metrics означает inf (нет убытков)."""
    return "inf" if num is None else f"{num:.2f}"


def _head(title: str) -> None:
    print(f"\n{title}")
    print(f"{'':<26}{'n':>5}{'PnL$':>10}{'WR%':>8}{'PF':>8}")


def _row(name: str, s: dict) -> None:
    print(f"{name:<26}{s['n']:>5}{s['pnl']:>10.2f}{s['wr']:>8.1f}{_fmt(s['pf']):>8}")


def main() -> None:
    end_ms = engine.resolve_end_ms(None)
    start_ms = end_ms - int(timedelta(days=DAYS).total_seconds() * 1000)

    all_trades: list[dict] = []
    for sym in SYMBOLS:
        try:
            klines = engine.fetch_klines(sym, INTERVAL, start_ms, end_ms)
            trades, _equity = engine.simulate(klines, sym, INITIAL_BALANCE,
                                             dict(engine.DEFAULT_PARAMS))
        except Exception as exc:  # один сломанный символ не должен убивать прогон
            print(f"{sym}: пропуск — {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for t in trades:
            t["symbol"] = sym
        all_trades.extend(trades)
        print(f"  {sym}: {len(trades)} сделок, {len(klines)} свечей", file=sys.stderr)

    if not all_trades:
        print("Сделок нет — эксперимент пуст.")
        return

    # Режим BTC на том же окне (и том же интервале → open_time совпадают со сделками)
    try:
        btc = engine.fetch_klines(REGIME_SYMBOL, INTERVAL, start_ms, end_ms)
    except Exception as exc:
        print(f"{REGIME_SYMBOL}: не удалось получить свечи — "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return

    sma_vals = sma([k["close"] for k in btc], SMA_PERIOD)
    regime_at = {k["open_time"]: classify_regime(k["close"], sv)
                 for k, sv in zip(btc, sma_vals)}
    counts = Counter(regime_at.values())

    print(f"\n{REGIME_SYMBOL} {INTERVAL}: {len(btc)} баров, SMA{SMA_PERIOD}: "
          f"BULL {counts['BULL']} / BEAR {counts['BEAR']} / UNKNOWN {counts['UNKNOWN']}")

    # Таблица по (side, regime)
    print(f"\n{'side':<8}{'regime':<10}{'n':>5}{'PnL$':>10}{'WR%':>8}{'PF':>8}")
    for (side, regime), b in sorted(bucket(all_trades, regime_at).items(),
                                    key=lambda kv: (kv[0][0] or "", kv[0][1])):
        wr = 100 * b["wins"] / b["n"] if b["n"] else 0.0
        pf = b["gross_win"] / b["gross_loss"] if b["gross_loss"] > 0 else (
            float("inf") if b["gross_win"] > 0 else 0.0)
        print(f"{str(side):<8}{regime:<10}{b['n']:>5}{b['pnl']:>10.2f}{wr:>8.1f}"
              f"{_fmt(pf) if not isinstance(pf, float) or pf == float('inf') else f'{pf:.2f}':>8}")

    gated = gate(all_trades, regime_at, GATE_RULES)
    base, gated_all = _metrics(all_trades), _metrics(gated)

    _head("Ungated vs gated (вся книга)")
    _row("ungated", base)
    _row("gated", gated_all)
    print(f"отфильтровано: {base['n'] - gated_all['n']} сделок "
          f"({100 * (base['n'] - gated_all['n']) / base['n']:.0f}%)")

    # ── split-half: старейшая половина vs новейшая половина ──
    ordered = sorted(all_trades, key=lambda t: t.get("entry_time") or 0)
    mid = len(ordered) // 2
    halves = [("ПОЛОВИНА 1 (старее)", ordered[:mid]), ("ПОЛОВИНА 2 (новее)", ordered[mid:])]

    improves: list[bool] = []
    for name, part in halves:
        pg = gate(part, regime_at, GATE_RULES)
        b, g = _metrics(part), _metrics(pg)
        improves.append(g["pnl"] > b["pnl"])
        _head(name)
        _row("baseline", b)
        _row("gated", g)
        print(f"{'дельта PnL':<26}{g['pnl'] - b['pnl']:>+10.2f}")

    stable = all(improves)
    verdict = ("УСТОЙЧИВО" if stable
               else "НЕ УСТОЙЧИВО (эффект на одной половине)")
    print(f"\nвердикт: {verdict}")

    if stable:
        sentence = ("гейт улучшает PnL в обеих половинах окна — гипотеза "
                    "переживает out-of-sample разбиение и заслуживает OOS-теста на бумаге.")
    else:
        sentence = ("выигрыш гейта сосредоточен в одной половине окна — это "
                    "артефакт одного эпизода, а не режимное преимущество.")
    print(f"\nИТОГ: гейт {'устойчив' if stable else 'не устойчив'} — {sentence}")


if __name__ == "__main__":
    main()
