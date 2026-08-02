"""
Трекер экстремальных ставок фондирования Bybit.

Логика:
  1. Получает текущие позиции (fetch_positions)
  2. Для символов в позициях запрашивает тикеры через /v5/market/tickers?category=linear
  3. Вычисляет экстремальные ставки: >0.1% (лонги платят шортам) или <-0.05% (шорты платят лонгам)
  4. Логирует обнаруженные аномалии в ~/.local/share/bybit-ws/funding.jsonl
  5. Возвращает список сообщений для интеграции в дашборд и алерты

Формат JSONL: {"timestamp": "2026-06-08T15:30:00", "symbol": "BTCUSDT", "rate": 0.15, "side": "LONG_PAYS"}
"""

import json
import os
import time
from datetime import datetime, timezone

from .api import bybit, fetch_positions
from . import DATA_DIR

FUNDING_JSONL = os.path.join(DATA_DIR, "funding.jsonl")
FUNDING_STATE_FILE = os.path.join(DATA_DIR, "funding_tracker_state.json")

# Пороги экстремального фондинга
HIGH_THRESHOLD = 0.1   # >0.1% — лонги платят слишком много
LOW_THRESHOLD = -0.05  # <-0.05% — шорты платят (медвежий перекос)

# Минимальный интервал между проверками (сек)
CHECK_INTERVAL_SEC = 3600  # 1 час
_last_check = 0.0


def _log(msg: str) -> None:
    """Запись в events.log."""
    from . import EVENTS_LOG
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(EVENTS_LOG, "a") as f:
        f.write(f"[{ts}] [funding_tracker] {msg}\n")


def _load_state() -> dict:
    """Загрузить состояние трекера (время последнего алерта по символу)."""
    try:
        with open(FUNDING_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Сохранить состояние трекера."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FUNDING_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_cycle() -> list[str]:
    """
    Вызывается из главного цикла монитора каждый тик.
    Реально выполняет проверку не чаще раза в час.
    Возвращает список алерт-сообщений для логирования.
    """
    global _last_check

    now = time.time()
    if now - _last_check < CHECK_INTERVAL_SEC:
        return []  # ещё не пора

    _last_check = now

    # 1. Получить текущие позиции
    positions = fetch_positions()
    if not positions:
        return []

    symbols = list(positions.keys())
    if not symbols:
        return []

    # 2. Запросить тикеры для символов в позициях
    # Bybit /v5/market/tickers возвращает fundingRate для всех linear-тикеров
    data = bybit("GET", "/v5/market/tickers?category=linear")
    if not data or data.get("retCode") != 0:
        _log(f"Ошибка получения тикеров: {data.get('retMsg', 'no response') if data else 'no response'}")
        return []

    tickers = data.get("result", {}).get("list", [])
    if not tickers:
        _log("Пустой список тикеров")
        return []

    # Словарь symbol → fundingRate (строка, например "0.0001" = 0.01%)
    ticker_map = {}
    for t in tickers:
        sym = t.get("symbol", "")
        fr_str = t.get("fundingRate", "")
        if sym and fr_str:
            try:
                ticker_map[sym] = float(fr_str) * 100  # переводим в проценты
            except (ValueError, TypeError):
                pass

    # 3. Найти экстремальные ставки среди символов в позициях
    state = _load_state()
    alerts = []
    extreme_records = []

    for sym in symbols:
        rate = ticker_map.get(sym)
        if rate is None:
            continue

        is_extreme = False
        direction = ""

        if rate > HIGH_THRESHOLD:
            is_extreme = True
            direction = "LONG_PAYS"
        elif rate < LOW_THRESHOLD:
            is_extreme = True
            direction = "SHORT_PAYS"

        if not is_extreme:
            continue

        # Дедупликация: не алертить чаще раза в 4 часа по одному символу
        sym_state = state.get(sym, {})
        last_alert = sym_state.get("last_alert", 0)
        if now - last_alert < 14400:  # 4 часа
            continue

        state[sym] = {"last_alert": now, "rate": rate, "direction": direction}

        record = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "symbol": sym,
            "rate": round(rate, 4),
            "side": direction,
        }
        extreme_records.append(record)

        emoji = "🔴" if rate > 0 else "🟢"
        pct_str = f"{rate:+.4f}%"
        if direction == "LONG_PAYS":
            msg = f"{emoji} Фондинг {sym}: {pct_str} — лонги платят шортам (перегрев LONG)"
        else:
            msg = f"{emoji} Фондинг {sym}: {pct_str} — шорты платят лонгам (медвежий перекос)"

        alerts.append(msg)

    # 4. Записать в JSONL
    if extreme_records:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(FUNDING_JSONL, "a") as f:
            for rec in extreme_records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _log(f"Записано {len(extreme_records)} экстремальных ставок в funding.jsonl")

    _save_state(state)

    if not alerts:
        _log(f"Проверено {len(symbols)} символов — экстремальных ставок нет")

    return alerts


def get_latest_extremes(limit: int = 20) -> list[dict]:
    """
    Прочитать последние N экстремальных записей из funding.jsonl.
    Используется дашбордом для отображения.
    """
    if not os.path.exists(FUNDING_JSONL):
        return []

    records = []
    with open(FUNDING_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Возвращаем последние N (самые свежие в конце файла)
    return records[-limit:]


def get_current_funding_for_symbols(symbols: list[str]) -> dict[str, float]:
    """
    Получить текущие ставки фондирования для списка символов.
    Возвращает {symbol: rate_in_percent}.

    Используется дашбордом.
    """
    if not symbols:
        return {}

    data = bybit("GET", "/v5/market/tickers?category=linear")
    if not data or data.get("retCode") != 0:
        return {}

    result = {}
    for t in data.get("result", {}).get("list", []):
        sym = t.get("symbol", "")
        if sym in symbols:
            fr_str = t.get("fundingRate", "")
            if fr_str:
                try:
                    result[sym] = float(fr_str) * 100
                except (ValueError, TypeError):
                    pass

    return result


# ── CLI ──
if __name__ == "__main__":
    alerts = check_cycle()
    if alerts:
        print(f"\n⚠️ Обнаружено {len(alerts)} экстремальных ставок:")
        for a in alerts:
            print(f"  {a}")
    else:
        print("✅ Экстремальных ставок фондирования нет")

    # Показать последние записи
    latest = get_latest_extremes(10)
    if latest:
        print(f"\n📋 Последние {len(latest)} записей в funding.jsonl:")
        for rec in latest:
            print(f"  {rec['timestamp']}  {rec['symbol']:12s}  {rec['rate']:+.4f}%  {rec['side']}")
