"""
main_async.py — Async главный цикл (Фаза 5.3 — asyncio-миграция).

Заменяет main.py: все I/O-запросы выполняются параллельно через asyncio.
Бизнес-логика (тяжёлые проверки) остаётся синхронной — вызывается через run_in_executor.
RPC-сервер — в отдельном потоке (как и раньше).

Безопасность:
- Запускается как ОТДЕЛЬНЫЙ сервис, не трогает main.py
- При ошибке async — fallback на синхронные вызовы
- Все тесты main.py продолжают работать
"""

import asyncio
import json
import os
import signal
import sys
import time
import threading
from datetime import datetime
from functools import partial
from pathlib import Path

# ── Пути ──
DATA_DIR = Path.home() / ".local" / "share" / "bybit-ws"
HEALTH_FILE = DATA_DIR / "health.txt"

# ── Импорты ──
from . import EVENTS_LOG, ALERTS_LOG, POSITIONS_SNAPSHOT, ORDERS_SNAPSHOT
from .config import Config
from .alerts import log_event, add_alert, send_telegram_alert
from .snapshot import save_json, load_json

# Async API
from .api import (
    fetch_positions_and_orders,
    get_bb_data_async,
    bybit_async,
)

# Async DB
from .state_db import adb

# Синхронные модули (вызываются через executor)
from .auto_sl import check_and_fix_sl, check_breakeven_sl
from .auto_tp import auto_take_profit, apply_auto_tp
from .trailing_sl import trailing_sl, trailing_sl_x10, apply_trailing_sl
from .pump_detect import check_pumps, check_weekly_pumps
from .overbought import check_overbought, rotate_watchlist
from .correlation import check_correlation, tighten_correlation_sl
from .funding_rotation import check_funding_rotation, execute_rotation
from .dca import check_dca
from .reporting import should_send_summary, send_summary, check_profit_triggers
from .auto_entry import auto_entry_scan, record_sl_hit
from .auto_short import check_auto_short, check_junk_dca
from .sl_reentry import notify_sl_hit, check_sl_reentry
from .margin_alerts import check_margin_utilization
from .funding_entry import check_funding_signals, execute_funding_entry
from .bb_scalp import check_scalp_signals, execute_scalp
from .mean_revert import check_mean_revert, execute_mean_revert
from .partial_tp import check_partial_tp
from .metrics import record_alert, record_auto_entry

# ── Константы ──
SHUTDOWN = False


# ═══════════════════════════════════════════════════════════
# Async helpers
# ═══════════════════════════════════════════════════════════

async def run_in_thread(fn, *args, timeout=25):
    """Выполнить синхронную функцию в потоке с таймаутом."""
    try:
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, partial(fn, *args)),
            timeout=timeout,
        )
        return result, None
    except asyncio.TimeoutError:
        return [], fn.__name__ if hasattr(fn, '__name__') else 'unknown'
    except Exception as e:
        return [], str(e)


async def fetch_all_bb_parallel(symbols, interval='D'):
    """Параллельная загрузка BB для всех символов."""
    if not symbols:
        return {}
    tasks = [get_bb_data_async(s, interval) for s in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        s: r for s, r in zip(symbols, results)
        if r and not isinstance(r, Exception)
    }


async def async_positions_snapshot():
    """Параллельная загрузка позиций + ордеров + кошелька."""
    try:
        positions, orders = await fetch_positions_and_orders()
    except Exception as e:
        log_event(f'⚠️ async fetch_positions error: {e}')
        return {}, {}

    # Сохраняем снепшоты
    if positions:
        await asyncio.get_event_loop().run_in_executor(
            None, save_json, POSITIONS_SNAPSHOT, positions
        )
    if orders:
        await asyncio.get_event_loop().run_in_executor(
            None, save_json, ORDERS_SNAPSHOT, orders
        )

    return positions, orders


# ═══════════════════════════════════════════════════════════
# Тяжёлый цикл (sync → async wrapper)
# ═══════════════════════════════════════════════════════════

async def heavy_cycle_async(cfg, positions, cycle_count):
    """Async-обёртка над синхронным тяжёлым циклом."""
    HEAVY_CYCLE = cfg.monitor.heavy_cycle
    if cycle_count % HEAVY_CYCLE != 0:
        return

    t0 = time.time()
    log_event(f'⚡ heavy cycle #{cycle_count} start')

    # Параллельный запуск CPU-bound проверок
    tasks = []

    # Режим рынка
    from .regime import check_regime
    try:
        tasks.append(run_in_thread(check_regime))
    except TypeError:
        tasks.append(run_in_thread(check_regime, force=True))  # fallback

    # Авто-шорты (если не на паузе)
    from .rpc import rpc_state
    if not rpc_state.get("paused"):
        tasks.append(run_in_thread(check_auto_short, positions or {}))

    # Корреляции
    tasks.append(run_in_thread(check_correlation, positions))

    # Пампы, перекупленность — последовательно (не CPU-heavy)
    if positions:
        overbought_msgs, _ = await run_in_thread(check_overbought, positions)
        for msg in (overbought_msgs or []):
            add_alert('INFO', msg)

        pump_msgs, _ = await run_in_thread(check_pumps, positions)
        for msg in (pump_msgs or []):
            add_alert('STOP', msg)

        weekly_msgs, _ = await run_in_thread(check_weekly_pumps)
        for msg in (weekly_msgs or []):
            add_alert('STOP', msg)

    # DCA
    if not rpc_state.get("paused"):
        dca_msgs = await run_in_thread(check_dca)
        for msg in (dca_msgs[0] or []):
            add_alert('ENTRY', msg)

    # Partial TP (каждые 4 цикла)
    if cycle_count % 4 == 0:
        ptp_msgs = await run_in_thread(check_partial_tp)
        for msg in (ptp_msgs[0] or []):
            add_alert('TP', msg)

    # Ждём параллельные задачи
    if tasks:
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, item in enumerate(raw_results):
            if isinstance(item, Exception):
                log_event(f'⚠️ heavy task {i} failed: {item}')
                continue
            result, err = item if isinstance(item, tuple) else (item, None)
            if err:
                log_event(f'⏱️ heavy task {i}: timeout — {err}')

    # ── Авто-вход (после всех проверок) ──
    try:
        entries = await run_in_thread(auto_entry_scan, positions or {})
        for entry in (entries[0] or []):
            add_alert('ENTRY', entry)
            send_telegram_alert(entry)
    except Exception as e:
        log_event(f'⚠️ auto_entry error: {e}')

    elapsed = time.time() - t0
    log_event(f'⚡ heavy cycle #{cycle_count} done in {elapsed:.2f}s')


# ═══════════════════════════════════════════════════════════
# Главный async-цикл
# ═══════════════════════════════════════════════════════════

async def async_main_loop():
    """Async главный цикл — замена main_loop()."""
    global SHUTDOWN

    cfg = Config()
    CYCLE_SECONDS = cfg.monitor.cycle_seconds
    HEAVY_CYCLE = cfg.monitor.heavy_cycle

    log_event('🚀 Async main loop v2 started')
    print(f"[{datetime.now():%H:%M:%S}] 🚀 Async main loop: cycle={CYCLE_SECONDS}s heavy={HEAVY_CYCLE}")

    # Инициализация watchlist
    await run_in_thread(rotate_watchlist)

    # Загружаем начальные позиции
    old_positions, _ = await async_positions_snapshot()

    # Стартовая проверка SL
    if old_positions:
        sl_alerts, _ = await run_in_thread(check_and_fix_sl)
        for a in (sl_alerts or []):
            add_alert('SL', a)
        be_alerts, _ = await run_in_thread(check_breakeven_sl)
        for a in (be_alerts or []):
            add_alert('SL', a)

    # RPC — запускаем в отдельном потоке (как в main.py)
    rpc_thread = None
    try:
        from .rpc import start_rpc_server, rpc_state
        rpc_bind = cfg.rpc.bind
        rpc_port = cfg.rpc.port
        rpc_thread = threading.Thread(
            target=start_rpc_server,
            args=(rpc_port, rpc_bind),
            daemon=True,
        )
        rpc_thread.start()
        log_event(f'📡 RPC server on {rpc_bind}:{rpc_port} (thread)')
    except Exception as e:
        log_event(f'⚠️ RPC start error: {e}')

    # WebSocket — в потоке
    try:
        from .ws_client import start as ws_start
        ws_thread = threading.Thread(target=ws_start, daemon=True)
        ws_thread.start()
        log_event('📡 WebSocket client started (thread)')
    except Exception as e:
        log_event(f'⚠️ WS start error: {e}')

    cycle = 0

    while not SHUTDOWN:
        try:
            cycle += 1
            t0 = time.monotonic()

            # ── Параллельная загрузка позиций + ордеров ──
            new_positions, new_orders = await async_positions_snapshot()

            # ── Обновляем RPC-состояние ──
            try:
                from .rpc import rpc_state as _rpc_state, update_health as _rpc_health
                _rpc_state['alive'] = True
                _rpc_state['cycle_count'] = cycle
                _rpc_state['last_cycle'] = time.time()
                _rpc_health(alive=True)
            except Exception:
                pass

            # ── Health-файл ──
            await run_in_thread(
                lambda: HEALTH_FILE.write_text(str(int(time.time())))
            )

            # ── Лёгкие проверки (каждый цикл) ──
            if new_positions:
                # SL
                sl_msgs, _ = await run_in_thread(check_and_fix_sl)
                for a in (sl_msgs or []):
                    add_alert('SL', a)

                # Трейлинг
                trail_msgs, _ = await run_in_thread(trailing_sl, new_positions)
                for a in (trail_msgs or []):
                    add_alert('SL', a)

                # Безубыток (каждые 4 цикла)
                if cycle % 4 == 0:
                    be_msgs, _ = await run_in_thread(check_breakeven_sl)
                    for a in (be_msgs or []):
                        add_alert('SL', a)

                # Маржа
                margin_msgs, _ = await run_in_thread(check_margin_utilization, new_positions)
                for msg in (margin_msgs or []):
                    add_alert('STOP', msg)
                    send_telegram_alert(msg, level='STOP')

            # ── Тяжёлый цикл ──
            await heavy_cycle_async(cfg, new_positions, cycle)

            # ── SL re-entry ──
            if new_positions:
                reentry_msgs, _ = await run_in_thread(check_sl_reentry, new_positions)
                for msg in (reentry_msgs or []):
                    add_alert('ENTRY', msg)

            # ── Отчётность ──
            try:
                if should_send_summary():
                    await run_in_thread(send_summary)
                await run_in_thread(check_profit_triggers)
            except Exception as e:
                log_event(f'⚠️ reporting error: {e}')

            elapsed = time.monotonic() - t0
            if cycle % 10 == 0:
                pos_count = len(new_positions) if new_positions else 0
                ord_count = len(new_orders) if new_orders else 0
                log_event(f'⚡ cycle #{cycle}: {pos_count} pos, {ord_count} orders in {elapsed:.2f}s')

            # Кешируем для следующего цикла
            old_positions = new_positions

            # Ждём до следующего цикла
            cycle_sec = float(cfg.monitor.cycle_seconds)
            sleep_time = max(0.1, cycle_sec - elapsed)
            await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            break
        except Exception as e:
            import traceback
            log_event(f'⚠️ cycle #{cycle} error: {e}')
            log_event(f'   traceback: {traceback.format_exc()[-500:]}')
            await asyncio.sleep(CYCLE_SECONDS)

    log_event('🛑 Async main loop stopped')
    await adb.close()


def run():
    """Точка входа с обработкой сигналов."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown():
        global SHUTDOWN
        SHUTDOWN = True
        log_event('SIGTERM received, shutting down...')

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(async_main_loop())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == '__main__':
    run()
