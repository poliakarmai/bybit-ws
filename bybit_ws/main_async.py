"""
main_async.py — Async главный цикл (Фаза 4.7 — asyncio-миграция).

Демонстрирует архитектуру: параллельные API-запросы, asyncio.gather,
неблокирующий RPC через aiohttp. 

Это скелет — полная миграция 40+ часов. Запуск:
    python3 -m bybit_ws.main_async
"""

import asyncio
import time
import signal
from datetime import datetime

from .api import (
    fetch_positions_and_orders,
    get_bb_data_async,
    bybit_async,
)
from .state_db import adb
from .alerts import log_event, add_alert
from .config import Config


# ═══════════════════════════════════════════════════════════
# Async helpers
# ═══════════════════════════════════════════════════════════

async def fetch_all_bb(symbols, interval='D'):
    """Параллельная загрузка BB для всех символов."""
    tasks = [get_bb_data_async(s, interval) for s in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {s: r for s, r in zip(symbols, results) if r and not isinstance(r, Exception)}


async def health_check_loop(rpc_handler):
    """Неблокирующий health-check с обновлением RPC."""
    while True:
        try:
            # В будущем: await rpc_handler.update_health(alive=True)
            await adb.set_kv('health_last', str(int(time.time())))
        except Exception:
            pass
        await asyncio.sleep(30)


async def heavy_cycle(cfg, positions):
    """Тяжёлый цикл: авто-входы, шорты, корреляции."""
    await asyncio.sleep(0)  # placeholder — полная миграция 40+ часов
    # Пример архитектуры:
    # symbols = list(positions.keys()) + AUTO_ENTRY_WATCH
    # bb_data = await fetch_all_bb(list(set(symbols)))
    # 
    # # Параллельный скоринг
    # async def score_symbol(sym):
    #     bb = bb_data.get(sym)
    #     if not bb: return None
    #     return full_score_coin_async(sym, bb)
    # 
    # scores = await asyncio.gather(*[score_symbol(s) for s in symbols])
    # ...


# ═══════════════════════════════════════════════════════════
# Главный цикл
# ═══════════════════════════════════════════════════════════

SHUTDOWN = False


async def main_loop():
    """Async главный цикл."""
    global SHUTDOWN
    cfg = Config()
    cycle_sec = cfg.monitor.cycle_seconds
    heavy_every = cfg.monitor.heavy_cycle

    log_event('🚀 Async main loop started')

    # WebSocket-клиент (пока синхронный в потоке)
    try:
        from .ws_client import start as ws_start
        ws_start()
    except Exception as e:
        log_event(f'⚠️ WS start error: {e}')

    cycle = 0

    while not SHUTDOWN:
        try:
            cycle += 1
            t0 = time.monotonic()

            # ── Параллельная загрузка позиций и ордеров ──
            positions, orders = await fetch_positions_and_orders()
            await adb.set_kv('cycle_count', str(cycle))

            # ── Тяжёлый цикл ──
            if cycle % heavy_every == 0:
                await heavy_cycle(cfg, positions)

            elapsed = time.monotonic() - t0
            log_event(f'⚡ cycle #{cycle}: {len(positions)} pos, {len(orders)} orders in {elapsed:.2f}s')

            # ── Ждём до следующего цикла ──
            sleep_time = max(0, cycle_sec - elapsed)
            await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_event(f'⚠️ cycle error: {e}')
            await asyncio.sleep(cycle_sec)

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
            pass  # Windows

    try:
        loop.run_until_complete(main_loop())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == '__main__':
    run()
