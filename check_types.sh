#!/bin/bash
# Type-check the core data-contract modules (bybit_ws).
# Files/scope заданы в [tool.mypy] файлами pyproject.toml.
cd "$(dirname "$0")"
exec /usr/bin/python3 -m mypy --config-file pyproject.toml
