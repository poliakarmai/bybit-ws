"""test_async_blocking_gridsignal.py — страж: async def не должен блокировать loop.

Замерено 17.10.2026: 6 хендлеров вызывали `subprocess.run` (bybit CLI, timeout до 25с)
и один — синхронный `urllib.request.urlopen`, всё прямо в `async def`.
Пока такой вызов идёт, бот не отвечает никому (включая поллинг и health).

Правило: дорогое уходит в `asyncio.to_thread(...)`. SQLite (`conn.execute`) намеренно
не проверяем — суб-миллисекундные вызовы, шум.
"""
from __future__ import annotations

import ast
from pathlib import Path

BOT = Path(__file__).with_name("gridsignal-bot.py")

BLOCKING_CALLS = {
    ("subprocess", "run"), ("subprocess", "call"), ("subprocess", "check_output"),
    ("subprocess", "check_call"), ("subprocess", "Popen"),
    ("time", "sleep"), ("os", "system"),
    ("urllib.request", "urlopen"),
    ("requests", "get"), ("requests", "post"),
}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.hits: list[tuple[str, int, str]] = []

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return  # sync-хелперы запускаются в потоке — не блокируют loop

    def visit_Await(self, node: ast.Await) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        if self.stack:
            name = _dotted(node.func)
            parent, _, last = name.rpartition(".")
            if (parent, last) in BLOCKING_CALLS:
                self.hits.append((" -> ".join(self.stack), node.lineno, name))
        self.generic_visit(node)


def test_no_blocking_calls_inside_async_def() -> None:
    tree = ast.parse(BOT.read_text(encoding="utf-8"))
    finder = _Finder()
    finder.visit(tree)
    assert not finder.hits, (
        "блокирующий вызов внутри async def (оберни в asyncio.to_thread):\n"
        + "\n".join(f"  gridsignal-bot.py:{line} [{fn}] {call}" for fn, line, call in finder.hits)
    )
