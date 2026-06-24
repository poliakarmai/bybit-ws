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
from .push_notifier import send_critical_alert, send_high_alert
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
from .risk_manager import check as risk_check, is_circuit_breaker_active

# ── Константы ──
SHUTDOWN = False


# ═══════════════════════════════════════════════════════════
# Async helpers
# ═══════════════════════════════════════════════════════════

_TIMEOUT_SENTINEL = object()  # sentinel for timeout detection

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
        fn_name = fn.__name__ if hasattr(fn, '__name__') else 'unknown'
        log_event(f'⚠️ TIMEOUT {fn_name} after {timeout}s — result discarded')
        return _TIMEOUT_SENTINEL, fn_name
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
    """Параллельная загрузка позиций + ордеров (REST)."""
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


async def async_positions_snapshot_ws(last_rest_sync: float, rest_interval: float):
    """
    Гибридная загрузка позиций (Фаза 6.3):
    - Если BYBIT_WS_FULL_ENABLED=1 и приватный WS жив: позиции из WS-кеша
    - REST-сверка раз в rest_interval секунд для коррекции
    - При падении WS: fallback на REST
    Возвращает (positions, orders, new_last_rest_sync).
    """
    from .ws_client import (
        is_full_enabled, is_private_connected, is_private_stale,
        get_all_positions, get_executions,
    )

    if is_full_enabled() and is_private_connected() and not is_private_stale(120):
        # WS-режим: позиции из кеша
        positions = get_all_positions()
        orders = {}  # ордера из execution-кеша (не полные, но актуальные)

        # REST-сверка раз в REST_SYNC_INTERVAL
        now = time.time()
        new_last_rest_sync = last_rest_sync
        if now - last_rest_sync > rest_interval:
            try:
                rest_positions, rest_orders = await fetch_positions_and_orders()
                if rest_positions:
                    # Сохраняем снепшоты
                    await asyncio.get_event_loop().run_in_executor(
                        None, save_json, POSITIONS_SNAPSHOT, rest_positions
                    )
                    # Сверяем: если REST отличается → логируем, но используем REST
                    ws_keys = set(positions.keys())
                    rest_keys = set(rest_positions.keys())
                    if ws_keys != rest_keys:
                        log_event(f'🔄 WS/REST desync: WS={ws_keys} REST={rest_keys}')
                        positions = rest_positions
                    orders = rest_orders if rest_orders else orders
                    new_last_rest_sync = now
            except Exception as e:
                log_event(f'⚠️ WS REST sync error: {e}')

        return positions, orders, new_last_rest_sync

    # Fallback: чистый REST
    positions, orders = await async_positions_snapshot()
    return positions, orders, last_rest_sync


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

    # ── Фаза 5.4: авто-переключение LONG/SHORT по режиму (BYBIT_REGIME_AUTO=1) ──
    regime_auto = os.environ.get('BYBIT_REGIME_AUTO', '0') == '1'
    if regime_auto:
        try:
            from .lstm_regime import get_current_regime_strategy
            reg_strat = get_current_regime_strategy()
            from . import __init__ as _pkg
            _pkg.REGIME_LONG_ENABLED = reg_strat['LONG_ENABLED']
            _pkg.REGIME_SHORT_ENABLED = reg_strat['SHORT_ENABLED']
            if cycle_count % 10 == 0:  # логируем раз в 10 тяжёлых циклов
                long_icon = 'OK' if reg_strat['LONG_ENABLED'] else 'OFF'
                short_icon = 'OK' if reg_strat['SHORT_ENABLED'] else 'OFF'
                log_event(
                    f'REGIME_AUTO: regime={reg_strat["regime"]} conf={reg_strat["confidence"]}% '
                    f'LONG={long_icon} SHORT={short_icon}'
                )
        except Exception as e:
            log_event(f'REGIME_AUTO error: {e}')

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
            send_critical_alert(msg)  # Push: 🚨 на телефон

        weekly_msgs, _ = await run_in_thread(check_weekly_pumps)
        for msg in (weekly_msgs or []):
            add_alert('STOP', msg)
            send_critical_alert(msg)  # Push: 🚨 на телефон

    # DCA
    if not rpc_state.get("paused"):
        dca_msgs = await run_in_thread(check_dca)
        for msg in (dca_msgs[0] or []):
            add_alert('ENTRY', msg)
            send_high_alert(msg, level='ENTRY')  # Push: ⚡ на телефон

    # Partial TP (каждые 4 цикла)
    if cycle_count % 4 == 0:
        ptp_msgs = await run_in_thread(check_partial_tp)
        for msg in (ptp_msgs[0] or []):
            add_alert('TP', msg)
            send_high_alert(msg, level='TP')  # Push: ⚡ на телефон

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
        # ── Risk Manager: проверка перед авто-входом ──
        entries = await run_in_thread(auto_entry_scan, positions or {})
        for entry in (entries[0] or []):
            # Извлекаем символ из строки входа для проверки риска
            import re
            sym_match = re.search(r'\b([A-Z]+USDT)\b', entry)
            if sym_match:
                sym = sym_match.group(1)
                side = 'Sell' if 'SHORT' in entry else 'Buy'
                entry_allowed, risk_reason = risk_check(positions or {}, new_symbol=sym, new_side=side)
                if not entry_allowed:
                    log_event(f'🛑 Risk blocked auto-entry {sym}: {risk_reason}')
                    continue
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

    # ── Фаза 6.3: Проверить WS Full feature flag ──
    _WS_FULL_ENABLED = os.environ.get('BYBIT_WS_FULL_ENABLED', '0') == '1'
    _last_rest_sync = 0.0  # время последнего REST-сверочного опроса
    _REST_SYNC_INTERVAL = 60.0  # REST-сверка раз в 60 сек

    log_event('🚀 Async main loop v2 started' + (' [WS FULL]' if _WS_FULL_ENABLED else ''))
    print(f"[{datetime.now():%H:%M:%S}] 🚀 Async main loop: cycle={CYCLE_SECONDS}s heavy={HEAVY_CYCLE}"
          + (' WS_FULL=ON' if _WS_FULL_ENABLED else ''))

    # ── Фаза 5.2: Проверить Optuna feature flag ──
    if os.environ.get('BYBIT_OPTUNA_ENABLED', '0') == '1':
        try:
            from .optuna_tuner import load_optuna_params
            optuna_p = load_optuna_params()
            if optuna_p:
                log_event(f'📊 Optuna ENABLED: {len(optuna_p)} symbols loaded')
                print(f'[{datetime.now():%H:%M:%S}] 📊 Optuna: {len(optuna_p)} symbols with tuned params')
            else:
                log_event('📊 Optuna ENABLED but no params found — run optuna_tuner first')
        except Exception as e:
            log_event(f'⚠️ Optuna init: {e}')

    # Инициализация watchlist
    await run_in_thread(rotate_watchlist)

    # Загружаем начальные позиции (REST — WS ещё не инициализирован)
    old_positions, _ = await async_positions_snapshot()
    # _last_rest_sync инициализирован выше (0.0)

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

    # WebSocket push-сервер для real-time дашборда (:8767)
    try:
        from .rpc import start_ws_server
        ws_push_thread = start_ws_server(port=8767, bind='127.0.0.1')
        log_event('📡 WebSocket push server on 127.0.0.1:8767 (real-time dashboard)')
    except Exception as e:
        log_event(f'⚠️ WS push server start error: {e}')

    # WebSocket — в потоке (публичный + приватный при BYBIT_WS_FULL_ENABLED=1)
    try:
        from .ws_client import start as ws_start, is_full_enabled as ws_full
        ws_thread = threading.Thread(target=ws_start, daemon=True)
        ws_thread.start()
        if ws_full():
            log_event('📡 WebSocket clients started: public + private (FULL mode)')
        else:
            log_event('📡 WebSocket client started (public only)')
    except Exception as e:
        log_event(f'⚠️ WS start error: {e}')

    cycle = 0

    while not SHUTDOWN:
        try:
            cycle += 1
            t0 = time.monotonic()

            if cycle <= 2 or cycle % 10 == 0:
                log_event(f'🔄 cycle #{cycle}: starting...')

            # ── Параллельная загрузка позиций + ордеров ──
            # Фаза 6.3: при BYBIT_WS_FULL_ENABLED=1 — WS-push с REST-сверкой
            # Жёсткий таймаут 20с: httpx keep-alive может виснуть на Bybit
            try:
                new_positions, new_orders, _last_rest_sync = await asyncio.wait_for(
                    async_positions_snapshot_ws(_last_rest_sync, _REST_SYNC_INTERVAL),
                    timeout=20.0
                )
            except asyncio.TimeoutError:
                log_event(f'⚠️ positions snapshot TIMEOUT (20s), using empty')
                new_positions, new_orders = {}, {}
            except Exception as e:
                log_event(f'⚠️ positions snapshot error: {e}')
                new_positions, new_orders = {}, {}

            # ── Обновляем RPC-состояние ──
            try:
                from .rpc import rpc_state as _rpc_state, update_health as _rpc_health
                _rpc_state['alive'] = True
                _rpc_state['cycle_count'] = cycle
                _rpc_state['last_cycle'] = time.time()
                _rpc_health(alive=True)
            except Exception as e:
                log_event(f'⚠️ RPC state update error: {e}')

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

            # ── Risk Manager: circuit breaker check (каждый цикл) ──
            risk_result, risk_err = await run_in_thread(
                risk_check, new_positions or {}
            )
            if isinstance(risk_result, tuple) and len(risk_result) == 2:
                cb_allowed, cb_reason = risk_result
            else:
                cb_allowed, cb_reason = True, ''
            if not cb_allowed and is_circuit_breaker_active():
                if cycle % 10 == 0:  # логируем не каждый цикл
                    log_event(f'🛑 CIRCUIT BREAKER ACTIVE: {str(cb_reason)[:200]}')
                # Обновляем RPC-состояние
                from .rpc import rpc_state as _rpc
                _rpc['circuit_breaker'] = True
                _rpc['circuit_breaker_reason'] = str(cb_reason)
            else:
                from .rpc import rpc_state as _rpc
                _rpc['circuit_breaker'] = False
                _rpc['circuit_breaker_reason'] = ''

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

            # ── A/B-тест: логирование статуса (каждые 10 циклов) ──
            if cycle % 10 == 0:
                try:
                    from .ab_test import is_ab_enabled, get_status as _ab_status
                    if is_ab_enabled():
                        ab = _ab_status()
                        if ab.get('significance', {}).get('verdict', '') not in ('', 'недостаточно данных'):
                            log_event(f'🧪 A/B вердикт: {ab["significance"]["verdict"]} '
                                      f'(p_boot={ab["significance"].get("p_value_bootstrap")})')
                except Exception as e:
                    log_event(f'⚠️ ab_status log: {e}')

            elapsed = time.monotonic() - t0
            if cycle % 10 == 0:
                pos_count = len(new_positions) if new_positions else 0
                ord_count = len(new_orders) if new_orders else 0
                log_event(f'⚡ cycle #{cycle}: {pos_count} pos, {ord_count} orders in {elapsed:.2f}s')

            # Кешируем для следующего цикла
            old_positions = new_positions

            # ── WebSocket push для real-time дашборда ──
            try:
                from .rpc import ws_broadcast
                pos_list = []
                if new_positions:
                    for sym, p in new_positions.items():
                        p = dict(p)
                        p['symbol'] = sym
                        pos_list.append(p)
                ws_broadcast({
                    'positions': pos_list,
                    'monitor': {
                        'alive': True,
                        'cycle_count': cycle,
                        'ts': time.time(),
                    },
                })
            except Exception:
                pass

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
