#!/usr/bin/env python3
"""Проверка соответствия бэктеста и реальной торговли (parity check).

Скрипт запускает backtest_runner на тех же параметрах, что использовались
в живой торговле, и сравнивает входы:
- символ
- время входа (допуск ±15 минут)
- сторона (Buy/Sell)

Выводит отчёт о расхождениях:
- совпавшие сделки (symbol, time, side совпадают)
- только в бэктесте (вход в backtest, но не в live)
- только в live (вход в live, но не в backtest)

Не влияет на работу сервиса, только диагностика.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
STATE_DB = DATA_DIR / "state.db"
OUTPUT_DIR = DATA_DIR / "parity_reports"


def load_live_entries(days: int) -> list[dict]:
    """Загрузить реальные входы из trade_history за последние N дней."""
    if not STATE_DB.exists():
        print(f"⚠️  State DB не найдена: {STATE_DB}")
        return []
    
    cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        cursor = conn.execute("""
            SELECT symbol, side, entry_price, opened_at, strategy
            FROM trade_history
            WHERE opened_at >= ?
            ORDER BY opened_at DESC
        """, (cutoff_ts,))
        
        entries = []
        for row in cursor:
            symbol, side, entry_price, opened_at, strategy = row
            entries.append({
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "opened_at": opened_at,
                "strategy": strategy or "unknown",
            })
        conn.close()
        return entries
    except Exception as e:
        print(f"⚠️  Ошибка чтения state.db: {e}")
        return []


def run_backtest(days: int, interval: int) -> list[dict]:
    """Запустить backtest_runner и получить входы."""
    cmd = [
        sys.executable, "-m", "bybit_ws.experiments.backtest_runner",
        "--days", str(days),
        "--interval", str(interval),
        "--output-json",
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            print(f"⚠️  Backtest runner завершился с ошибкой:\n{result.stderr}")
            return []
        
        # Парсим JSON из stdout
        output = result.stdout.strip()
        if not output:
            print("⚠️  Backtest runner не вернул данных")
            return []
        
        data = json.loads(output)
        trades = data.get("trades", [])
        
        entries = []
        for trade in trades:
            entries.append({
                "symbol": trade.get("symbol"),
                "side": trade.get("side"),
                "entry_price": trade.get("entry_price"),
                "opened_at": trade.get("entry_time"),
            })
        return entries
    except subprocess.TimeoutExpired:
        print("⚠️  Backtest runner превысил таймаут (180s)")
        return []
    except json.JSONDecodeError as e:
        print(f"⚠️  Ошибка парсинга JSON от backtest runner: {e}")
        return []
    except Exception as e:
        print(f"⚠️  Ошибка запуска backtest runner: {e}")
        return []


def compare_entries(live: list[dict], backtest: list[dict], tolerance_minutes: int = 15) -> dict:
    """Сравнить входы live и backtest.
    
    Критерий совпадения:
    - символ совпадает
    - сторона совпадает
    - время входа отличается не более чем на tolerance_minutes
    """
    tolerance_seconds = tolerance_minutes * 60
    
    matched = []
    only_live = []
    only_backtest = []
    
    live_used = set()
    backtest_used = set()
    
    # Ищем совпадения
    for i, live_entry in enumerate(live):
        for j, bt_entry in enumerate(backtest):
            if j in backtest_used:
                continue
            
            if live_entry["symbol"] != bt_entry["symbol"]:
                continue
            if live_entry["side"] != bt_entry["side"]:
                continue
            
            time_diff = abs(live_entry["opened_at"] - bt_entry["opened_at"])
            if time_diff <= tolerance_seconds:
                matched.append({
                    "symbol": live_entry["symbol"],
                    "side": live_entry["side"],
                    "live_time": datetime.fromtimestamp(live_entry["opened_at"]).isoformat(),
                    "backtest_time": datetime.fromtimestamp(bt_entry["opened_at"]).isoformat(),
                    "time_diff_seconds": time_diff,
                    "live_strategy": live_entry.get("strategy"),
                })
                live_used.add(i)
                backtest_used.add(j)
                break
    
    # Только в live
    for i, entry in enumerate(live):
        if i not in live_used:
            only_live.append({
                "symbol": entry["symbol"],
                "side": entry["side"],
                "opened_at": datetime.fromtimestamp(entry["opened_at"]).isoformat(),
                "strategy": entry.get("strategy"),
            })
    
    # Только в backtest
    for j, entry in enumerate(backtest):
        if j not in backtest_used:
            only_backtest.append({
                "symbol": entry["symbol"],
                "side": entry["side"],
                "opened_at": datetime.fromtimestamp(entry["opened_at"]).isoformat(),
            })
    
    return {
        "matched": matched,
        "only_live": only_live,
        "only_backtest": only_backtest,
        "summary": {
            "live_total": len(live),
            "backtest_total": len(backtest),
            "matched_count": len(matched),
            "only_live_count": len(only_live),
            "only_backtest_count": len(only_backtest),
            "match_rate": len(matched) / max(len(live), 1) * 100,
        }
    }


def generate_report(days: int, interval: int) -> dict:
    """Сгенерировать отчёт parity check."""
    print(f"🔍 Parity check: {days} дней, интервал {interval} минут")
    print()
    
    print("📊 Загрузка реальных входов...")
    live_entries = load_live_entries(days)
    print(f"   Найдено {len(live_entries)} входов в live")
    print()
    
    print("🧪 Запуск backtest...")
    backtest_entries = run_backtest(days, interval)
    print(f"   Найдено {len(backtest_entries)} входов в backtest")
    print()
    
    print("⚖️  Сравнение...")
    result = compare_entries(live_entries, backtest_entries)
    summary = result["summary"]
    
    print()
    print("=" * 70)
    print("📋 PARITY REPORT")
    print("=" * 70)
    print(f"Live trades:     {summary['live_total']}")
    print(f"Backtest trades: {summary['backtest_total']}")
    print(f"Matched:         {summary['matched_count']} ({summary['match_rate']:.1f}%)")
    print(f"Only in live:    {summary['only_live_count']}")
    print(f"Only in backtest:{summary['only_backtest_count']}")
    print("=" * 70)
    
    if result["matched"]:
        print()
        print("✅ Совпавшие сделки (первые 10):")
        for m in result["matched"][:10]:
            print(f"   {m['symbol']:10s} {m['side']:4s} "
                  f"Δt={m['time_diff_seconds']:3.0f}s "
                  f"strategy={m.get('live_strategy', '?')}")
    
    if result["only_live"]:
        print()
        print("⚠️  Только в live (первые 10):")
        for entry in result["only_live"][:10]:
            print(f"   {entry['symbol']:10s} {entry['side']:4s} "
                  f"{entry['opened_at']} strategy={entry.get('strategy', '?')}")
    
    if result["only_backtest"]:
        print()
        print("⚠️  Только в backtest (первые 10):")
        for entry in result["only_backtest"][:10]:
            print(f"   {entry['symbol']:10s} {entry['side']:4s} "
                  f"{entry['opened_at']}")
    
    # Сохраняем отчёт в JSON
    OUTPUT_DIR.mkdir(exist_ok=True)
    report_file = OUTPUT_DIR / f"parity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    
    report_data = {
        "timestamp": datetime.now().isoformat(),
        "days": days,
        "interval": interval,
        "result": result,
    }
    
    with open(report_file, "w") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)
    
    print()
    print(f"💾 Отчёт сохранён: {report_file}")
    
    return report_data


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Parity check: live vs backtest")
    parser.add_argument("--days", type=int, default=7,
                        help="Количество дней для анализа (по умолчанию 7)")
    parser.add_argument("--interval", type=int, default=240,
                        help="Интервал свечей в минутах (по умолчанию 240)")
    
    args = parser.parse_args()
    
    try:
        generate_report(args.days, args.interval)
    except KeyboardInterrupt:
        print("\n⛔ Отменено пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
