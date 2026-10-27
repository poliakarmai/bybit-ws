"""Регресс: /rpc/risk берёт лимиты из YAML-конфига, а не из несуществующего DATA_DIR/config.json.

Баг (найден 27.10): `_handle_risk` читал `~/.local/share/bybit-ws/config.json`,
файла нет → дефолт 50 вместо конфижных 200, при этом живой risk_manager
блокирует входы по 200. Мониторинг врал.
"""


def test_risk_limits_match_config():
    from bybit_ws.config import Config
    from bybit_ws.rpc import _risk_limits

    cfg = Config().risk
    limits = _risk_limits()
    assert limits.get("max_daily_loss") == cfg.get("max_daily_loss")
    assert limits.get("max_total_margin") == cfg.get("max_total_margin")


def test_risk_limits_is_not_hardcoded_default():
    """Если конфиг задаёт max_daily_loss, RPC не должен отдавать дефолт 50."""
    from bybit_ws.config import Config
    from bybit_ws.rpc import _risk_limits

    configured = Config().risk.get("max_daily_loss")
    if configured != 50:
        assert _risk_limits().get("max_daily_loss") != 50
