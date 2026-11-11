"""Тесты подводки per_symbol к n_eff-гейту self-learn.

Регрессия 11.11.2026: apply_journal_insights читал journal["per_symbol"],
которого никогда не было в ответе analyze_from_roundtrips → n_eff всегда 0.0 →
гейт N_EFF_MIN=100 молча блокировал самообучение (self_learn_skip).
Проверяем, что (1) ключ per_symbol теперь есть на верхнем уровне журнала,
(2) имена полей совпадают с контрактом гейта (total_trades, а не trades),
(3) n_eff по формуле гейта на реальных данных не нулевой.
"""
import os
import sys

os.environ["BYBIT_LOG_SILENT"] = "1"
sys.path.insert(0, "/home/openclaw")

from bybit_ws.journal import analyzer


def _rt(symbol: str, pnl: float, hold_hours: float = 10.0) -> analyzer.RoundTrip:
    return analyzer.RoundTrip(
        symbol=symbol, buy_ts=1000.0, sell_ts=2000.0, qty=1.0,
        buy_price=100.0, sell_price=100.0, hold_hours=hold_hours,
        pnl=pnl, pnl_pct=pnl / 100.0,
    )


def _n_eff(per_symbol: dict) -> float:
    """Та же формула, что в apply_journal_insights (self_learn.py:620-625)."""
    weights = [max(1, s.get("total_trades", 0)) for s in per_symbol.values()]
    sum_w = sum(weights)
    sum_w2 = sum(w * w for w in weights)
    return (sum_w ** 2) / sum_w2 if sum_w2 > 0 else 0.0


def test_per_symbol_present_and_contract():
    rts = [
        _rt("AAA", 10.0), _rt("AAA", -5.0), _rt("AAA", 1.0, hold_hours=2.0),
        _rt("BBB", 3.0),
    ]
    j = analyzer.analyze_from_roundtrips(rts)

    ps = j["per_symbol"]
    # ключ верхнего уровня есть и не пустой
    assert ps, "per_symbol пустой — гейт снова увидит n_eff=0"
    # контракт гейта: total_trades / win_rate / total_pnl / avg_hold_hours
    assert set(ps["AAA"].keys()) == {"total_trades", "win_rate", "total_pnl", "avg_hold_hours"}
    assert ps["AAA"]["total_trades"] == 3
    assert ps["AAA"]["win_rate"] == round(2 / 3, 3)   # 2 вин из 3
    assert ps["AAA"]["total_pnl"] == 6.0               # 10 - 5 + 1
    assert ps["AAA"]["avg_hold_hours"] == round((10 + 10 + 2) / 3, 1)
    assert ps["BBB"]["total_trades"] == 1


def test_n_eff_nonzero_and_matches_concentration():
    # 2 символа: AAA=3 сделки, BBB=1 → n_eff = (3+1)^2 / (9+1) = 16/10 = 1.6
    rts = [_rt("AAA", 1.0), _rt("AAA", 1.0), _rt("AAA", 1.0), _rt("BBB", 1.0)]
    j = analyzer.analyze_from_roundtrips(rts)
    n = _n_eff(j["per_symbol"])
    assert abs(n - 1.6) < 1e-9, n

    # равные веса → n_eff = число символов (проверка, что не залипаем на 0)
    rts2 = [_rt("AAA", 1.0), _rt("BBB", 1.0), _rt("CCC", 1.0)]
    j2 = analyzer.analyze_from_roundtrips(rts2)
    assert abs(_n_eff(j2["per_symbol"]) - 3.0) < 1e-9


def test_empty_roundtrips_yields_empty_per_symbol():
    j = analyzer.analyze_from_roundtrips([])
    assert j["per_symbol"] == {}


def test_n_eff_gate_pass_on_realistic_concentration():
    """Калибровка N_EFF_MIN: реалистичная концентрация (~90 символов, длинный хвост)
    должен проходить гейт, иначе self-learn снова заблокируется."""
    from bybit_ws.journal import self_learn as sl

    # ~90 символов: топ ~5-19 сделок, хвост 1-2 (как в проде)
    counts = [19, 11, 11, 8, 7, 6, 5, 5, 4, 4, 4, 3, 3, 3, 3] + [2] * 25 + [1] * 50
    per_symbol = {f"S{i:02d}": {"total_trades": c} for i, c in enumerate(counts)}
    n = _n_eff(per_symbol)
    assert n >= sl.N_EFF_MIN, f"n_eff={n:.1f} < N_EFF_MIN={sl.N_EFF_MIN} — гейт снова заблокирует"


def test_n_eff_gate_blocks_degenerate_concentration():
    """Вырожденная выборка (1 символ доминирует) должна оставаться под порогом."""
    from bybit_ws.journal import self_learn as sl

    per_symbol = {
        "DOMINANT": {"total_trades": 100},
        "tiny1": {"total_trades": 1},
        "tiny2": {"total_trades": 1},
    }
    n = _n_eff(per_symbol)
    assert n < sl.N_EFF_MIN, f"n_eff={n:.1f} должен быть < N_EFF_MIN={sl.N_EFF_MIN}"
