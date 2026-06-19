"""
Phase 3: Partial TP — динамический сплит тейк-профита.

Адаптирует 20/80 сплит (Middle/Upper BB) в зависимости от:
- Насколько цена близка к Middle BB
- Силы движения (momentum)
- Времени в позиции

Логика:
- Цена > 80% пути до Middle BB → 50% TP на Middle, 50% на Upper
- Цена > 50% пути до Middle BB → 30% TP на Middle, 70% на Upper  
- Цена < 50% пути до Middle BB → 20% TP на Middle, 80% на Upper (стандарт)
- Если позиция старше 48ч → 40% на Middle (ускоряем выход)
"""

import json
import math
import os
import sys
import time
from pathlib import Path
from statistics import mean, stdev

sys.path.insert(0, str(Path.home()))
sys.path.insert(0, str(Path(__file__).parent))
from .api import bybit, fetch_positions, fetch_orders, cancel_order, place_order, fetch_funding_total


DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
PARTIAL_TP_STATE = DATA_DIR / "partial_tp_state.json"


def _load_state() -> dict:
    if PARTIAL_TP_STATE.exists():
        with open(PARTIAL_TP_STATE) as f:
            return json.load(f)
    return {}


def _save_state(state: dict):
    tmp = str(PARTIAL_TP_STATE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, str(PARTIAL_TP_STATE))


def get_bb_data(symbol: str, interval: str = "D", limit: int = 30):
    """Получает BB данные: sma, upper, lower, current price, bb%."""
    try:
        resp = bybit(
            "GET",
            f"/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}",
        )
        if isinstance(resp, dict) and resp.get("retCode") == 0:
            klines = resp["result"]["list"]
            klines.reverse()  # старые → новые

            if len(klines) < 20:
                return None

            closes = [float(k[4]) for k in klines]

            sma_20 = mean(closes[-20:])
            std_20 = stdev(closes[-20:]) if len(closes[-20:]) > 1 else 0.0
            upper = sma_20 + 2 * std_20
            lower = sma_20 - 2 * std_20
            price = closes[-1]
            bb_pct = (
                (price - lower) / (upper - lower) * 100 if upper != lower else 50.0
            )

            return {
                "sma": round(sma_20, 8),
                "upper": round(upper, 8),
                "lower": round(lower, 8),
                "price": price,
                "bb_pct": round(bb_pct, 1),
            }
    except Exception:
        pass
    return None


def calculate_split(
    entry: float,
    price: float,
    sma: float,
    bb_pct: float,
    hours_open: float,
) -> tuple[float, float]:
    """
    Рассчитывает оптимальный TP-сплит.
    Возвращает (middle_frac, upper_frac) — доли для Middle и Upper BB.
    """
    # Базовый сплит
    middle_frac, upper_frac = 0.2, 0.8

    # 1. Расстояние до Middle BB в % от пути входа→SMA
    if sma > entry:
        progress = (price - entry) / (sma - entry) * 100 if sma != entry else 0
    else:
        progress = 0

    # 2. Коррекция по близости к Middle BB
    if progress > 80:
        middle_frac = 0.5
    elif progress > 50:
        middle_frac = 0.3

    # 3. Коррекция по времени в позиции (>48ч → ускоряем)
    if hours_open > 48:
        middle_frac = max(middle_frac, 0.4)

    # 4. Коррекция: если BB% растёт (импульс вверх) — оставляем больше на Upper
    if bb_pct > 40:
        # Цена уже ушла от нижней полосы — держим на Upper
        middle_frac = max(0.2, middle_frac - 0.1)

    upper_frac = round(1.0 - middle_frac, 2)
    middle_frac = round(middle_frac, 2)

    return middle_frac, upper_frac


def check_partial_tp() -> list[str]:
    """
    Проверяет все открытые позиции и корректирует TP-сплит.
    Возвращает список алертов.
    """
    alerts = []
    positions = fetch_positions()

    if not positions:
        return alerts

    state = _load_state()

    for sym, pos in positions.items():
        if pos.get("size", 0) <= 0:
            continue
        if pos.get("side") != "Buy":
            continue  # Partial TP только для LONG (пока)

        entry = float(pos.get("entry", 0))
        mark = float(pos.get("mark", 0))
        pos_size = float(pos.get("size", 0))

        if entry <= 0 or mark <= 0 or pos_size <= 0:
            continue

        # BB данные
        bb = get_bb_data(sym)
        if not bb:
            continue

        # Время в позиции (openTime из API — миллисекунды!)
        open_time = int(pos.get("openTime", 0))
        if open_time > 0:
            hours_open = (time.time() - open_time / 1000) / 3600
        else:
            hours_open = 0

        # Реализованная прибыль (уже снятые сливки)
        cum_rpnl = float(pos.get("cumRealisedPnl", 0))

        # Фандинг отдельно — чтобы не смешивать с TP-профитом
        funding_total = fetch_funding_total(sym, open_time)
        tp_only = cum_rpnl - funding_total  # чистый TP-профит без фандинга

        # Рассчитываем новый сплит
        new_mid, new_up = calculate_split(entry, mark, bb["sma"], bb["bb_pct"], hours_open)

        # Сравниваем с предыдущим
        prev_split = state.get(sym, {})
        prev_mid = prev_split.get("middle_frac", 0.2)

        if abs(new_mid - prev_mid) < 0.05:
            continue  # Изменение меньше 5% — не дёргаем

        # Проверяем существующие TP-ордера
        orders = fetch_orders()
        tp_orders = [
            o for o in orders.values()
            if o.get("symbol") == sym and o.get("kind") == "TP"
        ]

        if not tp_orders:
            continue

        # Обновляем самый дальний TP (Upper BB → меняем долю)
        # Отменяем существующие TP
        for o in tp_orders:
            try:
                cancel_order(sym, o["orderId"])
            except Exception:
                pass

        # Ставим новые TP с новым сплитом
        mid_qty = pos_size * new_mid
        up_qty = pos_size * new_up

        if mid_qty > 0:
            place_order(sym, "Sell", "Limit", mid_qty, bb["sma"], reduce_only=True,
                       position_idx=pos.get("positionIdx", 0))

        if up_qty > 0:
            place_order(sym, "Sell", "Limit", up_qty, bb["upper"], reduce_only=True,
                       position_idx=pos.get("positionIdx", 0))

        # Сохраняем состояние
        state[sym] = {
            "middle_frac": new_mid,
            "upper_frac": new_up,
            "updated_at": int(time.time()),
            "bb_pct": bb["bb_pct"],
            "progress": round((mark - entry) / (bb["sma"] - entry) * 100, 1) if bb["sma"] > entry else 0,
        }

        # Человеко-читаемый прогресс
        pct = state[sym]['progress']
        if pct >= 0:
            progress_str = f"до TP1: {pct:.0f}%"
        else:
            progress_str = f"отошла на {abs(pct):.0f}% от цели"

        # Понятный PnL
        total_pnl = cum_rpnl
        if abs(funding_total) > 0.01:
            pnl_str = f"${total_pnl:+.2f} (вкл. фандинг ${funding_total:+.2f})"
        else:
            pnl_str = f"${total_pnl:+.2f}"

        alerts.append(
            f"🔄 {sym} — перераспределение TP\n"
            f"Было: Middle {prev_mid*100:.0f}% / Upper {100-prev_mid*100:.0f}%\n"
            f"Стало: Middle {new_mid*100:.0f}% / Upper {new_up*100:.0f}%\n"
            f"Реализовано: {pnl_str}\n"
            f"В позиции: {hours_open:.0f}ч · {progress_str}"
        )

    _save_state(state)
    return alerts


# ─── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    alerts = check_partial_tp()
    if alerts:
        print("\n".join(alerts))
    else:
        print("✅ Partial TP: без изменений")
