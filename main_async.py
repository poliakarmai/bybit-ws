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
import traceback
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
from .state_db import adb, StateDB
sync_db = StateDB()

# Синхронные модули (вызываются через executor)
from .auto_sl import check_and_fix_sl, check_breakeven_sl  # legacy — kept for reference
from .unified_sl import manage_sl
from .auto_tp import auto_take_profit, apply_auto_tp
from .trailing_sl import trailing_sl, trailing_sl_x10, simple_trailing_sl, tight_trailing_sl, apply_trailing_sl
from .pump_detect import check_pumps, check_weekly_pumps
from .overbought import check_overbought, rotate_watchlist
from .correlation import check_correlation, tighten_correlation_sl
from .funding_rotation import check_funding_rotation
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
from .metrics import record_alert, record_auto_entry, ensure_today
from .risk_manager import check as risk_check, is_circuit_breaker_active

# ── Константы ──
SHUTDOWN = False
MEAN_REVERT_ENABLED = False  # отключено 01.08.2026
MEAN_REVERT_ENABLED = False  # отключено 01.08.2026 — BB%=0-5% на падающем рынке не работает


# ═══════════════════════════════════════════════════════════
# Async helpers
# ═══════════════════════════════════════════════════════════
def _clean_pump_state(data_dir, positions):
    """Sync helper: sqlite3 + pumps.json cleanup."""
    import sqlite3
    from .alerts import log_event
    db_path = str(data_dir / 'state.db')
    db = sqlite3.connect(db_path)
    try:
        pump_syms = [r[0] for r in db.execute("SELECT symbol FROM pump_state").fetchall()]
        orphaned = [s for s in pump_syms if s not in (positions or {})]
        for sym in orphaned:
            db.execute("DELETE FROM pump_state WHERE symbol=?", (sym,))
            db.commit()
            log_event(f'🧹 pump_state clean: {sym} (позиция закрыта)')
    finally:
        db.close()
    
    import json as _json
    pump_file = data_dir / 'pumps.json'
    if pump_file.exists():
        pumps = _json.loads(pump_file.read_text())
        stale = [s for s in pumps if s not in (positions or {})]
        if stale:
            for s in stale:
                del pumps[s]
            pump_file.write_text(_json.dumps(pumps, indent=2))
            log_event(f'🧹 pumps.json clean: {len(stale)} orphaned entries removed')


def _import_bybit_trades(data_dir):
    """Sync helper: import closed trades from Bybit API + dedup + canary tracking."""
    import json as _json
    from .api import bybit
    from .alerts import log_event
    from .journal.self_learn import (
        record_canary_result as _record_canary,
        match_canary_entry as _match_canary,
        record_exit as _record_exit,        # NEW v4
        record_trade_result as _record_streak,  # NEW v4
    )
    trades_file = data_dir / 'trades.jsonl'
    existing_keys = set()
    if trades_file.exists():
        with open(trades_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line:
                    try:
                        _d = _json.loads(_line)
                        existing_keys.add((_d.get('symbol',''), _d.get('ts',0), _d.get('side','')))
                    except Exception:
                        pass
    hist = bybit('GET', '/v5/position/closed-pnl?category=linear&limit=100')
    imported = 0
    if hist and hist.get('retCode') == 0:
        with open(trades_file, 'a') as _f:
            for item in hist['result'].get('list', []):
                sym = item.get('symbol', '')
                side = 'sell' if item.get('side') == 'Sell' else 'buy'
                qty = float(item.get('qty', 0))
                price = float(item.get('avgExitPrice', item.get('avgEntryPrice', 0)))
                ts_val = int(item.get('updatedTime', 0)) / 1000
                pnl = float(item.get('closedPnl', 0))
                key = (sym, ts_val, side)
                if key not in existing_keys and qty > 0:
                    _f.write(_json.dumps({
                        'symbol': sym, 'side': side, 'qty': qty,
                        'price': price, 'ts': ts_val, 'fee': 0, 'source': 'bybit_history',
                        'pnl': pnl,
                    }) + '\n')
                    imported += 1
                    existing_keys.add(key)
                    # ── SQLite: запись в trade_history для self-learn ──
                    try:
                        entry_ts_raw = int(item.get('createdTime', 0))
                        hold_h = (ts_val - entry_ts_raw/1000) / 3600 if entry_ts_raw else None
                        entry_px = float(item.get('avgEntryPrice', 0))
                        exit_px = float(item.get('avgExitPrice', 0))
                        # Bybit returns stopLoss/takeProfit=null for closed positions.
                        # Detect exit reason from price movement instead.
                        if entry_px > 0 and exit_px > 0:
                            if side == 'sell':  # SHORT
                                if exit_px > entry_px:
                                    exit_reason = 'SL'
                                else:
                                    exit_reason = 'TP'
                            else:  # LONG
                                if exit_px < entry_px:
                                    exit_reason = 'SL'
                                else:
                                    exit_reason = 'TP'
                            if hold_h and hold_h > 48:
                                exit_reason = 'Time'
                        else:
                            exit_reason = 'Unknown'
                        sync_db.add_trade(
                            symbol=sym, side='Buy' if side == 'buy' else 'Sell',
                            strategy='auto', entry_price=float(item.get('avgEntryPrice', 0)),
                            exit_price=price, size=qty, pnl=pnl,
                            entry_at=int(entry_ts_raw / 1000) if entry_ts_raw else None,
                            closed_at=int(ts_val),
                            exit_reason=exit_reason, hold_hours=hold_h,
                        )
                    except Exception:
                        pass
                    # ── v4: Exit reason tracking + streak ──
                    entry_ts = int(item.get('createdTime', 0)) / 1000
                    try:
                        hold_h = (ts_val - entry_ts) / 3600 if entry_ts > 0 else 0
                        entry_px = float(item.get('avgEntryPrice', 0))
                        exit_px = float(item.get('avgExitPrice', 0))
                        # Bybit returns stopLoss/takeProfit=null. Detect from price.
                        if entry_px > 0 and exit_px > 0:
                            if side == 'sell':
                                exit_reason = 'SL' if exit_px > entry_px else 'TP'
                            else:
                                exit_reason = 'SL' if exit_px < entry_px else 'TP'
                            if hold_h > 48:
                                exit_reason = 'Time'
                        else:
                            exit_reason = 'Unknown'
                        _record_exit(sym, side, pnl, exit_reason, hold_h,
                                     float(item.get('avgEntryPrice', 0)),
                                     float(item.get('avgExitPrice', 0)))
                        _record_streak(pnl)
                    except Exception:
                        pass
                    # Проброс в canary-трекер: только для канареечных входов
                    try:
                        if _match_canary(sym, side, ts_val, window=int(ts_val - entry_ts) + 3600):
                            _record_canary(pnl > 0)
                    except Exception:
                        pass
    log_event(f'📥 Импорт истории Bybit: {imported} новых трейдов (всего {len(existing_keys)})')
    return imported


def _load_trades_for_journal(data_dir):
    """Sync helper: load trades.jsonl for journal + self-learning."""
    import json as _json
    trades_file = data_dir / 'trades.jsonl'
    trades = []
    if trades_file.exists():
        with open(trades_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line:
                    trades.append(_json.loads(_line))
    return trades



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
        return [], fn_name
    except Exception as e:
        import traceback
        fn_name = fn.__name__ if hasattr(fn, '__name__') else 'unknown'
        tb = traceback.format_exc()
        log_event(f'💥 run_in_thread({fn_name}) unhandled: {e}')
        log_event(f'   traceback: {tb[:500]}')
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
        await asyncio.get_event_loop().run_in_executor(
            None, sync_db.save_positions, positions
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
                    await asyncio.get_event_loop().run_in_executor(
                        None, sync_db.save_positions, rest_positions
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

async def heavy_cycle_async(cfg, positions, cycle_count, orders=None):
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
            import bybit_ws
            bybit_ws.REGIME_LONG_ENABLED = reg_strat['LONG_ENABLED']
            bybit_ws.REGIME_SHORT_ENABLED = reg_strat['SHORT_ENABLED']
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

    # Пампы, перекупленность, funding — параллельно (независимые API-вызовы)
    if positions:
        # Запускаем все независимые проверки параллельно (timeout 10s вместо 25s)
        pump_tasks = [
            run_in_thread(check_overbought, positions, timeout=10),
            run_in_thread(check_pumps, positions, timeout=10),
            run_in_thread(check_weekly_pumps, timeout=10),
            run_in_thread(check_funding_signals, positions, timeout=10),
            run_in_thread(check_funding_rotation, positions, timeout=10),
        ]
        pump_results = await asyncio.gather(*pump_tasks, return_exceptions=True)

        # Разбираем результаты
        overbought_result = pump_results[0]
        if not isinstance(overbought_result, Exception) and overbought_result:
            overbought_msgs, _ = overbought_result if isinstance(overbought_result, tuple) else (overbought_result, None)
            if isinstance(overbought_msgs, list):
                for msg in overbought_msgs:
                    add_alert('INFO', msg)

        pump_result = pump_results[1]
        if not isinstance(pump_result, Exception) and pump_result:
            pump_msgs, _ = pump_result if isinstance(pump_result, tuple) else (pump_result, None)
            if isinstance(pump_msgs, list):
                for msg in pump_msgs:
                    add_alert('STOP', msg)
                    send_critical_alert(msg)  # Push: 🚨 на телефон

        weekly_result = pump_results[2]
        if not isinstance(weekly_result, Exception) and weekly_result:
            weekly_msgs, _ = weekly_result if isinstance(weekly_result, tuple) else (weekly_result, None)
            if isinstance(weekly_msgs, list):
                for msg in weekly_msgs:
                    add_alert('STOP', msg)
                    send_critical_alert(msg)  # Push: 🚨 на телефон

        # ── Авто-очистка pump_state для закрытых позиций ──
        try:
            await run_in_thread(_clean_pump_state, DATA_DIR, positions)
        except Exception:
            pass

        # ── Funding Rate Momentum x10 ──
        funding_result = pump_results[3]
        if not isinstance(funding_result, Exception) and funding_result:
            try:
                fund_alerts, fund_entries = funding_result[0] if isinstance(funding_result[0], tuple) else (funding_result[0] or [], [])
                for msg in (fund_alerts or []):
                    add_alert('ENTRY', msg)
                for entry_info in (fund_entries or []):
                    try:
                        sym = entry_info['symbol']
                        side = entry_info['side']
                        entry_allowed, risk_reason = risk_check(positions or {}, new_symbol=sym, new_side=side)
                        if not entry_allowed:
                            log_event(f'🛑 Risk blocked funding {sym}: {risk_reason}')
                            continue
                        exec_result = await run_in_thread(execute_funding_entry, entry_info)
                        ok = exec_result[0] if isinstance(exec_result, tuple) else exec_result
                        if ok:
                            log_event(f'💰 Funding Entry: {entry_info}')
                    except Exception as e:
                        log_event(f'⚠️ funding execute error: {e}')
            except Exception as e:
                log_event(f'⚠️ funding_entry error: {e}\\n' + traceback.format_exc())

        # ── Funding Rotation (информационные алерты) ──
        rot_result = pump_results[4]
        if not isinstance(rot_result, Exception) and rot_result:
            try:
                rotations = rot_result[0] if isinstance(rot_result[0], list) else (rot_result[0] or [])
                for r in (rotations or []):
                    add_alert('INFO', '🔄 Funding Rotation: ' + str(r.get('from', '?')) + ' → ' + str(r.get('to', '?')) + ' (' + str(r.get('reason', '')) + ')')
            except Exception as e:
                log_event(f'⚠️ funding_rotation error: {e}\\n' + traceback.format_exc())

        # ── Mean Reversion Extreme x10 (ОТКЛЮЧЕНО 01.08.2026) ──
        if MEAN_REVERT_ENABLED:
            try:
                mr_result = await run_in_thread(check_mean_revert, positions)
                mean_alerts, mean_entries = mr_result[0] if isinstance(mr_result[0], tuple) else (mr_result[0] or [], [])
                for msg in (mean_alerts or []):
                    add_alert('ENTRY', msg)
                for entry_info in (mean_entries or []):
                    try:
                        sym = entry_info['symbol']
                        side = entry_info['side']
                        entry_allowed, risk_reason = risk_check(positions or {}, new_symbol=sym, new_side=side)
                        if not entry_allowed:
                            log_event(f'🛑 Risk blocked mean_revert {sym}: {risk_reason}')
                            continue
                        exec_result = await run_in_thread(execute_mean_revert, entry_info)
                        ok = exec_result[0] if isinstance(exec_result, tuple) else exec_result
                        if ok:
                            log_event(f'📊 Mean Revert Entry: {entry_info}')
                    except Exception as e:
                        log_event(f'⚠️ mean_revert execute error: {e}')
            except Exception as e:
                log_event(f'⚠️ mean_revert error: {e}\n' + traceback.format_exc())

        # ── BB Scalping M5/M15 x10 ──
        try:
            sc_result = await run_in_thread(check_scalp_signals, positions, 0)
            scalp_alerts, scalp_entries = sc_result[0] if isinstance(sc_result[0], tuple) else (sc_result[0] or [], [])
            for msg in (scalp_alerts or []):
                add_alert('ENTRY', msg)
            for entry_info in (scalp_entries or []):
                try:
                    sym = entry_info['symbol']
                    side = entry_info['side']
                    entry_allowed, risk_reason = risk_check(positions or {}, new_symbol=sym, new_side=side)
                    if not entry_allowed:
                        log_event(f'🛑 Risk blocked scalp {sym}: {risk_reason}')
                        continue
                    exec_result = await run_in_thread(execute_scalp, entry_info)
                    ok = exec_result[0] if isinstance(exec_result, tuple) else exec_result
                    if ok:
                        log_event(f'⚡ Scalp Entry: {entry_info}')
                except Exception as e:
                    log_event(f'⚠️ scalp execute error: {e}')
        except Exception as e:
            log_event(f'⚠️ bb_scalp error: {e}\n' + traceback.format_exc())

    # DCA
    if not rpc_state.get("paused"):
        dca_msgs = await run_in_thread(check_dca, positions)
        for msg in (dca_msgs[0] or []):
            add_alert('ENTRY', msg)
            send_high_alert(msg, level='ENTRY')  # Push: ⚡ на телефон
            # ── v8.1: DCA counter ──
            try:
                sym = msg.split()[0] if msg else ''
                if sym:
                    sync_db.inc_dca_for_symbol(sym)
            except Exception:
                pass

    # Partial TP (каждые 4 цикла)
    if cycle_count % 4 == 0:
        ptp_msgs = await run_in_thread(check_partial_tp, positions)
        for msg in (ptp_msgs[0] or []):
            add_alert('TP', msg)
            send_high_alert(msg, level='TP')  # Push: ⚡ на телефон
            # ── v8.1: partial TP counter ──
            try:
                sym = msg.split()[0] if msg else ''
                if sym:
                    sync_db.inc_partial_tp_for_symbol(sym)
            except Exception:
                pass

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

    # ── Auto-TP: ATR-based тейк-профиты ──
    try:
        tp_actions, tp_err = await run_in_thread(auto_take_profit, positions or {}, orders or {})
        if tp_actions:
            await run_in_thread(apply_auto_tp, tp_actions)
    except Exception as e:
        log_event(f'⚠️ auto_tp error: {e}')

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

    # ── Инициализация метрик за сегодня ──
    try:
        ensure_today()
    except Exception as e:
        log_event(f'⚠️ ensure_today error: {e}')

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
        sl_alerts, _ = await run_in_thread(manage_sl, old_positions, 0)
        if isinstance(sl_alerts, list):
            for a in sl_alerts:
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
        ws_push_thread = start_ws_server(port=8768, bind='127.0.0.1')
        log_event('📡 WebSocket push server on 127.0.0.1:8768 (real-time dashboard)')
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

            # ── Импорт истории Bybit (закрытые PnL) — раз в 10 циклов ──
            if cycle % 10 == 0:
                try:
                    _import_bybit_trades(DATA_DIR)
                except Exception as e:
                    log_event(f'⚠️ bybit history import: {e}')

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
                _rpc_health(alive=True, cycle_count=cycle)
                _rpc_state['alive'] = True
                _rpc_state['cycle_count'] = cycle
                _rpc_state['last_cycle'] = time.time()
            except Exception as e:
                log_event(f'⚠️ RPC state update error: {e}')

            # ── Health-файл ──
            await run_in_thread(
                lambda: HEALTH_FILE.write_text(str(int(time.time())))
            )

            # ── Лёгкие проверки (каждый цикл) ──
            # BlackSwan v2: корреляционная паника
            if new_positions:
                try:
                    from .risk_manager import check_correlation_panic
                    panic_reason = check_correlation_panic(new_positions)
                    if panic_reason:
                        log_event(f'🦢 BLACKSWAN ALERT: {panic_reason}')
                        send_high_alert(f'🦢 BLACKSWAN: {panic_reason}\n\nЗакрыть красные позиции? Напиши «да»', level='CRITICAL')
                        # v9.1: только алерт, без авто-закрытия. Ждём команду пользователя.
                except Exception as e:
                    log_event(f'⚠️ blackswan check error: {e}')
            if new_positions:
                # Unified SL — все механизмы в одном, 1 API-вызов на позицию
                sl_msgs, _ = await run_in_thread(manage_sl, new_positions, cycle)
                if isinstance(sl_msgs, list):
                    for a in sl_msgs:
                        add_alert('SL', a)

                # Маржа
                margin_msgs, _ = await run_in_thread(check_margin_utilization, new_positions)
                if isinstance(margin_msgs, list):
                    for msg in margin_msgs:
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
            await heavy_cycle_async(cfg, new_positions, cycle, new_orders)

            # ── SL re-entry ──
            if new_positions:
                reentry_msgs, _ = await run_in_thread(check_sl_reentry, new_positions)
                if isinstance(reentry_msgs, list):
                    for msg in reentry_msgs:
                        add_alert('ENTRY', msg)

            # ── Отчётность ──
            try:
                label = should_send_summary()
                if label:
                    await run_in_thread(send_summary, label)
                await run_in_thread(check_profit_triggers, new_positions)
            except Exception as e:
                log_event(f'⚠️ reporting error: {e}')

            # ── A/B-тест: логирование статуса (каждые 10 циклов) ──
            if cycle % 10 == 0:
                try:
                    from .ab_test import is_ab_enabled, get_status as _ab_status
                    if is_ab_enabled():
                        ab = _ab_status()
                        if isinstance(ab, dict) and (ab.get('significance') or {}).get('verdict', '') not in ('', 'недостаточно данных'):
                            sig = ab.get('significance') or {}
                            log_event(f'🧪 A/B вердикт: {sig.get("verdict", "?")} '
                                      f'(p_boot={sig.get("p_value_bootstrap")})')
                except Exception as e:
                    log_event(f'⚠️ ab_status log: {e}')

            # ── Self-learning + Post-trade анализ (каждые 6ч по wall clock) ──
            try:
                from .journal.self_learn import (
                    apply_journal_insights as _apply_insights,
                    should_run_self_learn as _should_learn,
                    mark_self_learn_run as _mark_learn,
                )
            except ImportError:
                _should_learn = lambda: cycle % 720 == 1
                _mark_learn = lambda: None
                _apply_insights = None

            if _should_learn():
                _mark_learn()
                try:
                    from .journal.adapter import load_from_sqlite
                    from .post_trade import analyze_clusters as _cluster_analysis

                    # Загружаем из SQLite (SSOT) — адаптер создаёт парные entry+close сделки
                    result = await run_in_thread(load_from_sqlite)
                    journal = result[0] if isinstance(result, tuple) else result
                    if journal and 'error' not in journal:
                        p = journal.get('profile', {})
                        log_event(f'🧠 Self-learn: {p.get("total_trades",0)} trades, '
                                  f'WR={p.get("win_rate",0):.1%}, PnL=${p.get("total_pnl",0):.0f}')
                        adjustments = await _apply_insights(journal, cfg)
                        if adjustments:
                            log_event(f'🧠 Self-learn adjustments: {list(adjustments.keys())}')

                    else:
                        log_event(f'⚠️ self_learn: {journal.get("error", "no data")}' if journal else '⚠️ self_learn: no data')

                    clusters = _cluster_analysis()
                    if clusters:
                        blocked = clusters.get('blocked', [])
                        if blocked:
                            log_event(f'🚫 Post-trade блок: {len(blocked)} кластеров')

                    # ── NEW v5: regime-aware stats ──
                    try:
                        from .journal.self_learn import get_regime_aware_stats
                        regime_stats = await run_in_thread(get_regime_aware_stats)
                        if regime_stats:
                            summary = ', '.join(
                                f'{r}:{s["trades"]}t/{s["win_rate"]:.0%}WR'
                                for r, s in sorted(regime_stats.items())
                            )
                            log_event(f'📊 Regime stats: {summary}')
                    except Exception:
                        pass
                except Exception as e:
                    log_event(f'⚠️ self_learn error: {e}')

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
            log_event(f'   traceback: {traceback.format_exc()[:500]}')
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
