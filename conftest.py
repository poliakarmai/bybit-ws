"""conftest.py — обход pre-existing бага корневого __init__.py для pytest.

Корневой __init__.py содержит relative imports (from .constants import ...)
и ломает pytest collection для всех тестов в корне репо.
Этот conftest подменяет _pytest.python.PyModule._getobj чтобы пропустить __init__.py.
"""

import types
import sys
from pathlib import Path


def pytest_configure(config):
    """Monkey-patch _pytest.python чтобы игнорировать корневой __init__.py."""
    root = Path(__file__).parent
    root_init = root / "__init__.py"

    if not root_init.exists():
        return

    # Подменяем importtestmodule чтобы пропускать корневой __init__.py
    import _pytest.python as _pp

    _orig_importtestmodule = _pp.importtestmodule

    def _patched_importtestmodule(path, config):
        if path == root_init:
            # Возвращаем пустой модуль вместо импорта сломанного __init__.py
            mod = types.ModuleType("__init__")
            mod.__file__ = str(path)
            mod.__path__ = [str(path.parent)]
            return mod
        return _orig_importtestmodule(path, config)

    _pp.importtestmodule = _patched_importtestmodule
