"""Один авто-вход = один алерт.

Регрессия 18.11.2026: сделка SUIUSDT ушла в чат дважды —
`auto_entry.py` слал `🚀 LONG` через add_alert (ДО risk_check в main_async),
а main_async слал строку `🤖 Авто-вход` из entries (ПОСЛЕ risk_check).
Дедуп не ловил: разные тексты → разные хеши.

Контракт:
- auto_entry.py НЕ шлёт add_alert('ENTRY', '🚀 LONG ...') на успешном пути
  (алерт — зона main_async, после risk-гейта; иначе алерт врёт о заблокированном входе).
- main_async.py по-прежнему шлёт entries: add_alert + send_telegram_alert.
- auto_short.py НЕ тронут: его add_alert — единственная точка (main_async
  игнорирует возвращаемые actions).

Запуск: python3 -m pytest tests/test_entry_alert_no_dup.py -q
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_auto_entry_has_no_pre_risk_long_alert():
    src = (ROOT / 'auto_entry.py').read_text(encoding='utf-8')
    assert '🚀 LONG' not in src, (
        'auto_entry снова шлёт 🚀 LONG до risk_check — вернётся дубль алерта '
        '(инцидент 18.11.2026, SUIUSDT). Точка алерта входа — main_async.'
    )


def test_main_async_still_sends_entry_alert():
    src = (ROOT / 'main_async.py').read_text(encoding='utf-8')
    # for entry in (entries[0] or []): ... add_alert('ENTRY', entry) + send_telegram_alert(entry)
    block = re.search(
        r"for entry in \(entries\[0\] or \[\]\):.*?record_alert\('ENTRY', symbol=sym\)",
        src, re.S)
    assert block, 'main_async: цикл отправки entries пропал'
    body = block.group(0)
    assert "add_alert('ENTRY', entry)" in body, 'main_async: add_alert по entry пропал'
    assert 'send_telegram_alert(entry)' in body, 'main_async: telegram-отправка entry пропала'
    assert 'risk_check(' in body, 'main_async: risk_check перед алертом пропал'


def test_auto_short_alert_untouched():
    # auto_short — единственная точка алерта (main_async игнорирует actions):
    # не удалять add_alert('ENTRY', msg) там.
    src = (ROOT / 'auto_short.py').read_text(encoding='utf-8')
    assert src.count("add_alert('ENTRY', msg)") >= 2, (
        "auto_short: add_alert('ENTRY', msg) удалён — шорты станут немыми "
        '(main_async не шлёт алерты по результатам check_auto_short)'
    )
