"""
Дашборд — SVG-отчёт по винрейту на символ из trades.jsonl.

Генерирует SVG-файл с таблицей: Символ | Побед/Всего | Винрейт | Сумма PnL | Средний PnL.
Данные берутся из ~/.local/share/bybit-ws/trades.jsonl.
Дубликаты (одинаковые symbol+entry+exit+pnl) отбрасываются – учитывается только первая запись.

v2.7: + секция использования маржи (из positions.json).
"""

import json
import os
import sys

from datetime import datetime

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".local", "share", "bybit-ws")
TRADES_JSONL = os.path.join(DATA_DIR, "trades.jsonl")
FUNDING_JSONL = os.path.join(DATA_DIR, "funding.jsonl")
OUTPUT_SVG = os.path.join(DATA_DIR, "dashboard.svg")
POSITIONS_SNAPSHOT = os.path.join(DATA_DIR, "positions.json")
CORRELATION_SNAPSHOT = os.path.join(DATA_DIR, "correlation.json")

# Добавляем путь к bybit_ws для импорта (если запущен напрямую)
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def load_trades(path: str) -> list[dict]:
    """Загрузить сделки из JSONL, дедуплицируя по (symbol, entry, exit, pnl)."""
    trades = []
    seen = set()
    if not os.path.exists(path):
        return trades
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Ключ дедупликации: symbol + ts + pnl (формат v9)
            key = (t["symbol"], t.get("ts", 0), round(float(t["pnl"]), 4))
            if key in seen:
                continue
            seen.add(key)
            trades.append(t)
    return trades


def compute_winrate_by_symbol(trades: list[dict]) -> list[dict]:
    """Вычислить статистику по символам: wins, total, winrate, sum_pnl, avg_pnl."""
    by_symbol: dict[str, dict] = {}
    for t in trades:
        sym = t["symbol"]
        pnl = float(t["pnl"])
        if sym not in by_symbol:
            by_symbol[sym] = {"wins": 0, "total": 0, "pnl_sum": 0.0, "pnls": []}
        s = by_symbol[sym]
        s["total"] += 1
        s["pnl_sum"] += pnl
        s["pnls"].append(pnl)
        if pnl > 0:
            s["wins"] += 1

    rows = []
    for sym, stats in sorted(by_symbol.items()):
        total = stats["total"]
        wins = stats["wins"]
        pnl_sum = stats["pnl_sum"]
        avg_pnl = pnl_sum / total if total > 0 else 0.0
        winrate = (wins / total * 100) if total > 0 else 0.0
        rows.append({
            "symbol": sym,
            "wins": wins,
            "total": total,
            "winrate": winrate,
            "pnl_sum": pnl_sum,
            "avg_pnl": avg_pnl,
        })

    # Сортировка: по сумме PnL (худшие сверху)
    rows.sort(key=lambda r: r["pnl_sum"])
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Секция использования маржи
# ──────────────────────────────────────────────────────────────────────────────

def load_positions_snapshot(path: str) -> dict:
    """Загрузить снепшот позиций (positions.json)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def compute_margin_stats(positions: dict) -> dict:
    """Вычислить статистику маржи из снепшота позиций.

    Returns:
        {
            'total_margin': float,
            'max_margin': float,   # из конфига или $500 по умолчанию
            'utilization_pct': float,
            'position_count': int,
        }
    """
    max_margin = 500.0  # дефолт
    try:
        from .config import Config
        max_margin = float(Config().risk.get('max_total_margin', 500))
    except Exception:
        try:
            from bybit_ws.config import Config
            max_margin = float(Config().risk.get('max_total_margin', 500))
        except Exception as e:
            import logging; logging.getLogger('bybit.dashboard').warning(f'dashboard: {e}')

    total_margin = 0.0
    if positions:
        for sym, p in positions.items():
            margin = float(p.get('positionIM', 0))
            if margin == 0:
                size = float(p.get('size', 0))
                entry = float(p.get('entry', 0))
                leverage = float(p.get('leverage', 1))
                if leverage > 0 and size > 0 and entry > 0:
                    margin = size * entry / leverage
            total_margin += margin

    utilization_pct = (total_margin / max_margin * 100) if max_margin > 0 else 0.0

    return {
        'total_margin': total_margin,
        'max_margin': max_margin,
        'utilization_pct': utilization_pct,
        'position_count': len(positions) if positions else 0,
    }


def load_correlation_snapshot(path: str):
    """Load the last correlation computation result. Returns dict or None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def render_correlation_section(corr_data: dict, base_y: int,
                                svg_w: int, pad_x: int) -> tuple:
    """Render correlation risk warnings section.

    Args:
        corr_data: dict from correlation.json with 'flagged' and 'threshold' keys.
        base_y: starting Y coordinate.
        svg_w: SVG total width.
        pad_x: horizontal padding.

    Returns:
        (list of SVG strings, final Y coordinate)
    """
    if not corr_data:
        return [], base_y

    flagged = corr_data.get('flagged', [])
    if not flagged:
        return [], base_y

    ROW_H = 26
    HEADER_H = 32
    TITLE_H = 40
    GAP = 16

    COL_W = [110, 110, 80, 170]
    TOTAL_W = sum(COL_W) + (len(COL_W) - 1) * 2
    CARD_PAD = 16

    BG = "#1a1a2e"
    CARD_BG = "#16213e"
    HEADER_BG = "#16213e"
    ROW_EVEN = "#1a1a2e"
    ROW_ODD = "#1f1f3a"
    TEXT_HEADER = "#a0a0c0"
    TEXT_BODY = "#e0e0f0"
    RED = "#f44336"
    YELLOW = "#ff9800"
    WHITE = "#ffffff"

    threshold = corr_data.get('threshold', 0.80)
    pairs_count = len(flagged)

    COL_X = [pad_x]
    for i in range(len(COL_W) - 1):
        COL_X.append(COL_X[-1] + COL_W[i] + 2)

    parts = []

    # Section title
    PAD_Y = base_y + GAP
    parts.append(
        f'<text x="{svg_w // 2}" y="{PAD_Y + 20}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="15" font-weight="bold">'
        f'⚠️ Корреляционные риски (r &gt; ±{threshold:.2f})'
        f'</text>'
    )
    parts.append(
        f'<text x="{svg_w // 2}" y="{PAD_Y + 36}" text-anchor="middle" '
        f'fill="{TEXT_HEADER}" font-size="10">'
        f'{pairs_count} пар(ы) с высокой корреляцией цен за 24ч'
        f'</text>'
    )

    HEADER_Y = PAD_Y + TITLE_H + 4
    BODY_Y = HEADER_Y + HEADER_H

    # Header row
    parts.append(
        f'<rect x="{pad_x}" y="{HEADER_Y}" width="{TOTAL_W}" height="{HEADER_H}" '
        f'fill="{HEADER_BG}" rx="4"/>'
    )
    headers = ["Символ A", "Символ B", "Корр. r", "Риск"]
    aligns = ["left", "left", "right", "left"]
    for i, (hdr, align) in enumerate(zip(headers, aligns)):
        x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
        anchor = "end" if align == "right" else "start"
        parts.append(
            f'<text x="{x}" y="{HEADER_Y + HEADER_H // 2 + 6}" text-anchor="{anchor}" '
            f'fill="{TEXT_HEADER}" font-size="11" font-weight="bold">{hdr}</text>'
        )

    # Data rows
    for j, (s1, s2, corr) in enumerate(flagged):
        y_text = BODY_Y + j * ROW_H + ROW_H // 2 + 5
        row_y = BODY_Y + j * ROW_H
        bg = ROW_EVEN if j % 2 == 0 else ROW_ODD
        parts.append(
            f'<rect x="{pad_x}" y="{row_y}" width="{TOTAL_W}" height="{ROW_H}" '
            f'fill="{bg}" rx="2"/>'
        )

        abs_corr = abs(corr)
        if abs_corr > 0.95:
            corr_color = RED
            risk_label = "КРИТИЧЕСКИЙ"
            risk_color = RED
        elif abs_corr > 0.90:
            corr_color = YELLOW
            risk_label = "ВЫСОКИЙ"
            risk_color = YELLOW
        else:
            corr_color = YELLOW
            risk_label = "ПОВЫШЕННЫЙ"
            risk_color = YELLOW

        vals = [
            (s1, "left", TEXT_BODY),
            (s2, "left", TEXT_BODY),
            (f"{corr:+.3f}", "right", corr_color),
            (risk_label, "left", risk_color),
        ]
        for i, (val, align, color) in enumerate(vals):
            x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
            anchor = "end" if align == "right" else "start"
            parts.append(
                f'<text x="{x}" y="{y_text}" text-anchor="{anchor}" '
                f'fill="{color}" font-size="11">{val}</text>'
            )

    final_y = BODY_Y + len(flagged) * ROW_H
    return parts, final_y


def render_margin_section(margin_stats: dict, y_offset: int,
                           svg_w: int, pad_x: int) -> tuple[list[str], int]:
    """Отрендерить карточку использования маржи.

    Возвращает (список svg-строк, итоговая Y-координата).
    """
    CARD_H = 70
    CARD_PAD = 16
    GAP = 12

    BG = "#1a1a2e"
    CARD_BG = "#16213e"
    TEXT_HEADER = "#a0a0c0"
    TEXT_BODY = "#e0e0f0"
    GREEN = "#4caf50"
    YELLOW = "#ff9800"
    RED = "#f44336"
    WHITE = "#ffffff"
    ACCENT = "#7c8aff"

    util = margin_stats['utilization_pct']
    total = margin_stats['total_margin']
    max_m = margin_stats['max_margin']
    count = margin_stats['position_count']

    # Цвет индикатора
    if util > 95:
        util_color = RED
        status_text = "КРИТИЧЕСКИЙ"
    elif util > 80:
        util_color = YELLOW
        status_text = "ПРЕДУПРЕЖДЕНИЕ"
    else:
        util_color = GREEN
        status_text = "НОРМА"

    card_y = y_offset + GAP
    card_w = svg_w - pad_x * 2

    parts = []

    # Заголовок секции
    parts.append(
        f'<text x="{svg_w // 2}" y="{card_y - 2}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="15" font-weight="bold">'
        f'💰 Использование маржи'
        f'</text>'
    )

    # Карточка
    parts.append(
        f'<rect x="{pad_x}" y="{card_y + 20}" width="{card_w}" height="{CARD_H}" '
        f'fill="{CARD_BG}" rx="6" stroke="{util_color}" stroke-width="2"/>'
    )

    # Прогресс-бар
    bar_x = pad_x + CARD_PAD
    bar_y = card_y + 20 + CARD_PAD
    bar_w = card_w - CARD_PAD * 2
    bar_h = 16
    bar_radius = 8

    # Фон прогресс-бара
    parts.append(
        f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" '
        f'fill="#0d1117" rx="{bar_radius}"/>'
    )

    # Заполнение прогресс-бара
    fill_w = max(0, min(bar_w, bar_w * util / 100))
    if fill_w > 0:
        parts.append(
            f'<rect x="{bar_x}" y="{bar_y}" width="{fill_w}" height="{bar_h}" '
            f'fill="{util_color}" rx="{bar_radius}"/>'
        )

    # Процент внутри прогресс-бара
    pct_text_x = bar_x + bar_w // 2
    pct_text_y = bar_y + bar_h // 2 + 5
    parts.append(
        f'<text x="{pct_text_x}" y="{pct_text_y}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="11" font-weight="bold">{util:.1f}%</text>'
    )

    # Текст: «$X / $Y (N позиций) — СТАТУС»
    info_y = bar_y + bar_h + 18
    parts.append(
        f'<text x="{bar_x}" y="{info_y}" text-anchor="start" '
        f'fill="{TEXT_BODY}" font-size="12">'
        f'${total:.0f} / ${max_m:.0f}  •  {count} поз.  •  '
        f'<tspan fill="{util_color}" font-weight="bold">{status_text}</tspan>'
        f'</text>'
    )

    final_y = card_y + 20 + CARD_H
    return parts, final_y


# ──────────────────────────────────────────────────────────────────────────────
# Рендеринг winrate-таблицы
# ──────────────────────────────────────────────────────────────────────────────

def render_svg(rows: list[dict], trades_count: int, unique_trades: int) -> str:
    """Сгенерировать SVG-дашборд."""
    # Константы макета
    ROW_H = 28
    HEADER_H = 36
    FOOTER_H = 36
    TITLE_H = 50
    PAD_X = 16
    PAD_TOP = 16
    COL_W = [110, 90, 75, 100, 100]  # Символ, Побед/Всего, Винрейт, Сумма PnL, Средний PnL
    TOTAL_W = sum(COL_W) + (len(COL_W) - 1) * 2  # межколоночные отступы (~2px каждый)
    SVG_W = TOTAL_W + PAD_X * 2
    SVG_H = PAD_TOP + TITLE_H + HEADER_H + len(rows) * ROW_H + FOOTER_H + PAD_TOP

    COL_X = [PAD_X]
    for i in range(len(COL_W) - 1):
        COL_X.append(COL_X[-1] + COL_W[i] + 2)

    HEADER_Y = PAD_TOP + TITLE_H
    BODY_Y = HEADER_Y + HEADER_H

    # Цвета
    BG = "#1a1a2e"
    HEADER_BG = "#16213e"
    ROW_EVEN = "#1a1a2e"
    ROW_ODD = "#1f1f3a"
    TEXT_HEADER = "#a0a0c0"
    TEXT_BODY = "#e0e0f0"
    GREEN = "#4caf50"
    RED = "#f44336"
    WHITE = "#ffffff"
    ACCENT = "#7c8aff"

    def _x(col_idx, align="left"):
        """Центр колонки для right-align; левый край для left-align."""
        if align == "right":
            return COL_X[col_idx] + COL_W[col_idx]
        return COL_X[col_idx]

    def _y(row_idx):
        return BODY_Y + row_idx * ROW_H + ROW_H // 2 + 5  # +5 для baseline

    def _pnl_color(val):
        return GREEN if val > 0 else (RED if val < 0 else TEXT_BODY)

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_W}" height="{SVG_H}" font-family="monospace">',
        f'<rect width="100%" height="100%" fill="{BG}" rx="8"/>',
        # Заголовок
        f'<text x="{SVG_W // 2}" y="{PAD_TOP + 30}" text-anchor="middle" fill="{WHITE}" font-size="18" font-weight="bold">',
        f'📊 Винрейт по символам (Bybit WS)',
        f'</text>',
        f'<text x="{SVG_W // 2}" y="{PAD_TOP + 48}" text-anchor="middle" fill="{TEXT_HEADER}" font-size="11">',
        f'Сделок: {trades_count} (уникальных: {unique_trades}) • {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        f'</text>',
        # Шапка таблицы
        f'<rect x="{PAD_X}" y="{HEADER_Y}" width="{TOTAL_W}" height="{HEADER_H}" fill="{HEADER_BG}" rx="4"/>',
    ]

    headers = ["Символ", "Побед/Всего", "Винрейт", "Сумма PnL", "Средний PnL"]
    aligns = ["left", "left", "left", "right", "right"]
    for i, (hdr, align) in enumerate(zip(headers, aligns)):
        x = _x(i, align="right") if align == "right" else _x(i)
        anchor = "end" if align == "right" else "start"
        svg_parts.append(
            f'<text x="{x}" y="{HEADER_Y + HEADER_H // 2 + 6}" text-anchor="{anchor}" '
            f'fill="{TEXT_HEADER}" font-size="12" font-weight="bold">{hdr}</text>'
        )

    # Строки данных
    for j, r in enumerate(rows):
        y = _y(j)
        row_y = BODY_Y + j * ROW_H
        bg = ROW_EVEN if j % 2 == 0 else ROW_ODD
        svg_parts.append(f'<rect x="{PAD_X}" y="{row_y}" width="{TOTAL_W}" height="{ROW_H}" fill="{bg}" rx="2"/>')

        vals = [
            (r["symbol"], "left", TEXT_BODY),
            (f'{r["wins"]}/{r["total"]}', "left", WHITE),
            (f'{r["winrate"]:.0f}%', "left", _pnl_color(r["winrate"] - 50)),
            (f'${r["pnl_sum"]:+.2f}', "right", _pnl_color(r["pnl_sum"])),
            (f'${r["avg_pnl"]:+.2f}', "right", _pnl_color(r["avg_pnl"])),
        ]
        for i, (val, align, color) in enumerate(vals):
            x = _x(i, align="right") if align == "right" else _x(i)
            anchor = "end" if align == "right" else "start"
            svg_parts.append(
                f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{color}" font-size="12">{val}</text>'
            )

    # Футер: итоговая строка
    total_wins = sum(r["wins"] for r in rows)
    total_all = sum(r["total"] for r in rows)
    total_pnl = sum(r["pnl_sum"] for r in rows)
    total_wr = (total_wins / total_all * 100) if total_all > 0 else 0
    # Average PnL per trade (weighted by trade count per symbol? or simple average of per-symbol avg)
    # Use total_pnl / total_all for global avg
    global_avg = total_pnl / total_all if total_all > 0 else 0

    footer_y = BODY_Y + len(rows) * ROW_H
    svg_parts.append(
        f'<rect x="{PAD_X}" y="{footer_y}" width="{TOTAL_W}" height="{FOOTER_H}" fill="{HEADER_BG}" rx="4"/>'
    )
    footer_vals = [
        ("ИТОГО", "left", WHITE),
        (f'{total_wins}/{total_all}', "left", WHITE),
        (f'{total_wr:.0f}%', "left", _pnl_color(total_wr - 50)),
        (f'${total_pnl:+.2f}', "right", _pnl_color(total_pnl)),
        (f'${global_avg:+.2f}', "right", _pnl_color(global_avg)),
    ]
    fy = footer_y + FOOTER_H // 2 + 6
    for i, (val, align, color) in enumerate(footer_vals):
        x = _x(i, align="right") if align == "right" else _x(i)
        anchor = "end" if align == "right" else "start"
        svg_parts.append(
            f'<text x="{x}" y="{fy}" text-anchor="{anchor}" fill="{color}" font-size="13" font-weight="bold">{val}</text>'
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


# ──────────────────────────────────────────────────────────────────────────────
# Секция фондирования
# ──────────────────────────────────────────────────────────────────────────────

def load_funding_extremes(path: str, limit: int = 30) -> list[dict]:
    """Загрузить последние N записей экстремального фондирования из JSONL."""
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-limit:]


def aggregate_funding(records: list[dict]) -> list[dict]:
    """
    Агрегировать записи по символам: последняя ставка, количество срабатываний,
    направление.
    """
    by_symbol: dict[str, dict] = {}
    for rec in records:
        sym = rec["symbol"]
        rate = rec["rate"]
        side = rec.get("side", "LONG_PAYS" if rate > 0 else "SHORT_PAYS")
        ts = rec.get("timestamp", "")
        if sym not in by_symbol:
            by_symbol[sym] = {
                "symbol": sym,
                "last_rate": rate,
                "last_ts": ts,
                "count": 0,
                "side": side,
            }
        else:
            by_symbol[sym]["last_rate"] = rate
            by_symbol[sym]["last_ts"] = ts
            by_symbol[sym]["side"] = side
        by_symbol[sym]["count"] += 1

    rows = sorted(by_symbol.values(), key=lambda r: abs(r["last_rate"]), reverse=True)
    return rows


def render_funding_section(funding_rows: list[dict], base_y: int,
                           svg_w: int, pad_x: int) -> tuple[list[str], int]:
    """
    Отрендерить секцию экстремального фондирования в SVG.
    Возвращает (список svg-строк, итоговая Y-координата).
    """
    if not funding_rows:
        return [], base_y

    ROW_H = 24
    HEADER_H = 32
    TITLE_H = 40
    GAP = 16  # отступ между секциями

    COL_W = [100, 85, 55, 140]  # Символ, Ставка, Срабатываний, Последнее
    TOTAL_W = sum(COL_W) + (len(COL_W) - 1) * 2
    PAD_Y = base_y + GAP

    BG = "#1a1a2e"
    HEADER_BG = "#16213e"
    ROW_EVEN = "#1a1a2e"
    ROW_ODD = "#1f1f3a"
    TEXT_HEADER = "#a0a0c0"
    TEXT_BODY = "#e0e0f0"
    GREEN = "#4caf50"
    RED = "#f44336"
    WHITE = "#ffffff"

    COL_X = [pad_x]
    for i in range(len(COL_W) - 1):
        COL_X.append(COL_X[-1] + COL_W[i] + 2)

    def _pnl_color(val):
        return RED if val > 0 else (GREEN if val < 0 else TEXT_BODY)

    parts = []
    # Заголовок секции
    parts.append(
        f'<text x="{svg_w // 2}" y="{PAD_Y + 20}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="15" font-weight="bold">'
        f'💸 Экстремальный фондинг (пороги: &gt;0.1% LONG, &lt;-0.05% SHORT)'
        f'</text>'
    )

    HEADER_Y = PAD_Y + TITLE_H
    BODY_Y = HEADER_Y + HEADER_H

    # Шапка
    parts.append(
        f'<rect x="{pad_x}" y="{HEADER_Y}" width="{TOTAL_W}" height="{HEADER_H}" '
        f'fill="{HEADER_BG}" rx="4"/>'
    )
    headers = ["Символ", "Ставка", "Сраб.", "Последнее"]
    aligns = ["left", "right", "left", "left"]
    for i, (hdr, align) in enumerate(zip(headers, aligns)):
        x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
        anchor = "end" if align == "right" else "start"
        parts.append(
            f'<text x="{x}" y="{HEADER_Y + HEADER_H // 2 + 6}" text-anchor="{anchor}" '
            f'fill="{TEXT_HEADER}" font-size="11" font-weight="bold">{hdr}</text>'
        )

    # Строки данных
    for j, r in enumerate(funding_rows):
        y_text = BODY_Y + j * ROW_H + ROW_H // 2 + 5
        row_y = BODY_Y + j * ROW_H
        bg = ROW_EVEN if j % 2 == 0 else ROW_ODD
        parts.append(
            f'<rect x="{pad_x}" y="{row_y}" width="{TOTAL_W}" height="{ROW_H}" '
            f'fill="{bg}" rx="2"/>'
        )

        rate = r["last_rate"]
        last_ts = r.get("last_ts", "")[:16]  # "2026-06-08T15:30"
        vals = [
            (r["symbol"], "left", WHITE),
            (f'{rate:+.3f}%', "right", _pnl_color(rate)),
            (str(r["count"]), "left", TEXT_BODY),
            (last_ts, "left", TEXT_HEADER),
        ]
        for i, (val, align, color) in enumerate(vals):
            x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
            anchor = "end" if align == "right" else "start"
            parts.append(
                f'<text x="{x}" y="{y_text}" text-anchor="{anchor}" '
                f'fill="{color}" font-size="11">{val}</text>'
            )

    final_y = BODY_Y + len(funding_rows) * ROW_H
    return parts, final_y


# ──────────────────────────────────────────────────────────────────────────────
# Секция рыночного режима (Market Regime)
# ──────────────────────────────────────────────────────────────────────────────

REGIME_FILE = os.path.join(DATA_DIR, "regime.json")

REGIME_COLORS = {
    "TRENDING_UP": "#4caf50",
    "TRENDING_DOWN": "#f44336",
    "CHOPPY": "#ff9800",
    "HIGH_VOL": "#e91e63",
    "LOW_VOL": "#2196f3",
    "NEUTRAL": "#9e9e9e",
    "UNKNOWN": "#607d8b",
}

REGIME_LABELS = {
    "TRENDING_UP": "TRENDING UP \u2191",
    "TRENDING_DOWN": "TRENDING DOWN \u2193",
    "CHOPPY": "CHOPPY \u2248",
    "HIGH_VOL": "HIGH VOL \u26a1",
    "LOW_VOL": "LOW VOL \ud83d\udca4",
    "NEUTRAL": "NEUTRAL \u2194",
    "UNKNOWN": "???",
}


def load_regime(path: str) -> dict:
    """Load the latest regime snapshot from JSON."""
    if not os.path.exists(path):
        return {"regime": "UNKNOWN", "confidence": 0, "details": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"regime": "UNKNOWN", "confidence": 0, "details": {}}


def render_regime_badge(regime_data: dict, y_offset: int,
                         svg_w: int, pad_x: int) -> tuple[list[str], int]:
    """Render a compact colored regime badge.

    Returns (svg_lines, final_y).
    """
    BADGE_H = 44
    GAP = 12

    regime = regime_data.get("regime", "UNKNOWN")
    confidence = regime_data.get("confidence", 0)
    details = regime_data.get("details", {})

    color = REGIME_COLORS.get(regime, "#9e9e9e")
    label = REGIME_LABELS.get(regime, regime)

    btc_chg = details.get("btc_change_pct", 0)
    eth_chg = details.get("eth_change_pct", 0)

    card_y = y_offset + GAP + 2  # +2 for text baseline
    card_w = svg_w - pad_x * 2

    WHITE = "#ffffff"
    TEXT_BODY = "#e0e0f0"
    TEXT_HEADER = "#a0a0c0"
    CARD_BG = "#16213e"

    parts = []

    # Section title
    parts.append(
        f'<text x="{svg_w // 2}" y="{card_y}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="15" font-weight="bold">'
        f'\U0001f4ca Market Regime'
        f'</text>'
    )

    badge_y = card_y + 8

    # Badge background
    parts.append(
        f'<rect x="{pad_x}" y="{badge_y}" width="{card_w}" height="{BADGE_H}" '
        f'fill="{CARD_BG}" rx="8" stroke="{color}" stroke-width="2.5"/>'
    )

    # Regime label pill
    pill_w = 170
    pill_h = 26
    pill_x = pad_x + 14
    pill_y = badge_y + 9
    pill_radius = 13

    parts.append(
        f'<rect x="{pill_x}" y="{pill_y}" width="{pill_w}" height="{pill_h}" '
        f'fill="{color}" rx="{pill_radius}"/>'
    )
    parts.append(
        f'<text x="{pill_x + pill_w // 2}" y="{pill_y + pill_h // 2 + 5}" '
        f'text-anchor="middle" fill="{WHITE}" font-size="12" font-weight="bold">'
        f'{label}'
        f'</text>'
    )

    # Confidence
    conf_x = pill_x + pill_w + 16
    conf_y = pill_y + pill_h // 2 + 5
    parts.append(
        f'<text x="{conf_x}" y="{conf_y}" text-anchor="start" '
        f'fill="{TEXT_HEADER}" font-size="11">'
        f'conf: <tspan fill="{WHITE}" font-weight="bold">{confidence}%</tspan>'
        f'</text>'
    )

    # BTC / ETH summary
    summary_x = pill_x + pill_w + 16
    summary_y = conf_y + 14
    btc_sign = "+" if btc_chg >= 0 else ""
    eth_sign = "+" if eth_chg >= 0 else ""

    def _chg_color(val):
        return "#4caf50" if val > 0 else ("#f44336" if val < 0 else TEXT_BODY)

    parts.append(
        f'<text x="{summary_x}" y="{summary_y}" text-anchor="start" '
        f'fill="{TEXT_HEADER}" font-size="10">'
        f'BTC <tspan fill="{_chg_color(btc_chg)}">{btc_sign}{btc_chg:.2f}%</tspan>  '
        f'ETH <tspan fill="{_chg_color(eth_chg)}">{eth_sign}{eth_chg:.2f}%</tspan>'
        f'</text>'
    )

    final_y = badge_y + BADGE_H
    return parts, final_y


def generate_dashboard():
    """Главная точка входа: загрузить данные, вычислить, отрендерить SVG."""
    all_trades = load_trades(TRADES_JSONL)
    # Общее число строк в файле (включая дубликаты) — для информации
    raw_count = 0
    if os.path.exists(TRADES_JSONL):
        with open(TRADES_JSONL) as f:
            raw_count = sum(1 for line in f if line.strip())

    # ── Winrate-таблица ──
    rows = compute_winrate_by_symbol(all_trades)

    # ── Константы макета (используются для всех секций) ──
    ROW_H = 28
    HEADER_H = 36
    TITLE_H = 50
    PAD_TOP = 16
    PAD_X = 16
    COL_W = [110, 90, 75, 100, 100]
    TOTAL_W = sum(COL_W) + (len(COL_W) - 1) * 2
    SVG_W = TOTAL_W + PAD_X * 2

    # Базовая высота winrate-таблицы
    winrate_h = PAD_TOP + TITLE_H + HEADER_H + len(rows) * ROW_H + 36 + PAD_TOP
    # + FOOTER_H (=36) для итоговой строки

    # ── Секция рыночного режима (самая верхняя) ──
    regime_data = load_regime(REGIME_FILE)
    regime_parts, regime_end_y = render_regime_badge(regime_data, PAD_TOP, SVG_W, PAD_X)

    # ── Секция маржи ──
    margin_offset = regime_end_y + 16 if regime_parts else PAD_TOP
    positions = load_positions_snapshot(POSITIONS_SNAPSHOT)
    margin_stats = compute_margin_stats(positions)
    margin_parts, margin_end_y = render_margin_section(margin_stats, margin_offset, SVG_W, PAD_X)

    # ── Winrate-таблица (сдвинутая вниз) ──
    winrate_offset = margin_end_y + 16 if margin_parts else PAD_TOP
    winrate_svg = render_svg_shifted(rows, raw_count, len(all_trades),
                                      winrate_offset, SVG_W, PAD_X)

    # ── Секция фондирования ──
    funding_records = load_funding_extremes(FUNDING_JSONL, limit=100)
    funding_rows_list = aggregate_funding(funding_records)

    # Вычисляем Y где заканчивается winrate-таблица
    FOOTER_H = 36
    winrate_end_y = winrate_offset + TITLE_H + HEADER_H + len(rows) * ROW_H + FOOTER_H

    # ── Секция корреляционных рисков ──
    corr_data = load_correlation_snapshot(CORRELATION_SNAPSHOT)
    corr_parts, corr_end_y = render_correlation_section(
        corr_data, winrate_end_y, SVG_W, PAD_X
    )

    # ── Секция фондирования ──
    funding_records = load_funding_extremes(FUNDING_JSONL, limit=100)
    funding_rows_list = aggregate_funding(funding_records)

    funding_base_y = corr_end_y if corr_parts else winrate_end_y
    if funding_rows_list:
        funding_parts, funding_end_y = render_funding_section(
            funding_rows_list, funding_base_y, SVG_W, PAD_X
        )
    else:
        funding_parts = []
        funding_end_y = funding_base_y

    final_svg_h = max(funding_end_y, corr_end_y, winrate_end_y) + PAD_TOP

    # ── Сборка итогового SVG ──
    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_W}" height="{final_svg_h}" font-family="monospace">',
        f'<rect width="100%" height="100%" fill="#1a1a2e" rx="8"/>',
    ]

    # Вставляем рыночный режим (самый верх)
    if regime_parts:
        svg_parts.extend(regime_parts)

    # Вставляем маржу
    if margin_parts:
        svg_parts.extend(margin_parts)

    # Вставляем winrate (без <svg> и </svg>)
    svg_parts.append(winrate_svg)

    # Вставляем корреляцию
    if corr_parts:
        svg_parts.extend(corr_parts)

    # Вставляем фондинг
    if funding_parts:
        svg_parts.extend(funding_parts)

    svg_parts.append("</svg>")
    svg = "\n".join(svg_parts)

    os.makedirs(os.path.dirname(OUTPUT_SVG), exist_ok=True)
    with open(OUTPUT_SVG, "w") as f:
        f.write(svg.encode("utf-8", errors="replace").decode("utf-8"))

    print(f"✅ Дашборд сохранён: {OUTPUT_SVG}")
    regime_label = regime_data.get("regime", "UNKNOWN") if regime_data else "UNKNOWN"
    regime_conf = regime_data.get("confidence", 0) if regime_data else 0
    print(f"   Режим рынка: {regime_label} (conf: {regime_conf}%)")
    print(f"   Строк в JSONL: {raw_count}")
    print(f"   Уникальных сделок: {len(all_trades)}")
    print(f"   Символов: {len(rows)}")
    print(f"   Использование маржи: ${margin_stats['total_margin']:.0f} / ${margin_stats['max_margin']:.0f} "
          f"({margin_stats['utilization_pct']:.1f}%) • {margin_stats['position_count']} поз.")
    for r in rows:
        print(f"   {r['symbol']:14s}  {r['wins']}/{r['total']:<6d}  WR={r['winrate']:5.0f}%  "
              f"Σ={r['pnl_sum']:+8.2f}  avg={r['avg_pnl']:+7.2f}")

    if funding_rows_list:
        print(f"\n💸 Экстремальный фондинг ({len(funding_rows_list)} символов):")
        for fr in funding_rows_list:
            print(f"   {fr['symbol']:14s}  {fr['last_rate']:+.3f}%  ×{fr['count']}  "
                  f"{fr.get('last_ts', '')[:16]}")

    return svg


def render_svg_shifted(rows: list[dict], trades_count: int, unique_trades: int,
                        y_offset: int, svg_w: int, pad_x: int) -> str:
    """Версия render_svg с произвольным Y-смещением (для вставки в общий SVG)."""
    ROW_H = 28
    HEADER_H = 36
    TITLE_H = 50
    COL_W = [110, 90, 75, 100, 100]
    TOTAL_W = sum(COL_W) + (len(COL_W) - 1) * 2

    COL_X = [pad_x]
    for i in range(len(COL_W) - 1):
        COL_X.append(COL_X[-1] + COL_W[i] + 2)

    HEADER_Y = y_offset + TITLE_H
    BODY_Y = HEADER_Y + HEADER_H

    BG = "#1a1a2e"
    HEADER_BG = "#16213e"
    ROW_EVEN = "#1a1a2e"
    ROW_ODD = "#1f1f3a"
    TEXT_HEADER = "#a0a0c0"
    TEXT_BODY = "#e0e0f0"
    GREEN = "#4caf50"
    RED = "#f44336"
    WHITE = "#ffffff"

    def _pnl_color(val):
        return GREEN if val > 0 else (RED if val < 0 else TEXT_BODY)

    parts = []

    # Заголовок
    parts.append(
        f'<text x="{svg_w // 2}" y="{y_offset + 30}" text-anchor="middle" '
        f'fill="{WHITE}" font-size="18" font-weight="bold">'
        f'📊 Винрейт по символам (Bybit WS)'
        f'</text>'
    )
    parts.append(
        f'<text x="{svg_w // 2}" y="{y_offset + 48}" text-anchor="middle" '
        f'fill="{TEXT_HEADER}" font-size="11">'
        f'Сделок: {trades_count} (уникальных: {unique_trades}) • {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        f'</text>'
    )

    # Шапка таблицы
    parts.append(
        f'<rect x="{pad_x}" y="{HEADER_Y}" width="{TOTAL_W}" height="{HEADER_H}" '
        f'fill="{HEADER_BG}" rx="4"/>'
    )

    headers = ["Символ", "Побед/Всего", "Винрейт", "Сумма PnL", "Средний PnL"]
    aligns = ["left", "left", "left", "right", "right"]
    for i, (hdr, align) in enumerate(zip(headers, aligns)):
        x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
        anchor = "end" if align == "right" else "start"
        parts.append(
            f'<text x="{x}" y="{HEADER_Y + HEADER_H // 2 + 6}" text-anchor="{anchor}" '
            f'fill="{TEXT_HEADER}" font-size="12" font-weight="bold">{hdr}</text>'
        )

    # Строки данных
    for j, r in enumerate(rows):
        y_text = BODY_Y + j * ROW_H + ROW_H // 2 + 5
        row_y = BODY_Y + j * ROW_H
        bg = ROW_EVEN if j % 2 == 0 else ROW_ODD
        parts.append(
            f'<rect x="{pad_x}" y="{row_y}" width="{TOTAL_W}" height="{ROW_H}" fill="{bg}" rx="2"/>'
        )

        vals = [
            (r["symbol"], "left", TEXT_BODY),
            (f'{r["wins"]}/{r["total"]}', "left", WHITE),
            (f'{r["winrate"]:.0f}%', "left", _pnl_color(r["winrate"] - 50)),
            (f'${r["pnl_sum"]:+.2f}', "right", _pnl_color(r["pnl_sum"])),
            (f'${r["avg_pnl"]:+.2f}', "right", _pnl_color(r["avg_pnl"])),
        ]
        for i, (val, align, color) in enumerate(vals):
            x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
            anchor = "end" if align == "right" else "start"
            parts.append(
                f'<text x="{x}" y="{y_text}" text-anchor="{anchor}" fill="{color}" font-size="12">{val}</text>'
            )

    # Футер
    total_wins = sum(r["wins"] for r in rows)
    total_all = sum(r["total"] for r in rows)
    total_pnl = sum(r["pnl_sum"] for r in rows)
    total_wr = (total_wins / total_all * 100) if total_all > 0 else 0
    global_avg = total_pnl / total_all if total_all > 0 else 0

    footer_y = BODY_Y + len(rows) * ROW_H
    parts.append(
        f'<rect x="{pad_x}" y="{footer_y}" width="{TOTAL_W}" height="36" fill="{HEADER_BG}" rx="4"/>'
    )
    footer_vals = [
        ("ИТОГО", "left", WHITE),
        (f'{total_wins}/{total_all}', "left", WHITE),
        (f'{total_wr:.0f}%', "left", _pnl_color(total_wr - 50)),
        (f'${total_pnl:+.2f}', "right", _pnl_color(total_pnl)),
        (f'${global_avg:+.2f}', "right", _pnl_color(global_avg)),
    ]
    fy = footer_y + 18 + 6
    for i, (val, align, color) in enumerate(footer_vals):
        x = COL_X[i] + COL_W[i] if align == "right" else COL_X[i]
        anchor = "end" if align == "right" else "start"
        parts.append(
            f'<text x="{x}" y="{fy}" text-anchor="{anchor}" fill="{color}" font-size="13" font-weight="bold">{val}</text>'
        )

    return "\n".join(parts)


# ── CLI ──
if __name__ == "__main__":
    generate_dashboard()
