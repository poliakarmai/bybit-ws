"""
ab_test.py — A/B-тестирование ML Gate (Фаза 5.3).

Сплит 50/50: группа A (ML Gate) vs группа B (без фильтра).
Отслеживает сделки по группам, генерирует статистический отчёт.

Использование:
    from .ab_test import assign_group, record_entry, record_outcome, get_report
"""

import hashlib
import os
import sqlite3
import time
from typing import Optional

DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
DB_PATH = os.path.join(DATA_DIR, 'state.db')

SCHEMA_ADDON = """
CREATE TABLE IF NOT EXISTS ab_test (
    signal_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL CHECK(group_name IN ('A', 'B')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    outcome TEXT,
    ml_prob REAL,
    score INTEGER,
    created_at INTEGER,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ab_group ON ab_test(group_name);
CREATE INDEX IF NOT EXISTS idx_ab_outcome ON ab_test(outcome);
"""


def _get_conn():
    """Ленивое подключение к state.db."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.executescript(SCHEMA_ADDON)
    conn.commit()
    return conn


def assign_group(signal_id: str) -> str:
    """
    Детерминированное назначение группы по хешу signal_id.
    Один signal_id → всегда одна группа. Сплит ~50/50.
    """
    h = hashlib.md5(signal_id.encode()).hexdigest()
    return 'A' if int(h[:8], 16) % 2 == 0 else 'B'


def record_entry(signal_id: str, symbol: str, side: str, qty: float,
                 entry_price: float, score: int, ml_prob: Optional[float] = None) -> str:
    """Записать вход в позицию. Возвращает назначенную группу."""
    group = assign_group(signal_id)
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO ab_test
            (signal_id, group_name, symbol, side, qty, entry_price, score, ml_prob, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, group, symbol, side, qty, entry_price, score, ml_prob, int(time.time())))
    conn.commit()
    conn.close()
    return group


def record_outcome(signal_id: str, outcome: str, exit_price: float, pnl: float):
    """Записать исход закрытой сделки."""
    conn = _get_conn()
    conn.execute("""
        UPDATE ab_test SET outcome=?, exit_price=?, pnl=?, closed_at=?
        WHERE signal_id=?
    """, (outcome, exit_price, pnl, int(time.time()), signal_id))
    conn.commit()
    conn.close()


def record_outcome_for_symbol(symbol: str, outcome: str, exit_price: float, pnl: float) -> bool:
    """
    Записать исход для символа — находит последнюю открытую запись.
    Возвращает True если запись найдена и обновлена.
    """
    conn = _get_conn()
    row = conn.execute("""
        SELECT signal_id FROM ab_test
        WHERE symbol=? AND outcome IS NULL
        ORDER BY created_at DESC LIMIT 1
    """, (symbol,)).fetchone()
    if row:
        signal_id = row[0]
        conn.execute("""
            UPDATE ab_test SET outcome=?, exit_price=?, pnl=?, closed_at=?
            WHERE signal_id=?
        """, (outcome, exit_price, pnl, int(time.time()), signal_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def get_report() -> dict:
    """
    Сравнительный отчёт групп A vs B.
    Возвращает словарь с метриками и t-тестом (scipy при наличии).
    """
    conn = _get_conn()

    total = conn.execute("SELECT COUNT(*) FROM ab_test").fetchone()[0]
    if total == 0:
        conn.close()
        return {'status': 'no_data', 'total_trades': 0}

    stats = {}
    for group in ('A', 'B'):
        closed = conn.execute("""
            SELECT COUNT(*), AVG(pnl), SUM(pnl),
                   SUM(CASE WHEN outcome='TP' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome='SL' THEN 1 ELSE 0 END)
            FROM ab_test
            WHERE group_name=? AND outcome IS NOT NULL
        """, (group,)).fetchone()

        total_group = conn.execute(
            "SELECT COUNT(*) FROM ab_test WHERE group_name=?", (group,)
        ).fetchone()[0]

        open_group = conn.execute(
            "SELECT COUNT(*) FROM ab_test WHERE group_name=? AND outcome IS NULL",
            (group,)
        ).fetchone()[0]

        n, avg_pnl, sum_pnl, tp_count, sl_count = closed
        n = n or 0
        avg_pnl = float(avg_pnl) if avg_pnl else 0.0
        sum_pnl = float(sum_pnl) if sum_pnl else 0.0
        tp_count = tp_count or 0
        sl_count = sl_count or 0
        winrate = tp_count / n if n > 0 else 0

        # Max drawdown (cumulative PnL)
        rows = conn.execute("""
            SELECT pnl FROM ab_test
            WHERE group_name=? AND outcome IS NOT NULL
            ORDER BY closed_at ASC
        """, (group,)).fetchall()

        cumulative = 0.0
        max_dd = 0.0
        peak = 0.0
        for (pnl_val,) in rows:
            cumulative += pnl_val
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Simplified Sharpe
        pnls = [r[0] for r in rows]
        mean_ret = sum(pnls) / len(pnls) if pnls else 0
        std_ret = (sum((r - mean_ret) ** 2 for r in pnls) / len(pnls)) ** 0.5 if len(pnls) > 1 else 0
        sharpe = (mean_ret / std_ret * (len(pnls) ** 0.5)) if std_ret > 0 else 0

        stats[group] = {
            'name': 'ML Gate' if group == 'A' else 'Baseline (без ML)',
            'total': total_group,
            'open': open_group,
            'closed': n,
            'winrate': round(winrate, 3),
            'tp_count': tp_count or 0,
            'sl_count': sl_count or 0,
            'avg_pnl': round(avg_pnl, 2) if avg_pnl else 0.0,
            'sum_pnl': round(sum_pnl, 2) if sum_pnl else 0.0,
            'max_drawdown': round(max_dd, 2),
            'sharpe': round(sharpe, 3),
        }

    # Welch's t-test (scipy — optional)
    p_value = None
    significance = None
    try:
        from scipy import stats as scipy_stats
        pnls_a = [r[0] for r in conn.execute(
            "SELECT pnl FROM ab_test WHERE group_name='A' AND outcome IS NOT NULL"
        ).fetchall()]
        pnls_b = [r[0] for r in conn.execute(
            "SELECT pnl FROM ab_test WHERE group_name='B' AND outcome IS NOT NULL"
        ).fetchall()]
        if len(pnls_a) >= 5 and len(pnls_b) >= 5:
            t_stat, p_value = scipy_stats.ttest_ind(pnls_a, pnls_b, equal_var=False)
            significance = 'significant' if p_value < 0.05 else 'not_significant'
    except ImportError:
        pass

    conn.close()

    return {
        'status': 'ok',
        'total_trades': total,
        'groups': stats,
        'p_value': round(float(p_value), 4) if p_value is not None else None,
        'significance': significance,
        'recommendation': _recommendation(stats, significance),
    }


def _recommendation(stats: dict, significance: str | None) -> str:
    """Человекочитаемая рекомендация по результатам теста."""
    a = stats.get('A', {})
    b = stats.get('B', {})
    n_a, n_b = a.get('closed', 0), b.get('closed', 0)

    if n_a < 10 or n_b < 10:
        return f'Недостаточно данных: A={n_a}, B={n_b}. Нужно ≥10 закрытых сделок в каждой группе.'

    sum_a, sum_b = a.get('sum_pnl', 0), b.get('sum_pnl', 0)

    if significance == 'significant':
        if sum_a > sum_b:
            return f'✅ ML Gate значимо лучше (p<0.05). A: ${sum_a:.0f} vs B: ${sum_b:.0f}. Оставить.'
        else:
            return f'⚠️ Baseline значимо лучше (p<0.05). A: ${sum_a:.0f} vs B: ${sum_b:.0f}. Убрать ML Gate.'
    else:
        if sum_a > sum_b:
            return f'📊 ML Gate лучше, но статистически незначимо. A: ${sum_a:.0f} vs B: ${sum_b:.0f}. Нужно больше данных.'
        else:
            return f'📊 Сопоставимо. A: ${sum_a:.0f} vs B: ${sum_b:.0f}. Продолжить тест.'


def reset_test():
    """Сбросить A/B тест (очистить таблицу)."""
    conn = _get_conn()
    conn.execute("DELETE FROM ab_test")
    conn.commit()
    conn.close()


# ── CLI ─────────────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'report':
        print(json.dumps(get_report(), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == 'reset':
        reset_test()
        print("A/B test reset.")
    else:
        print("Usage: python ab_test.py [report|reset]")
