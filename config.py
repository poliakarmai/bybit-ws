"""
Config module for bybit-ws — reads YAML configuration with ${ENV_VAR} substitution.

Configuration path: ~/.config/bybit-ws/config.yaml
If no config exists, creates config.example.yaml with defaults.
"""

import os
import re
from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / '.config' / 'bybit-ws'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'
EXAMPLE_CONFIG_PATH = CONFIG_DIR / 'config.example.yaml'

# ── Resolved config (singleton) ──────────────────────────────────────────────
_cfg = None


def _env_subst(obj):
    """Recursively substitute ${ENV_VAR} patterns in strings."""
    if isinstance(obj, str):
        # Replace ${VAR} or ${VAR:-default}
        def _replace(m):
            expr = m.group(1)
            if ':-' in expr:
                var, default = expr.split(':-', 1)
                return os.environ.get(var.strip(), default.strip())
            else:
                return os.environ.get(expr, m.group(0))
        return re.sub(r'\$\{([^}]+)\}', _replace, obj)
    elif isinstance(obj, dict):
        return {k: _env_subst(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_env_subst(v) for v in obj]
    return obj


# ── Default configuration ────────────────────────────────────────────────────

_DEFAULT_STRATEGY_LONG = {
    'leverage': 3,
    'margin_tiers': {7: 15, 5.5: 10, 0: 5},
    'entry_offset': 0.03,
    'sl_offset': 0.07,
    'tp_middle_pct': 0.20,
    'tp_upper_pct': 0.80,
    'max_positions': 12,# безопасный дефолт (было 0=unlimited)
    'cooldown_after_sl': 14400,    # 4 часа после SL перед повторным входом
    'cooldown_after_tp': 3600,     # 1 час после TP
}

_DEFAULT_STRATEGY_SHORT = {
    'leverage': 3,
    'margin': 10,
    'entry_offset': 0.02,
    'sl_tier_ab': 0.10,   # +10% SL для SHORT Tier A/B (23.06.2026: +5% → +10%)
    'sl_tier_cd': 0.07,
    'bb_threshold': 85,
    'max_positions': 3,
    'cooldown_seconds': 7200,
    'max_short_pct': 20,       # макс % шортов от всех позиций
    'max_hold_hours': 72,      # авто-закрытие SHORT через 72ч если не сработал TP/SL
    'instant_tp_symbols': [],  # закрыть при любом профите (список символов)
    'junk_daily_pump_threshold': 0.80,   # мин дневной рост для шлак-шорта (80%)
    'junk_dca_levels': [1.0, 1.2],       # DCA-лесенка: +100% и +120% от входа
}

_DEFAULT_STRATEGY_ML = {
    'rf_enabled': True,
    'rf_threshold': 0.22,        # ML Gate порог (F1=0.921)
    'rf_weight': 0.5,            # вес RF в комбинированном скоре
    'dspy_enabled': False,       # DSPy-оптимизация (Фаза 5.1)
    'dspy_weight': 0.5,          # вес DSPy в комбинированном скоре
    'dspy_threshold': 50.0,      # порог DSPy-гейта (score ≥ 50 → ENTER)
    'dspy_model': 'openai/gpt-4o-mini',  # LLM для DSPy
}

_DEFAULT_STRATEGY_DCA = {
    'enabled': True,
    'levels': [0.95, 0.90, 0.85],
    'multiplier': 2,
    'max_margin_per_symbol': 80,   # не более $80 суммарной маржи на одну монету
    'max_dca_count': 2,            # максимум 2 DCA-добавки (не 3)
}

_DEFAULT_STRATEGY_X10 = {
    'max_daily_losses': 3,         # стоп x10 после N убыточных сделок за день
    'cooldown_after_stop_hours': 24,  # пауза всех x10 после стопа
    'require_atr_validation': True,   # обязательная ATR-проверка
    'max_position_risk_pct': 2.0,     # макс риск на позицию (% от баланса)
}

_DEFAULT_STRATEGY_JUNK = {
    'enabled': False,                  # выключен по умолчанию
    'min_pump_pct': 80,               # вход при росте ≥80%
    'dca_levels': [1.0, 1.2],         # +100%, +120%
    'max_loss_pct': 15,               # hard stop: -15% убытка по марже
    'max_hold_hours': 48,             # авто-закрытие шлак-шорта через 48ч
    'max_positions': 2,               # не более 2 шлак-шортов
}

_DEFAULT_WATCHLIST = {
    'mode': 'top',
    'top_n': 50,
    'exclude': ['BTCUSDT', 'ETHUSDT'],
}

_DEFAULT_TIERS = {
    'S': ['BTCUSDT', 'ETHUSDT'],
    'A': [
        'SOLUSDT', 'LTCUSDT', 'XRPUSDT', 'ADAUSDT', 'DOTUSDT', 'LINKUSDT',
        'UNIUSDT', 'AVAXUSDT', 'SUIUSDT', 'NEARUSDT', 'APTUSDT',
    ],
    'B': [
        'ARBUSDT', 'OPUSDT', 'AAVEUSDT', 'INJUSDT', 'ONDOUSDT',
        'ENAUSDT', 'FETUSDT', 'WLDUSDT', 'ATOMUSDT', 'ALGOUSDT', 'RUNEUSDT',
    ],
    'one_way': [
        'XRPUSDT', 'ONDOUSDT', 'WLFIUSDT', 'ENJUSDT', 'ESPORTSUSDT',
        'AVAXUSDT', 'APTUSDT', 'SUIUSDT',
    ],
}

_DEFAULT_MONITOR = {
    'cycle_seconds': 30,
    'heavy_cycle': 10,
    'watchdog_seconds': 180,
}

_DEFAULT_RPC = {
    'port': 8766,
    'bind': '127.0.0.1',      # default to localhost; set 0.0.0.0 for external
    'auth_token': '${RPC_TOKEN}',  # Bearer token for RPC auth
    'rate_limit_per_min': 60,  # max requests per minute per IP
}

_DEFAULT_WEBSOCKET = {
    'enabled': True,            # WebSocket live prices/BB (Фаза 4)
}

_DEFAULT_RISK = {
    'max_drawdown_pct': 15,       # global stop: -15% of deposit → close all
    'max_total_margin': 300,      # max $300 total in positions (fallback, если dynamic_margin_pct=0)
    'dynamic_margin_pct': 30,     # % от available balance для авто-расчёта max_total_margin (0=выкл)
    'max_position_size': 100,     # max $100 in single position (10% of deposit)
    'max_daily_loss': 50,         # stop for the day at -$50
    'max_long_positions': 12,      # limit LONG entries
    'emergency_close_all': True,   # close all positions on max_drawdown
    'drawdown_mode': 'peak',      # 'peak' (от пикового баланса) или 'start' (от начального)
    'drawdown_reset_hours': 24,   # авто-сброс паузы через N часов после emergency
    'max_per_sector': 3,           # не более 3 позиций в одном секторе (L1/DeFi/AI/Meme)
    'banned_symbols': [],          # символы в перманентном бане (не торгуются НИКОГДА)
    'sectors': {
        'L1': ['SOLUSDT', 'SUIUSDT', 'APTUSDT', 'NEARUSDT', 'AVAXUSDT', 'ADAUSDT', 'DOTUSDT'],
        'DeFi': ['AAVEUSDT', 'UNIUSDT', 'INJUSDT', 'RUNEUSDT'],
        'AI': ['FETUSDT', 'WLDUSDT'],
        'Meme': ['DOGEUSDT'],
    },
}

_DEFAULT_LOGGING = {
    'max_size_mb': 50,
    'max_files': 7,
    'format': 'json',            # 'json' or 'text'
    'trades_max_size_mb': 100,   # ротация trades.jsonl при 100 МБ
    'trades_archive': True,       # архивировать старые в .gz
}

_DEFAULT_ALERTS = {
    'telegram_enabled': False,
    'correlation_threshold': 0.80,
    'sl_alert': True,
    'tp_alert': True,
    'push': {
        'enabled': True,               # PUSH_ENABLED env или этот флаг
        'ntfy_server': 'https://ntfy.sh',  # NTFY_SERVER env
        'ntfy_topic': '',              # NTFY_TOPIC env — обязательно для ntfy
        'telegram_fallback': True,     # слать в Telegram если ntfy недоступен
        'dedup_ttl': 300,              # 5 мин — не слать одинаковый алерт чаще
    },
}

_DEFAULT_POSITION_SIZING = {
    'enabled': True,
    'long_risk_pct': 0.20,
    'x10_risk_pct': 0.05,
    'dca_risk_pct': 0.10,
    'pump_risk_pct': 0.06,
    'max_positions': 5,
    'min_margin': 5.0,
    'max_position_share': 0.40,
    'min_deposit': 30.0,
    'score_multipliers': {8.5: 1.4, 7.5: 1.15, 6.5: 1.0, 5.5: 0.75},
}

_DEFAULT_API = {
    'key': '${BYBIT_API_KEY}',
    'secret': '${BYBIT_API_SECRET}',
    'base_url': 'https://api.bytick.com',
    'retry_count': 3,
    'retry_backoff': [1, 3, 10],   # seconds
    'timeout': 30,
}


def _default_config() -> dict:
    """Return the full default configuration dict."""
    return {
        'api': dict(_DEFAULT_API),
        'strategy': {
            'long': dict(_DEFAULT_STRATEGY_LONG),
            'short': dict(_DEFAULT_STRATEGY_SHORT),
            'dca': dict(_DEFAULT_STRATEGY_DCA),
            'x10': dict(_DEFAULT_STRATEGY_X10),
            'junk': dict(_DEFAULT_STRATEGY_JUNK),
            'ml': dict(_DEFAULT_STRATEGY_ML),
        },
        'watchlist': dict(_DEFAULT_WATCHLIST),
        'tiers': dict(_DEFAULT_TIERS),
        'monitor': dict(_DEFAULT_MONITOR),
        'rpc': dict(_DEFAULT_RPC),
        'websocket': dict(_DEFAULT_WEBSOCKET),
        'risk': dict(_DEFAULT_RISK),
        'logging': dict(_DEFAULT_LOGGING),
        'alerts': dict(_DEFAULT_ALERTS),
        'position_sizing': dict(_DEFAULT_POSITION_SIZING),
    }


def _generate_example() -> str:
    """Generate example YAML as a string with comments."""
    # Use pyyaml to dump, then we'll add comments manually
    # For simplicity, write the example from a static template

    #  Build tier A list
    tier_a_str = '\n  '.join(f'- "{s}"' for s in _DEFAULT_TIERS['A'])
    tier_b_str = '\n  '.join(f'- "{s}"' for s in _DEFAULT_TIERS['B'])
    one_way_str = '\n  '.join(f'- "{s}"' for s in _DEFAULT_TIERS['one_way'])

    return f"""# bybit-ws configuration
# Path: ~/.config/bybit-ws/config.yaml
# Copy this file to config.yaml and fill in your values.
#
# Environment variables (${{VAR}}) are substituted at load time.
# Use ${{VAR:-default}} for fallback values.

api:
  key: "${{BYBIT_API_KEY}}"
  secret: "${{BYBIT_API_SECRET}}"
  base_url: "https://api.bytick.com"

strategy:
  long:
    leverage: {_DEFAULT_STRATEGY_LONG['leverage']}
    margin_tiers:
      7: {_DEFAULT_STRATEGY_LONG['margin_tiers'][7]}     # score >= 7 → $margin
      5.5: {_DEFAULT_STRATEGY_LONG['margin_tiers'][5.5]}  # score >= 5.5
      0: {_DEFAULT_STRATEGY_LONG['margin_tiers'][0]}      # score < 5.5
    entry_offset: {_DEFAULT_STRATEGY_LONG['entry_offset']}       # -3% below Lower BB
    sl_offset: {_DEFAULT_STRATEGY_LONG['sl_offset']}          # -7% from Lower BB
    tp_middle_pct: {_DEFAULT_STRATEGY_LONG['tp_middle_pct']}     # 20% on Middle
    tp_upper_pct: {_DEFAULT_STRATEGY_LONG['tp_upper_pct']}      # 80% on Upper
    max_positions: {_DEFAULT_STRATEGY_LONG['max_positions']}       # безопасный дефолт (15)
    cooldown_after_sl: {_DEFAULT_STRATEGY_LONG['cooldown_after_sl']}  # 4ч после SL перед повторным входом
    cooldown_after_tp: {_DEFAULT_STRATEGY_LONG['cooldown_after_tp']}   # 1ч после TP

  short:
    leverage: {_DEFAULT_STRATEGY_SHORT['leverage']}
    margin: {_DEFAULT_STRATEGY_SHORT['margin']}
    entry_offset: {_DEFAULT_STRATEGY_SHORT['entry_offset']}       # +2% above market
    sl_tier_ab: {_DEFAULT_STRATEGY_SHORT['sl_tier_ab']}         # +5% SL for Tier A/B
    sl_tier_cd: {_DEFAULT_STRATEGY_SHORT['sl_tier_cd']}         # +7% SL for Tier C/D
    bb_threshold: {_DEFAULT_STRATEGY_SHORT['bb_threshold']}         # BB% > threshold triggers SHORT
    max_positions: {_DEFAULT_STRATEGY_SHORT['max_positions']}
    cooldown_seconds: {_DEFAULT_STRATEGY_SHORT['cooldown_seconds']}
    max_hold_hours: {_DEFAULT_STRATEGY_SHORT['max_hold_hours']}        # авто-закрытие SHORT через 72ч

  dca:
    enabled: {str(_DEFAULT_STRATEGY_DCA['enabled']).lower()}
    levels: {_DEFAULT_STRATEGY_DCA['levels']}
    multiplier: {_DEFAULT_STRATEGY_DCA['multiplier']}
    max_margin_per_symbol: {_DEFAULT_STRATEGY_DCA['max_margin_per_symbol']}   # не более $80 на монету
    max_dca_count: {_DEFAULT_STRATEGY_DCA['max_dca_count']}            # максимум 2 добавки

  x10:
    max_daily_losses: {_DEFAULT_STRATEGY_X10['max_daily_losses']}         # стоп x10 после N убыточных сделок
    cooldown_after_stop_hours: {_DEFAULT_STRATEGY_X10['cooldown_after_stop_hours']}  # пауза на сутки
    require_atr_validation: {str(_DEFAULT_STRATEGY_X10['require_atr_validation']).lower()}
    max_position_risk_pct: {_DEFAULT_STRATEGY_X10['max_position_risk_pct']}

  junk:
    enabled: {str(_DEFAULT_STRATEGY_JUNK['enabled']).lower()}
    min_pump_pct: {_DEFAULT_STRATEGY_JUNK['min_pump_pct']}
    dca_levels: {_DEFAULT_STRATEGY_JUNK['dca_levels']}
    max_loss_pct: {_DEFAULT_STRATEGY_JUNK['max_loss_pct']}
    max_hold_hours: {_DEFAULT_STRATEGY_JUNK['max_hold_hours']}
    max_positions: {_DEFAULT_STRATEGY_JUNK['max_positions']}

position_sizing:
  enabled: {str(_DEFAULT_POSITION_SIZING['enabled']).lower()}
  long_risk_pct: {_DEFAULT_POSITION_SIZING['long_risk_pct']}
  x10_risk_pct: {_DEFAULT_POSITION_SIZING['x10_risk_pct']}
  dca_risk_pct: {_DEFAULT_POSITION_SIZING['dca_risk_pct']}
  pump_risk_pct: {_DEFAULT_POSITION_SIZING['pump_risk_pct']}
  max_positions: {_DEFAULT_POSITION_SIZING['max_positions']}
  min_margin: {_DEFAULT_POSITION_SIZING['min_margin']}
  max_position_share: {_DEFAULT_POSITION_SIZING['max_position_share']}
  min_deposit: {_DEFAULT_POSITION_SIZING['min_deposit']}
  score_multipliers:
    8.5: 1.4
    7.5: 1.15
    6.5: 1.0
    5.5: 0.75

watchlist:
  mode: "{_DEFAULT_WATCHLIST['mode']}"           # top | fixed
  top_n: {_DEFAULT_WATCHLIST['top_n']}
  exclude: {_DEFAULT_WATCHLIST['exclude']}
  # fixed: ["SOLUSDT", "ADAUSDT"]   # if mode=fixed

tiers:
  S: {_DEFAULT_TIERS['S']}
  A:
  {tier_a_str}
  B:
  {tier_b_str}
  one_way:
  {one_way_str}

monitor:
  cycle_seconds: {_DEFAULT_MONITOR['cycle_seconds']}
  heavy_cycle: {_DEFAULT_MONITOR['heavy_cycle']}           # heavy checks every N cycles
  watchdog_seconds: {_DEFAULT_MONITOR['watchdog_seconds']}

rpc:
  port: {_DEFAULT_RPC['port']}
  bind: "{_DEFAULT_RPC['bind']}"
  auth_token: "${{RPC_TOKEN}}"     # Bearer token (empty = no auth)
  rate_limit_per_min: {_DEFAULT_RPC['rate_limit_per_min']}

risk:
  max_drawdown_pct: {_DEFAULT_RISK['max_drawdown_pct']}       # -15% от пикового баланса → закрыть всё
  max_total_margin: {_DEFAULT_RISK['max_total_margin']}        # не более $300 суммарно в позициях (30% депозита)
  max_position_size: {_DEFAULT_RISK['max_position_size']}       # не более $100 в одну позицию (10% депозита)
  max_daily_loss: {_DEFAULT_RISK['max_daily_loss']}            # стоп на день при -$50
  max_long_positions: {_DEFAULT_RISK['max_long_positions']}     # лимит LONG
  emergency_close_all: {str(_DEFAULT_RISK['emergency_close_all']).lower()}
  drawdown_mode: "{_DEFAULT_RISK['drawdown_mode']}"            # peak (от пика) или start (от начального)
  drawdown_reset_hours: {_DEFAULT_RISK['drawdown_reset_hours']}   # авто-сброс паузы через 24ч
  max_per_sector: {_DEFAULT_RISK['max_per_sector']}              # не более N позиций в одном секторе
  banned_symbols: {_DEFAULT_RISK['banned_symbols']}          # перманентный бан (например, ['BLESSUSDT'])

logging:
  max_size_mb: {_DEFAULT_LOGGING['max_size_mb']}
  max_files: {_DEFAULT_LOGGING['max_files']}
  format: "{_DEFAULT_LOGGING['format']}"
  trades_max_size_mb: {_DEFAULT_LOGGING['trades_max_size_mb']}    # ротация trades.jsonl
  trades_archive: {str(_DEFAULT_LOGGING['trades_archive']).lower()}

alerts:
  telegram_enabled: {str(_DEFAULT_ALERTS['telegram_enabled']).lower()}
  correlation_threshold: {_DEFAULT_ALERTS['correlation_threshold']}
  sl_alert: {str(_DEFAULT_ALERTS['sl_alert']).lower()}
  tp_alert: {str(_DEFAULT_ALERTS['tp_alert']).lower()}
  push:
    enabled: true                  # ntfy push-уведомления (Фаза 6.4)
    ntfy_server: "https://ntfy.sh" # или self-hosted
    ntfy_topic: ""                 # имя топика (задать или ${NTFY_TOPIC})
    telegram_fallback: true        # слать в Telegram если ntfy недоступен
    dedup_ttl: 300                 # 5 мин — не слать одинаковый алерт чаще
"""


# ── Load / initialise ────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load YAML config from ~/.config/bybit-ws/config.yaml.

    If config.yaml doesn't exist, writes config.example.yaml and returns defaults.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        # Write example config
        example_content = _generate_example()
        with open(EXAMPLE_CONFIG_PATH, 'w') as f:
            f.write(example_content)
        print(f'📝 Example config written to {EXAMPLE_CONFIG_PATH}')
        print(f'   Copy it to {CONFIG_PATH} and fill in your API keys.')
        return _default_config()

    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f) or {}

    # Deep-merge with defaults (user config overrides defaults)
    merged = _deep_merge(_default_config(), raw)
    # Apply env-var substitution
    merged = _env_subst(merged)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflicts."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_config() -> dict:
    """Return the loaded config dict (singleton)."""
    global _cfg
    if _cfg is None:
        _cfg = _load_config()
    return _cfg


def reload_config() -> dict:
    """Force reload the config from disk."""
    global _cfg
    _cfg = None
    return get_config()


# ── Convenience accessors ────────────────────────────────────────────────────

class _ConfigProxy:
    """Attribute-access proxy for nested dicts."""

    def __init__(self, data: dict):
        object.__setattr__(self, '_data', data)

    def __getattr__(self, name):
        data = object.__getattribute__(self, '_data')
        if name in data:
            value = data[name]
            if isinstance(value, dict):
                return _ConfigProxy(value)
            return value
        raise AttributeError(f'No config key: {name}')

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __repr__(self):
        return repr(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()


def Config() -> _ConfigProxy:
    """Return the config as an attribute-accessible proxy.

    Usage:
        cfg = Config()
        cfg.strategy.short.leverage     → 3
        cfg.monitor.cycle_seconds       → 30
        cfg.tiers.S                     → ['BTCUSDT', 'ETHUSDT']
        cfg.tiers.one_way              → ['XRPUSDT', ...]
    """
    return _ConfigProxy(get_config())


# ── Module-level convenience (lazy) ──────────────────────────────────────────

# Deprecated: use Config() instead for explicit access.
# These exist so existing code can `from .config import config` without changes.
# But prefer `from .config import Config; cfg = Config()`.

def _lazy_config():
    return _ConfigProxy(get_config())


# Make `config` a module-level attribute that lazily resolves
import sys as _sys
_module = _sys.modules[__name__]


class _LazyConfig:
    """Lazy config singleton — resolves at attribute access time."""
    def __getattr__(self, name):
        return getattr(Config(), name)

    def __repr__(self):
        return repr(get_config())


_module.config = _LazyConfig()
