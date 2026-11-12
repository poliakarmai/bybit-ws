#!/usr/bin/env python3
"""bybit-ws entry point — runs the main trading loop.

`python -m bybit_ws` == `python -m bybit_ws.main_async` (движок).
MCP-сервер для агентов — отдельная команда: `bybit-ws-mcp`.
"""
from bybit_ws.main_async import run

if __name__ == "__main__":
    run()
