"""
ab_test.py — A/B-тестирование стратегий (Фаза 5.3 ROADMAP).

Случайное назначение варианта A (текущие параметры) или B (изменённые параметры:
другие SL/TP пороги, min_score, BB-период).
Для каждого сигнала открываются paper-позиции для ОБОИХ вариантов,
реальная позиция — только для назначенного варианта.
Метрики считаются раздельно для A и B.
Статистическая значимость — t-test + bootstrap после 30+ сделок.

Feature flag: BYBIT_AB_ENABLED (env, default 0).

Использование:
    from .ab_test import (
        AbTestManager, is_ab_enabled, assign_variant,
        record_paper_entry, record_paper_exit,
        record_real_exit, get_status, get_report,
    )

SQLite таблица: ab_results (в state.db)
"""

import hashlib
import math
import os
import random
import sqlite3
import time
from typing import Optional

# ── Пути ─────────────────────────────────────────────────────
DATA_DIR = os.path.expanduser('~/.local/share/bybit-ws')
DB_PATH = os.path.join(DATA_DIR, 'state.db')

# ── Feature flag ─────────────────────────────────────────────
AB_ENABLED = os.environ.get('BYBIT_AB_ENABLED', '0') == '1'

# ── Параметры вариантов ─────────────────────────────────────
# Вариант A — текущие параметры (базовые)
VARIANT_A_PARAMS = {
    'sl_offset': 0.07,        # -7% SL от Lower BB (LONG)
    'tp_middle_pct': 0.20,    # 20% на Middle BB
    'tp_upper_pct': 0.80,     # 80% на Upper BB
    'min_score': 25,          # мин скор для входа
    'bb_period': 20,          # период BB
    'bb_std': 2,              # множитель стд-отклонения
    'sl_tier_ab': 0.05,       # +5% SL для SHORT Tier A/B
    'sl_tier_cd': 0.07,       # +7% SL для SHORT Tier C/D
    'bb_threshold': 85,       # BB% > порог → SHORT
}

# Вариант B — изменённые параметры (более агрессивные/консервативные)
VARIANT_B_PARAMS = {
    'sl_offset': 0.10,        # -10% SL — шире стоп (менее агрессивный)
    'tp_middle_pct': 0.30,    # 30% на Middle (держим дольше)
    'tp_upper_pct': 0.70,     # 70% на Upper
    'min_score': 20,          # ниже порог — больше входов
    'bb_period': 30,          # период BB 30 (более плавный)
    'bb_std': 2.5,            # шире полосы
    'sl_tier_ab': 0.07,       # +7% SL (шире)
    'sl_tier_cd': 0.09,       # +9% SL (шире)
    'bb_threshold': 80,       # более низкий порог → чаще шорт
}


# ── SQLite schema ────────────────────────────────────────────

SCHEMA_AB_RESULTS = """
CREATE TABLE IF NOT EXISTS ab_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    variant TEXT NOT NULL CHECK(variant IN ('A', 'B')),
    is_paper INTEGER NOT NULL DEFAULT 1,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT,
    signal_score REAL,
    variant_params TEXT,
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    pnl REAL,
    win INTEGER,
    fees REAL DEFAULT 0,
    entry_ts INTEGER,
    exit_ts INTEGER,
    created_at INTEGER DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_ab_variant ON ab_results(variant);
CREATE INDEX IF NOT EXISTS idx_ab_symbol ON ab_results(symbol);
CREATE INDEX IF NOT EXISTS idx_ab_paper ON ab_results(is_paper);
CREATE INDEX IF NOT EXISTS idx_ab_signal ON ab_results(signal_id);
CREATE INDEX IF NOT EXISTS idx_ab_closed ON ab_results(exit_ts);
"""


def _get_conn() -> sqlite3.Connection:
    """Ленивое подключение к state.db с созданием таблиц."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA_AB_RESULTS)
    conn.commit()
    return conn


# ── Вспомогательные функции ──────────────────────────────────

def is_ab_enabled() -> bool:
    """Проверить, включён ли A/B-тест."""
    return AB_ENABLED


def _generate_signal_id(symbol: str, side: str, ts: Optional[float] = None) -> str:
    """Сгенерировать уникальный ID сигнала."""
    if ts is None:
        ts = time.time()
    seed = f"{symbol}:{side}:{ts}:{random.randint(0, 999999)}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def assign_variant(symbol: str, side: str, score: float = 0) -> str:
    """
    Случайное назначение варианта A или B для сигнала.
    Возвращает 'A' или 'B'.
    Сплит ~50/50 через random.
    """
    if not AB_ENABLED:
        return 'A'  # Fallback: всегда A если тест выключен
    return 'A' if random.random() < 0.5 else 'B'


def get_variant_params(variant: str) -> dict:
    """Получить параметры для заданного варианта."""
    if variant == 'A':
        return dict(VARIANT_A_PARAMS)
    return dict(VARIANT_B_PARAMS)


# ── Запись сделок ────────────────────────────────────────────

def record_paper_entry(
    signal_id: str,
    variant: str,
    symbol: str,
    side: str,
    strategy: str,
    entry_price: float,
    qty: float,
    signal_score: float = 0,
) -> int:
    """
    Записать paper-вход в ab_results.
    Возвращает row id записи.
    """
    if not AB_ENABLED:
        return 0
    conn = _get_conn()
    params_json = str(get_variant_params(variant))
    cursor = conn.execute("""
        INSERT INTO ab_results
            (signal_id, variant, is_paper, symbol, side, strategy,
             signal_score, variant_params, entry_price, qty, entry_ts)
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id, variant, symbol, side, strategy,
        signal_score, params_json, entry_price, qty, int(time.time())
    ))
    conn.commit()
    row_id = cursor.lastrowid or 0
    conn.close()
    return row_id


def record_paper_exit(
    signal_id: str,
    variant: str,
    exit_price: float,
    pnl: float,
    win: bool,
    fees: float = 0,
):
    """
    Записать закрытие paper-позиции в ab_results.
    Находит последнюю открытую paper-запись для signal_id+variant.
    """
    if not AB_ENABLED:
        return
    conn = _get_conn()
    conn.execute("""
        UPDATE ab_results
        SET exit_price = ?, pnl = ?, win = ?, fees = ?, exit_ts = ?
        WHERE signal_id = ? AND variant = ? AND is_paper = 1
          AND exit_ts IS NULL
        ORDER BY id DESC LIMIT 1
    """, (exit_price, pnl, int(win), fees, int(time.time()), signal_id, variant))
    conn.commit()
    conn.close()


def record_real_exit(
    signal_id: str,
    variant: str,
    symbol: str,
    side: str,
    strategy: str,
    entry_price: float,
    exit_price: float,
    qty: float,
    pnl: float,
    win: bool,
    signal_score: float = 0,
    fees: float = 0,
):
    """
    Записать реальную сделку (один signal_id → одна запись, is_paper=0).
    """
    if not AB_ENABLED:
        return
    conn = _get_conn()
    params_json = str(get_variant_params(variant))
    conn.execute("""
        INSERT INTO ab_results
            (signal_id, variant, is_paper, symbol, side, strategy,
             signal_score, variant_params, entry_price, exit_price,
             qty, pnl, win, fees, entry_ts, exit_ts)
        VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id, variant, symbol, side, strategy,
        signal_score, params_json, entry_price, exit_price,
        qty, pnl, int(win), fees, int(time.time()), int(time.time())
    ))
    conn.commit()
    conn.close()


# ── Метрики ──────────────────────────────────────────────────

def _compute_metrics(rows: list[dict]) -> dict:
    """
    Вычислить метрики для списка закрытых сделок.
    winrate, avg PnL, max drawdown, profit factor, Sharpe.
    """
    if not rows:
        return {
            'closed': 0,
            'winrate': 0,
            'avg_pnl': 0,
            'sum_pnl': 0,
            'max_drawdown': 0,
            'profit_factor': 0,
            'sharpe': 0,
            'tp_count': 0,
            'sl_count': 0,
        }

    n = len(rows)
    tp_count = sum(1 for r in rows if r.get('win'))
    sl_count = n - tp_count
    winrate = tp_count / n if n > 0 else 0

    pnls = [r['pnl'] for r in rows]
    sum_pnl = sum(pnls)
    avg_pnl = sum_pnl / n if n > 0 else 0

    # Profit factor: sum(gains) / sum(|losses|)
    gains = sum(p for p in pnls if p > 0)
    losses = sum(abs(p) for p in pnls if p < 0)
    profit_factor = gains / losses if losses > 0 else (float('inf') if gains > 0 else 0)

    # Max drawdown (cumulative PnL)
    cumulative = 0.0
    max_dd = 0.0
    peak = 0.0
    for p in pnls:
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (упрощённый)
    mean_ret = avg_pnl
    if n > 1:
        std_ret = math.sqrt(sum((p - mean_ret) ** 2 for p in pnls) / n)
    else:
        std_ret = 0
    sharpe = (mean_ret / std_ret * math.sqrt(n)) if std_ret > 0 else 0

    return {
        'closed': n,
        'winrate': round(winrate, 4),
        'avg_pnl': round(avg_pnl, 4),
        'sum_pnl': round(sum_pnl, 4),
        'max_drawdown': round(max_dd, 4),
        'profit_factor': round(profit_factor, 4) if profit_factor != float('inf') else None,
        'sharpe': round(sharpe, 4),
        'tp_count': tp_count,
        'sl_count': sl_count,
    }


# ── Статистическая значимость ────────────────────────────────

def _bootstrap_test(pnls_a: list[float], pnls_b: list[float], n_bootstrap: int = 10000) -> dict:
    """
    Bootstrap-тест разницы средних.
    Возвращает p-value без внешних зависимостей (нет scipy).
    """
    import random as _random

    if len(pnls_a) < 5 or len(pnls_b) < 5:
        return {'p_value': None, 'method': 'bootstrap', 'note': 'need ≥5 trades each'}

    obs_diff = sum(pnls_a) / len(pnls_a) - sum(pnls_b) / len(pnls_b)
    combined = pnls_a + pnls_b
    n_a = len(pnls_a)

    count_extreme = 0
    for _ in range(n_bootstrap):
        _random.shuffle(combined)
        sample_a = combined[:n_a]
        sample_b = combined[n_a:]
        boot_diff = sum(sample_a) / n_a - sum(sample_b) / len(sample_b)
        if abs(boot_diff) >= abs(obs_diff):
            count_extreme += 1

    p_value = count_extreme / n_bootstrap
    return {
        'p_value': round(p_value, 4),
        'method': 'bootstrap',
        'n_bootstrap': n_bootstrap,
        'observed_diff': round(obs_diff, 4),
    }


def _welch_ttest(pnls_a: list[float], pnls_b: list[float]) -> dict:
    """
    Welch's t-test без scipy. Вычисляет t-статистику и приближённый p-value
    через нормальную аппроксимацию.
    """
    n_a, n_b = len(pnls_a), len(pnls_b)
    if n_a < 2 or n_b < 2:
        return {'p_value': None, 'method': 'welch_t', 'note': 'need ≥2 trades each'}

    mean_a = sum(pnls_a) / n_a
    mean_b = sum(pnls_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in pnls_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in pnls_b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return {'p_value': 1.0, 'method': 'welch_t', 'note': 'zero variance'}

    t_stat = (mean_a - mean_b) / se

    # Welch-Satterthwaite degrees of freedom
    num = (var_a / n_a + var_b / n_b) ** 2
    den = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = num / den if den > 0 else 1

    # Приближённый p-value через нормальную CDF (для df > 30 t ≈ normal)
    # Используем аппроксимацию Абрамовица-Стегуна для CDF нормального распределения
    def _norm_cdf(z):
        """CDF стандартного нормального распределения."""
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))

    def _t_cdf(t, df):
        """Приближённая CDF t-распределения через нормальную при df > 30,
        иначе через бета-функцию (упрощённо)."""
        if df > 30:
            return _norm_cdf(t)
        # Для малых df: используем аппроксимацию
        # t_cdf ≈ norm_cdf(t * (1 - 1/(4*df)) / sqrt(1 + t^2/(2*df)))
        adj = t * (1 - 1 / (4 * df)) / math.sqrt(1 + t * t / (2 * df))
        return _norm_cdf(adj)

    # Двусторонний тест
    p_value = 2 * (1 - _t_cdf(abs(t_stat), df))

    return {
        'p_value': round(max(0, min(1, p_value)), 4),
        'method': 'welch_t',
        't_statistic': round(t_stat, 4),
        'df': round(df, 1),
    }


def _compute_significance(pnls_a: list[float], pnls_b: list[float]) -> dict:
    """
    Вычислить статистическую значимость разницы между A и B.
    Использует bootstrap (основной) + Welch t-test (дополнительный).
    """
    n_a, n_b = len(pnls_a), len(pnls_b)

    if n_a < 30 or n_b < 30:
        return {
            'status': 'collecting',
            'note': f'Нужно ≥30 закрытых сделок в каждой группе. Сейчас: A={n_a}, B={n_b}.',
            'p_value_bootstrap': None,
            'p_value_welch': None,
            'verdict': 'недостаточно данных',
        }

    boot = _bootstrap_test(pnls_a, pnls_b)
    welch = _welch_ttest(pnls_a, pnls_b)

    # Используем bootstrap p-value как основной
    p_value = boot.get('p_value')
    if p_value is None:
        p_value = welch.get('p_value')

    if p_value is not None and p_value < 0.05:
        mean_a = sum(pnls_a) / n_a
        mean_b = sum(pnls_b) / n_b
        if mean_a > mean_b:
            verdict = 'A лучше'
        else:
            verdict = 'B лучше'
        sig_status = 'significant'
    else:
        verdict = 'недостаточно данных'
        sig_status = 'not_significant'

    return {
        'status': sig_status,
        'p_value_bootstrap': boot.get('p_value'),
        'p_value_welch': welch.get('p_value'),
        'verdict': verdict,
        'n_a': n_a,
        'n_b': n_b,
        'mean_diff': round(sum(pnls_a) / n_a - sum(pnls_b) / n_b, 4),
    }


# ── Основные API-функции ─────────────────────────────────────

def get_status() -> dict:
    """
    GET /rpc/ab_status — текущий статус A/B тестов.
    Возвращает сводку: enabled, варианты, сделки, метрики, значимость.
    """
    if not AB_ENABLED:
        return {
            'enabled': False,
            'feature_flag': 'BYBIT_AB_ENABLED',
            'note': 'A/B-тест выключен. Установите BYBIT_AB_ENABLED=1 для включения.',
        }

    conn = _get_conn()

    # Общая статистика
    total = conn.execute("SELECT COUNT(*) FROM ab_results").fetchone()[0]
    if total == 0:
        conn.close()
        return {
            'enabled': True,
            'status': 'no_data',
            'total_trades': 0,
            'variants': {
                'A': VARIANT_A_PARAMS,
                'B': VARIANT_B_PARAMS,
            },
            'metrics': {},
            'significance': None,
        }

    result = {
        'enabled': True,
        'status': 'ok',
        'total_trades': total,
        'variants': {
            'A': VARIANT_A_PARAMS,
            'B': VARIANT_B_PARAMS,
        },
        'metrics': {},
        'significance': None,
    }

    # Метрики по вариантам (только реальные сделки, is_paper=0)
    for variant in ('A', 'B'):
        real_rows = conn.execute("""
            SELECT pnl, win
            FROM ab_results
            WHERE variant = ? AND is_paper = 0 AND exit_ts IS NOT NULL
            ORDER BY exit_ts ASC
        """, (variant,)).fetchall()

        paper_rows = conn.execute("""
            SELECT pnl, win
            FROM ab_results
            WHERE variant = ? AND is_paper = 1 AND exit_ts IS NOT NULL
            ORDER BY exit_ts ASC
        """, (variant,)).fetchall()

        real_list = [{'pnl': r[0], 'win': bool(r[1])} for r in real_rows]
        paper_list = [{'pnl': r[0], 'win': bool(r[1])} for r in paper_rows]

        open_real = conn.execute("""
            SELECT COUNT(*) FROM ab_results
            WHERE variant = ? AND is_paper = 0 AND exit_ts IS NULL
        """, (variant,)).fetchone()[0]

        open_paper = conn.execute("""
            SELECT COUNT(*) FROM ab_results
            WHERE variant = ? AND is_paper = 1 AND exit_ts IS NULL
        """, (variant,)).fetchone()[0]

        result['metrics'][variant] = {
            'real': {
                **{f'real_{k}': v for k, v in _compute_metrics(real_list).items()},
                'open': open_real,
            },
            'paper': {
                **{f'paper_{k}': v for k, v in _compute_metrics(paper_list).items()},
                'open': open_paper,
            },
        }

    # Значимость (только реальные сделки)
    pnls_a = [r[0] for r in conn.execute(
        "SELECT pnl FROM ab_results WHERE variant='A' AND is_paper=0 AND exit_ts IS NOT NULL"
    ).fetchall()]
    pnls_b = [r[0] for r in conn.execute(
        "SELECT pnl FROM ab_results WHERE variant='B' AND is_paper=0 AND exit_ts IS NOT NULL"
    ).fetchall()]

    result['significance'] = _compute_significance(pnls_a, pnls_b)

    conn.close()
    return result


def get_report() -> dict:
    """
    Детальный отчёт A/B теста (совместимость со старым API).
    Возвращает словарь с метриками и статистической значимостью.
    """
    if not AB_ENABLED:
        return {'status': 'disabled', 'note': 'BYBIT_AB_ENABLED=0'}

    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM ab_results").fetchone()[0]
    if total == 0:
        conn.close()
        return {'status': 'no_data', 'total_trades': 0}

    stats = {}
    for variant in ('A', 'B'):
        rows = conn.execute("""
            SELECT pnl, win, exit_price, entry_price, qty, symbol, side, exit_ts
            FROM ab_results
            WHERE variant = ? AND is_paper = 0 AND exit_ts IS NOT NULL
            ORDER BY exit_ts ASC
        """, (variant,)).fetchall()

        trades = [{'pnl': r[0], 'win': bool(r[1])} for r in rows]
        stats[variant] = {
            'name': f'Вариант {variant}',
            'params': get_variant_params(variant),
            'total': conn.execute(
                "SELECT COUNT(*) FROM ab_results WHERE variant=? AND is_paper=0", (variant,)
            ).fetchone()[0],
            'open': conn.execute(
                "SELECT COUNT(*) FROM ab_results WHERE variant=? AND is_paper=0 AND exit_ts IS NULL", (variant,)
            ).fetchone()[0],
            **_compute_metrics(trades),
        }

    pnls_a = [r[0] for r in conn.execute(
        "SELECT pnl FROM ab_results WHERE variant='A' AND is_paper=0 AND exit_ts IS NOT NULL"
    ).fetchall()]
    pnls_b = [r[0] for r in conn.execute(
        "SELECT pnl FROM ab_results WHERE variant='B' AND is_paper=0 AND exit_ts IS NOT NULL"
    ).fetchall()]

    significance = _compute_significance(pnls_a, pnls_b)

    conn.close()

    return {
        'status': 'ok',
        'total_trades': total,
        'groups': stats,
        'significance': significance,
        'verdict': significance.get('verdict', ''),
    }


def reset_test():
    """Сбросить A/B тест (очистить таблицу)."""
    conn = _get_conn()
    conn.execute("DELETE FROM ab_results")
    conn.commit()
    conn.close()


def set_enabled(enabled: bool):
    """Включить/выключить A/B тест (устанавливает env var)."""
    global AB_ENABLED
    AB_ENABLED = enabled
    os.environ['BYBIT_AB_ENABLED'] = '1' if enabled else '0'


# ── Интеграция с Paper Trading API ───────────────────────────

def open_paper_positions_for_signal(
    signal_id: str,
    symbol: str,
    side: str,
    strategy: str,
    entry_price: float,
    qty: float,
    signal_score: float = 0,
) -> dict:
    """
    Открыть paper-позиции для ОБОИХ вариантов A и B.
    Возвращает {variant: row_id, ...}.
    Реальная позиция открывается только для назначенного варианта.
    """
    if not AB_ENABLED:
        return {}

    result = {}
    for variant in ('A', 'B'):
        row_id = record_paper_entry(
            signal_id=signal_id,
            variant=variant,
            symbol=symbol,
            side=side,
            strategy=strategy,
            entry_price=entry_price,
            qty=qty,
            signal_score=signal_score,
        )
        result[variant] = row_id

    return result


def close_paper_positions_for_signal(
    signal_id: str,
    exit_price: float,
    entry_price_a: float,
    entry_price_b: float,
    qty: float,
    side: str,
) -> dict:
    """
    Закрыть paper-позиции для обоих вариантов.
    Вычисляет PnL для каждого варианта раздельно.
    Возвращает {variant: {pnl, win}, ...}.
    """
    if not AB_ENABLED:
        return {}

    result = {}
    for variant, entry_price in [('A', entry_price_a), ('B', entry_price_b)]:
        if side == 'Buy':
            pnl = qty * (exit_price - entry_price)
        else:
            pnl = qty * (entry_price - exit_price)
        fees = exit_price * qty * 0.0006  # 0.06% taker fee
        net_pnl = pnl - fees
        win = net_pnl > 0

        record_paper_exit(
            signal_id=signal_id,
            variant=variant,
            exit_price=exit_price,
            pnl=net_pnl,
            win=win,
            fees=fees,
        )
        result[variant] = {'pnl': net_pnl, 'win': win, 'fees': fees}

    return result


# ── CLI ─────────────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'status':
            print(json.dumps(get_status(), indent=2, ensure_ascii=False))
        elif cmd == 'report':
            print(json.dumps(get_report(), indent=2, ensure_ascii=False))
        elif cmd == 'reset':
            reset_test()
            print("A/B test reset.")
        elif cmd == 'enable':
            set_enabled(True)
            print("A/B test ENABLED.")
        elif cmd == 'disable':
            set_enabled(False)
            print("A/B test DISABLED.")
        else:
            print("Usage: python ab_test.py [status|report|reset|enable|disable]")
    else:
        print(json.dumps(get_status(), indent=2, ensure_ascii=False))
