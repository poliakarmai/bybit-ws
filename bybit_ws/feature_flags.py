"""
Feature flags — единый источник правды (C9 fix).
Все модули импортируют флаги отсюда вместо os.environ.get.

Использование:
    from bybit_ws.feature_flags import FLAGS
    if FLAGS.ml_enabled:
        ...
    if FLAGS.ws_full_enabled:
        ...

Добавление нового флага:
    1. Добавить поле в FeatureFlags
    2. Все модули автоматически получат доступ
"""

import os
from dataclasses import dataclass, field


@dataclass
class FeatureFlags:
    """Все feature flags bybit-ws. Читаются из env один раз при старте."""

    # ── ML / DSPy ──
    ml_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_ML_ENABLED", "1") == "1"
    )
    dspy_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_DSPY_ENABLED", "0") == "1"
    )
    optuna_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_OPTUNA_ENABLED", "0") == "1"
    )

    # ── WebSocket ──
    ws_full_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_WS_FULL_ENABLED", "0") == "1"
    )
    ws_bb_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_WS_BB_ENABLED", "1") == "1"
    )

    # ── Trading ──
    ab_enabled: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_AB_ENABLED", "0") == "1"
    )
    regime_auto: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_REGIME_AUTO", "0") == "1"
    )

    # ── Notifications ──
    push_enabled: bool = field(
        default_factory=lambda: os.environ.get("PUSH_ENABLED", "1") == "1"
    )

    # ── Production guard ──
    production: bool = field(
        default_factory=lambda: os.environ.get("BYBIT_WS_PRODUCTION", "0") == "1"
    )

    # ── Exchange ──
    exchange: str = field(
        default_factory=lambda: os.environ.get("BYBIT_EXCHANGE", "bybit").lower()
    )

    # ── Paths (read-only, from env) ──
    data_dir: str = field(
        default_factory=lambda: os.environ.get(
            "BYBIT_DATA_DIR", os.path.expanduser("~/.local/share/bybit-ws")
        )
    )

    def as_dict(self) -> dict:
        """Для отладки / RPC /metrics."""
        return {
            "ml_enabled": self.ml_enabled,
            "dspy_enabled": self.dspy_enabled,
            "optuna_enabled": self.optuna_enabled,
            "ws_full_enabled": self.ws_full_enabled,
            "ws_bb_enabled": self.ws_bb_enabled,
            "ab_enabled": self.ab_enabled,
            "regime_auto": self.regime_auto,
            "push_enabled": self.push_enabled,
            "production": self.production,
            "exchange": self.exchange,
            "data_dir": self.data_dir,
        }


# Единственный экземпляр — импортируется всеми модулями
FLAGS = FeatureFlags()
