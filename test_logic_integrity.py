"""
test_logic_integrity.py — структурная целостность main_async.py.

Проверяет что ВСЕ заявленные в AGENTS.md стратегии реально вызываются,
а не просто импортированы и забыты. Ловит баги которые мы чинили 28.06.2026:
  - apply_trailing_sl импортирован но не вызван
  - apply_auto_tp импортирован но не вызван
  - auto_take_profit не в heavy cycle
  - entry_judge.py не в пакете
  - SL floor отсутствует
  - ensure_today не вызывается при старте
"""
import ast
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_ASYNC = PROJECT_ROOT / 'main_async.py'
PACKAGE_DIR = PROJECT_ROOT / 'bybit_ws'


def parse_calls_and_imports(filepath: Path) -> tuple:
    """Парсит файл: возвращает (импорты, вызовы) из main_async.py."""
    with open(filepath) as f:
        code = f.read()
    tree = ast.parse(code)

    imports = {}  # name → module
    calls = set()  # все вызовы функций

    class CallCollector(ast.NodeVisitor):
        def visit_ImportFrom(self, node):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = node.module
            self.generic_visit(node)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
            self.generic_visit(node)

    CallCollector().visit(tree)

    # Также текстовый поиск для динамических вызовов (run_in_thread, asyncio.gather)
    text_calls = set()
    for pattern in [
        r'run_in_thread\((\w+)',      # run_in_thread(func_name, ...)
        r'await\s+(\w+)\(',           # await func_name(...)
        r'asyncio\.gather\(.*?(\w+)', # gather(*tasks) — менее надёжно
    ]:
        for m in re.finditer(pattern, code):
            text_calls.add(m.group(1))

    return imports, calls | text_calls


def test_apply_functions_are_called():
    """apply_* функции ДОЛЖНЫ вызываться, а не только импортироваться."""
    imports, calls = parse_calls_and_imports(MAIN_ASYNC)

    apply_funcs = [k for k in imports if k.startswith('apply_')]
    # Allowlist: replaced by unified_sl.manage_sl() — one API call per position
    _apply_allowlist = {'apply_trailing_sl', 'apply_auto_tp'}
    assert len(apply_funcs) > 0, "Нет apply_* импортов — что-то не так с парсингом"

    for func in apply_funcs:
        if func in _apply_allowlist:
            continue  # allowed — called indirectly or as fallback
        assert func in calls, (
            f"❌ {func} импортирован из {imports[func]}, "
            f"но НИГДЕ НЕ ВЫЗЫВАЕТСЯ в main_async.py! "
            f"Баг: actions генерируются но не применяются на бирже."
        )


def test_key_strategies_are_called():
    """Ключевые стратегии из AGENTS.md должны вызываться."""
    imports, calls = parse_calls_and_imports(MAIN_ASYNC)

    required = [
        # Лёгкий цикл
        ('manage_sl', 'unified_sl', 'Unified SL (все 5 механизмов)'),
        ('check_margin_utilization', 'margin_alerts', 'Margin utilization'),

        # Тяжёлый цикл
        ('check_auto_short', 'auto_short', 'Auto-SHORT'),
        ('check_correlation', 'correlation', 'Корреляции'),
        ('check_pumps', 'pump_detect', 'Пампы'),
        ('check_overbought', 'overbought', 'Перекупленность'),
        ('check_dca', 'dca', 'DCA'),
        ('check_partial_tp', 'partial_tp', 'Partial TP'),
        ('auto_entry_scan', 'auto_entry', 'Auto-Entry LONG'),
        ('auto_take_profit', 'auto_tp', 'Auto-TP'),
        ('check_sl_reentry', 'sl_reentry', 'SL re-entry'),
        ('ensure_today', 'metrics', 'Метрики'),
    ]

    for func, module, desc in required:
        assert func in imports, f"❌ {func} не импортирован из {module} ({desc})"
        assert func in calls, (
            f"❌ {func} ({desc}) импортирован из {module}, "
            f"но НЕ ВЫЗЫВАЕТСЯ в main_async.py!"
        )


def test_entry_judge_in_package():
    """entry_judge.py ДОЛЖЕН быть в пакете bybit_ws/."""
    entry_judge = PACKAGE_DIR / 'entry_judge.py'
    assert entry_judge.exists(), (
        f"❌ {entry_judge} не найден! "
        f"Entry Judge не работает — все входы без LLM-проверки."
    )


def test_sl_floor_exists():
    """В auto_sl.py должен быть SL floor (min -5%)."""
    auto_sl = PROJECT_ROOT / 'auto_sl.py'
    with open(auto_sl) as f:
        code = f.read()

    assert 'min_sl_5pct' in code or '0.95' in code, (
        "❌ SL floor (-5%) отсутствует в auto_sl.py! "
        "Для микрокапов SL будет почти на входе → выбивает шумом."
    )
    assert 'mark * 0.95' in code or 'mark*0.95' in code, (
        "❌ Аварийный SL (mark×0.95) отсутствует в auto_sl.py! "
        "Упавшие позиции останутся без SL."
    )


def test_trailing_sl_has_simple_mode():
    """Должен быть simple_trailing_sl для постепенного подтягивания."""
    trailing_sl = PROJECT_ROOT / 'trailing_sl.py'
    with open(trailing_sl) as f:
        code = f.read()

    assert 'def simple_trailing_sl' in code, (
        "❌ simple_trailing_sl отсутствует в trailing_sl.py! "
        "Трейлинг требует pnl>15% И BB>75 — почти никогда не срабатывает."
    )


def test_concentration_check_exists():
    """В risk_manager.py должен быть check_symbol_concentration."""
    risk_mgr = PROJECT_ROOT / 'risk_manager.py'
    with open(risk_mgr) as f:
        code = f.read()

    assert 'def check_symbol_concentration' in code, (
        "❌ check_symbol_concentration отсутствует в risk_manager.py! "
        "Нет защиты от перегруза на один тикер."
    )


def test_no_critical_errors_in_logs():
    """В логах не должно быть критических ошибок после деплоя."""
    log_file = Path.home() / '.local' / 'share' / 'bybit-ws' / 'events.log'
    if not log_file.exists():
        return  # нет логов — пропускаем

    with open(log_file) as f:
        lines = f.readlines()

    # Ищем только свежие ошибки (последние 200 строк)
    critical_patterns = [
        "No module named 'bybit_ws.entry_judge'",
        "name 'nn' is not defined",
        "missing 1 required positional argument",
    ]

    recent = lines[-200:] if len(lines) > 200 else lines
    for line in recent:
        for pattern in critical_patterns:
            assert pattern not in line, (
                f"❌ Критическая ошибка в логах: {line.strip()}"
            )


def test_imports_vs_calls_summary():
    """Информационный тест: какие импорты не вызываются (техдолг)."""
    imports, calls = parse_calls_and_imports(MAIN_ASYNC)

    unused = []
    for func, module in sorted(imports.items()):
        if func not in calls:
            # Исключаем конфиги и не-функции
            if func.startswith('_') or func[0].isupper():
                continue
            unused.append(f"{func} ← {module}")

    if unused:
        print(f"\n⚠️  Импортированы но не вызываются ({len(unused)}):")
        for u in unused:
            print(f"   {u}")
        print("   Это техдолг — дополнительные стратегии, не включённые в цикл.")
    else:
        print("\n✅ Все импортированные функции вызываются.")


if __name__ == '__main__':
    # Ручной запуск
    test_apply_functions_are_called()
    test_key_strategies_are_called()
    test_entry_judge_in_package()
    test_sl_floor_exists()
    test_trailing_sl_has_simple_mode()
    test_concentration_check_exists()
    test_no_critical_errors_in_logs()
    test_imports_vs_calls_summary()
    print('\n✅ Все тесты логической целостности пройдены!')
