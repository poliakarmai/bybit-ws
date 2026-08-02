"""
Bybit WS SDK — Python-клиент для AI-агентов.

Минимальный интерфейс для управления трейдинг-монитором через REST API.

Usage:
    from bybit_ws_sdk import Monitor
    m = Monitor("http://localhost:8766")
    
    # Проверить здоровье
    health = m.health()
    
    # Получить сигналы
    signals = m.scan(mode="short", limit=3)
    
    # Войти в позицию
    m.enter("WLDUSDT", "Sell", qty=50, sl=0.52, tp=0.35)
    
    # Закрыть позицию
    m.close("WLDUSDT")
    
    # Позиции
    positions = m.positions()
"""

import requests
from typing import Optional, Dict, Any


class Monitor:
    """Клиент для Bybit Bollinger Grid Monitor REST API."""
    
    def __init__(self, base_url: str = "http://localhost:8766", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
    
    def _get(self, path: str) -> Dict[str, Any]:
        r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()
    
    def _post(self, path: str, data: dict) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}{path}", json=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
    
    def health(self) -> dict:
        """Статус монитора: alive, uptime, cycle_count, watchdog_ok."""
        return self._get("/health")
    
    def positions(self) -> dict:
        """Текущие позиции: count, long, short, total_pnl, positions[]. """
        return self._get("/positions")
    
    def orders(self) -> dict:
        """Активные лимитные ордера."""
        return self._get("/orders")
    
    def metrics(self) -> dict:
        """Метрики за сегодня: alerts, auto_entries, sl_hits, tp_hits."""
        return self._get("/metrics")
    
    def signals(self) -> dict:
        """Текущие SHORT-кандидаты (перегретые монеты)."""
        return self._get("/signals")
    
    def scan(self, mode: str = "short", limit: int = 5) -> dict:
        """Запустить GridSignal-сканер. mode: long|short."""
        return self._post("/scan", {"mode": mode, "limit": limit})
    
    def enter(self, symbol: str, side: str, qty: float,
              sl: Optional[float] = None, tp: Optional[float] = None) -> dict:
        """Войти в позицию рынком."""
        data = {"symbol": symbol, "side": side, "qty": qty}
        if sl: data["sl"] = sl
        if tp: data["tp"] = tp
        return self._post("/enter", data)
    
    def close(self, symbol: str) -> dict:
        """Закрыть позицию рынком."""
        return self._post("/close", {"symbol": symbol})


# ─── Webhook handler (для приёма уведомлений от монитора) ───

class WebhookHandler:
    """
    Принимает webhook-уведомления от монитора (SL/TP-hit).
    
    Монитор отправляет POST на webhook_url с телом:
    {"event": "SL_HIT"|"TP_HIT", "symbol": "WLDUSDT", "pnl": "+2.50", "entry": 0.50, "exit": 0.52}
    """
    
    def __init__(self):
        self._handlers = {}
    
    def on(self, event: str):
        """Декоратор: зарегистрировать обработчик события."""
        def decorator(fn):
            self._handlers[event] = fn
            return fn
        return decorator
    
    def handle(self, payload: dict):
        """Обработать входящий webhook."""
        event = payload.get("event", "")
        if event in self._handlers:
            return self._handlers[event](payload)
        return None


# ─── Пример использования ───

if __name__ == "__main__":
    m = Monitor()
    
    # Проверить что монитор жив
    h = m.health()
    print(f"Status: {h['status']}, uptime: {h['uptime']}s")
    
    # Сканировать SHORT-сигналы
    scan = m.scan("short", limit=3)
    for s in scan.get("signals", []):
        print(f"  {s['symbol']}: score={s['score']}, BB={s['bb_pos']}%")
    
    # Позиции
    pos = m.positions()
    print(f"Positions: {pos['count']} ({pos['long']}L/{pos['short']}S), PnL: {pos['total_pnl']}")
