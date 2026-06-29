#!/usr/bin/env python3
"""Scan for double-tuple unwrapping bugs: run_in_thread result used incorrectly."""

import ast
from pathlib import Path

def get_func_returns(filepath, func_name):
    """Parse a .py file and check if a function returns a tuple."""
    try:
        tree = ast.parse(Path(filepath).read_text())
    except SyntaxError:
        return 'syntax_error'
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value:
                    if isinstance(child.value, ast.Tuple):
                        return 'tuple'
                    if isinstance(child.value, ast.Name):
                        return 'var:' + child.value.id
                    if isinstance(child.value, ast.List):
                        return 'list'
                    if isinstance(child.value, ast.Dict):
                        return 'dict'
                    if isinstance(child.value, ast.Constant):
                        return f'const:{child.value.value}'
            return 'no_return_found'
    return 'def_not_found'


def main():
    main_path = Path('main_async.py')
    if not main_path.exists():
        main_path = Path('bybit_ws/main_async.py')
    
    tree = ast.parse(main_path.read_text())
    
    issues = []
    safe = []
    
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.targets[0], ast.Tuple):
            continue
        if len(node.targets[0].elts) < 2:
            continue
        
        val = node.value
        if not isinstance(val, ast.Await):
            continue
        if not isinstance(val.value, ast.Call):
            continue
        call = val.value
        if not isinstance(call.func, ast.Name):
            continue
        if call.func.id != 'run_in_thread':
            continue
        
        if not call.args:
            continue
        
        arg0 = call.args[0]
        if not isinstance(arg0, ast.Name):
            continue
        
        func_name = arg0.id
        
        # Search for this function's return type
        ret = 'not_found'
        for f in Path('.').rglob('*.py'):
            if '__pycache__' in str(f) or '.venv' in str(f):
                continue
            r = get_func_returns(str(f), func_name)
            if r != 'def_not_found':
                ret = r
                break
        
        targets = ', '.join(ast.unparse(t) for t in node.targets[0].elts)
        entry = {
            'line': node.lineno,
            'targets': targets,
            'func': func_name,
            'returns': ret,
        }
        
        if ret == 'tuple':
            issues.append(entry)
        else:
            safe.append(entry)
    
    print("=== SAFE (функция возвращает list/dict/const) ===")
    for s in safe:
        print(f"  🟢 L{s['line']}: {s['targets']} = run_in_thread({s['func']}) → {s['returns']}")
    
    # ── Доп. проверка: isinstance guard для for-циклов после run_in_thread ──
    guard_issues = []
    source = main_path.read_text().split('\n')
    for s in safe:
        line_no = s['line']
        # Проверить следующие 2 строки на наличие isinstance guard
        for offset in range(1, 4):
            check_line = source[line_no + offset - 1] if line_no + offset <= len(source) else ''
            if 'for ' in check_line and s['targets'].split(',')[0].strip() in check_line:
                if 'or []' in check_line and 'isinstance' not in source[line_no - 1:line_no + 5].__repr__():
                    guard_issues.append(f"  🔴 L{line_no}+{offset}: '{check_line.strip()[:80]}' — нет isinstance guard! Замени (xxx or []) на isinstance(xxx, list)")
                break
    
    if guard_issues:
        print(f"\n=== GUARD ISSUES (for-цикл без isinstance после run_in_thread) ===")
        for gi in guard_issues:
            print(gi)
    
    print(f"\n=== BUGS (функция возвращает tuple — двойная распаковка!) ===")
    if issues:
        for i in issues:
            print(f"  🔴 L{i['line']}: {i['targets']} = run_in_thread({i['func']}) → returns TUPLE")
    else:
        print("  ✅ Нет проблем!")
    
    return len(issues) + len(guard_issues)


if __name__ == '__main__':
    exit(main())
