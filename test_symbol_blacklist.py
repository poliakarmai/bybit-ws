"""Тесты для symbol_blacklist.py — перманентный чёрный список символов.

Использует tmp_path (pytest fixture) для изоляции от реального DATA_DIR.
Покрывает:
- добавление → is_blacklisted True → remove → False (полный цикл)
- load отсутствующего файла → {}
- битый JSON → {} (не падает)
- reason и added_at сохраняются корректно
- два разных символа независимы
"""

import json
import time

import pytest

from bybit_ws.symbol_blacklist import (
    add_to_blacklist,
    is_blacklisted,
    list_blacklist,
    load_blacklist,
    remove_from_blacklist,
)


class TestBlacklistLifecycle:
    """Полный цикл: add → check → remove → check."""

    def test_add_then_remove(self, tmp_path):
        """Добавление → is_blacklisted True → remove → False."""
        fp = str(tmp_path / 'blacklist.json')
        sym = 'STGUSDT'

        assert is_blacklisted(sym, path=fp) is False

        add_to_blacklist(sym, reason='крупный SL', path=fp)
        assert is_blacklisted(sym, path=fp) is True

        remove_from_blacklist(sym, path=fp)
        assert is_blacklisted(sym, path=fp) is False

    def test_remove_nonexistent_is_idempotent(self, tmp_path):
        """remove_from_blacklist на несуществующем символе — тихо, без ошибок."""
        fp = str(tmp_path / 'blacklist.json')
        # Не должно бросать
        remove_from_blacklist('NOPEUSDT', path=fp)
        assert load_blacklist(path=fp) == {}


class TestLoadEdgeCases:
    """Граничные случаи load_blacklist."""

    def test_load_missing_file_returns_empty(self, tmp_path):
        """Файл не существует → {}."""
        fp = str(tmp_path / 'does_not_exist.json')
        assert load_blacklist(path=fp) == {}

    def test_load_broken_json_returns_empty(self, tmp_path):
        """Битый JSON → {} (не падает)."""
        fp = tmp_path / 'broken.json'
        fp.write_text('{это не валидный JSON,,,', encoding='utf-8')
        assert load_blacklist(path=str(fp)) == {}

    def test_load_empty_file_returns_empty(self, tmp_path):
        """Пустой файл → {} (json.JSONDecodeError → {})."""
        fp = tmp_path / 'empty.json'
        fp.write_text('', encoding='utf-8')
        assert load_blacklist(path=str(fp)) == {}


class TestBlacklistData:
    """Целостность и формат данных."""

    def test_reason_and_added_at_preserved(self, tmp_path):
        """reason и added_at сохраняются корректно."""
        fp = str(tmp_path / 'blacklist.json')
        sym = 'XRPUSDT'
        reason = '3 SL подряд за день'

        before = time.time()
        add_to_blacklist(sym, reason=reason, path=fp)
        after = time.time()

        data = load_blacklist(path=fp)
        assert sym in data
        assert data[sym]['reason'] == reason
        # added_at — float, в окне [before, after]
        added_at = data[sym]['added_at']
        assert isinstance(added_at, (int, float))
        assert before - 0.5 <= added_at <= after + 0.5

    def test_default_reason_empty_string(self, tmp_path):
        """reason по умолчанию — пустая строка."""
        fp = str(tmp_path / 'blacklist.json')
        add_to_blacklist('BTCUSDT', path=fp)
        data = load_blacklist(path=fp)
        assert data['BTCUSDT']['reason'] == ''

    def test_two_symbols_independent(self, tmp_path):
        """Два разных символа хранятся независимо."""
        fp = str(tmp_path / 'blacklist.json')

        add_to_blacklist('ETHUSDT', reason='A', path=fp)
        add_to_blacklist('SOLUSDT', reason='B', path=fp)

        data = load_blacklist(path=fp)
        assert set(data.keys()) == {'ETHUSDT', 'SOLUSDT'}
        assert data['ETHUSDT']['reason'] == 'A'
        assert data['SOLUSDT']['reason'] == 'B'

        # Удаляем один — второй остаётся
        remove_from_blacklist('ETHUSDT', path=fp)
        data = load_blacklist(path=fp)
        assert 'ETHUSDT' not in data
        assert 'SOLUSDT' in data

    def test_list_blacklist_is_synonym(self, tmp_path):
        """list_blacklist возвращает то же, что load_blacklist."""
        fp = str(tmp_path / 'blacklist.json')
        add_to_blacklist('ADAUSDT', reason='r', path=fp)

        assert list_blacklist(path=fp) == load_blacklist(path=fp)

    def test_add_overwrites_existing(self, tmp_path):
        """Повторное add перезаписывает reason и added_at."""
        fp = str(tmp_path / 'blacklist.json')
        sym = 'DOGEUSDT'

        add_to_blacklist(sym, reason='first', path=fp)
        first_ts = load_blacklist(path=fp)[sym]['added_at']

        # Небольшая пауза чтобы time.time() гарантированно отличался
        time.sleep(0.01)
        add_to_blacklist(sym, reason='second', path=fp)
        second = load_blacklist(path=fp)[sym]

        assert second['reason'] == 'second'
        assert second['added_at'] >= first_ts

    def test_unicode_reason_preserved(self, tmp_path):
        """Кириллица в reason сохраняется (ensure_ascii=False)."""
        fp = str(tmp_path / 'blacklist.json')
        reason = 'крупный убыток -$50, не входить'

        add_to_blacklist('STGUSDT', reason=reason, path=fp)

        # На диске — UTF-8 без ascii-escape
        raw = (tmp_path / 'blacklist.json').read_text(encoding='utf-8')
        assert 'крупный' in raw

        # И при чтении — теряем
        assert load_blacklist(path=fp)['STGUSDT']['reason'] == reason
