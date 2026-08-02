"""
DSPy Optimizer v1.0 — оптимизация сигналов через DSPy (Фаза 5.1).

Загружает историю сделок из trade_history (SSOT), формирует признаки,
обучает DSPy-сигнатуру «входить/нет», оптимизирует через BootstrapFewShot + MIPROv2.

Интеграция с ml_scorer.py: dspy_scorer() вызывается параллельно с RF,
результаты усредняются или голосование.

Feature flag: BYBIT_DSPY_ENABLED (env, default 0).

Зависимости: dspy, numpy, sqlite3, joblib
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import dspy
import numpy as np

# ── Пути ──────────────────────────────────────────────────────────────────────
HOME = Path.home()
DATA_DIR = HOME / '.local' / 'share' / 'bybit-ws'
DSPY_MODEL_PATH = DATA_DIR / 'dspy_program'  # директория (save_program=True требует директорию без расширения)
DSPY_FEATURES_PATH = DATA_DIR / 'dspy_features.json'
DSPY_OPTIMIZED_PATH = DATA_DIR / 'dspy_optimized.json'
STATE_DB_PATH = DATA_DIR / 'state.db'
TRADE_HISTORY_TABLE = 'trade_history'

# ── Feature flag ──────────────────────────────────────────────────────────────
DSPY_ENABLED = os.getenv('BYBIT_DSPY_ENABLED', '0') == '1'

# ── HMAC (тот же секрет что у ml_scorer) ─────────────────────────────────────
_FALLBACK_KEY = 'bybit-ws-model-integrity-dev'
_HMAC_RAW = os.getenv('BYBIT_HMAC_SECRET')
if not _HMAC_RAW:
    if os.getenv('BYBIT_WS_PRODUCTION') == '1':
        sys.exit('FATAL: BYBIT_HMAC_SECRET not set in production')
    else:
        _HMAC_RAW = _FALLBACK_KEY
HMAC_SECRET: bytes = _HMAC_RAW.encode()


def _sign_file(path: Path) -> None:
    """Подписать файл HMAC-SHA256."""
    sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    sig = hmac.new(HMAC_SECRET, sha.encode(), hashlib.sha256).hexdigest()
    open(str(path) + '.hmac', 'w').write(sig)


def _verify_file(path: Path) -> bool:
    """Проверить HMAC-подпись файла."""
    sig_path = str(path) + '.hmac'
    if not os.path.exists(sig_path):
        return False
    sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    expected = hmac.new(HMAC_SECRET, sha.encode(), hashlib.sha256).hexdigest()
    actual = open(sig_path).read().strip()
    return hmac.compare_digest(expected, actual)


# ── Загрузка данных из trade_history (SSOT) ──────────────────────────────────

def _load_trade_history() -> list[dict]:
    """
    Загружает все завершённые сделки из state.db → trade_history.
    Возвращает список dict-ов с колонками:
    id, symbol, side, strategy, entry_price, exit_price, size, pnl, fees, entry_at, closed_at.
    """
    import sqlite3

    if not STATE_DB_PATH.exists():
        print(f'⚠️ state.db не найден: {STATE_DB_PATH}', flush=True)
        return []

    conn = sqlite3.connect(str(STATE_DB_PATH))
    rows = conn.execute(
        f'SELECT * FROM {TRADE_HISTORY_TABLE} WHERE entry_price > 0 AND exit_price > 0 ORDER BY closed_at DESC'
    ).fetchall()
    cols = [d[1] for d in conn.execute(f'PRAGMA table_info({TRADE_HISTORY_TABLE})')]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def _trade_to_features(trade: dict) -> Optional[dict]:
    """
    Преобразует одну сделку в словарь признаков для DSPy.

    Признаки:
      - pnl_pct: PnL в % от (entry_price * size) — прибыльность
      - is_long: 1 если LONG, 0 если SHORT
      - strategy_type: числовой код стратегии (x10=0, x10:scalp=1, ...)
      - price_change_pct: (exit - entry) / entry * 100
      - side_num: 1=LONG, -1=SHORT
      - abs_pnl: модуль PnL
      - profit_label: 1 если pnl > 0 (было TP), 0 иначе

    Возвращает None если данных недостаточно.
    """
    try:
        entry_price = float(trade.get('entry_price', 0))
        exit_price = float(trade.get('exit_price', 0))
        size = float(trade.get('size', 0))
        pnl = float(trade.get('pnl', 0))
        side = str(trade.get('side', '')).strip()
        strategy = str(trade.get('strategy', '')).strip()

        if entry_price <= 0 or size <= 0:
            return None

        # Процентное изменение цены
        price_change_pct = (exit_price - entry_price) / entry_price * 100

        # PnL в % от вложенного (entry_price * size)
        notional = entry_price * size
        pnl_pct = (pnl / notional * 100) if notional > 0 else 0

        # Кодирование стратегии
        strategy_map = {
            'x10': 0,
            'x10:scalp': 1,
            'x10:swing': 2,
            'long': 3,
            'short': 4,
            'junk': 5,
            'dca': 6,
        }
        strategy_type = strategy_map.get(strategy, -1)

        # Сторона
        is_long = 1 if side.lower() == 'buy' else 0
        side_num = 1 if side.lower() == 'buy' else -1
        # Для SHORT: положительный price_change = убыток
        if side_num == -1:
            price_change_pct = -price_change_pct

        features = {
            'pnl': pnl,
            'pnl_pct': round(pnl_pct, 4),
            'price_change_pct': round(price_change_pct, 4),
            'is_long': is_long,
            'side_num': side_num,
            'strategy_type': strategy_type,
            'abs_pnl': abs(pnl),
            'size': size,
            'symbol': trade.get('symbol', ''),
            'strategy': strategy,
            'side': side,
            'entry_price': entry_price,
            'exit_price': exit_price,
        }
        # Целевая метка: был ли трейд прибыльным?
        features['profit_label'] = 1 if pnl > 0 else 0

        return features
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _load_features() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """
    Загружает все сделки, извлекает признаки и целевые метки.

    Returns:
        X: numpy array признаков (float)
        y: numpy array меток (0/1 — убыток/прибыль)
        meta: список полных dict-ов признаков + метаданных
    """
    trades = _load_trade_history()
    features_list = []
    targets = []
    meta = []

    feature_keys = [
        'pnl_pct', 'price_change_pct', 'is_long', 'side_num',
        'strategy_type', 'abs_pnl', 'size'
    ]

    for trade in trades:
        feat = _trade_to_features(trade)
        if feat is None:
            continue
        row = [feat[k] for k in feature_keys]
        features_list.append(row)
        targets.append(feat['profit_label'])
        meta.append(feat)

    if not features_list:
        return np.array([]).reshape(0, len(feature_keys)), np.array([]), []

    X = np.array(features_list, dtype=np.float64)
    y = np.array(targets, dtype=np.int32)

    return X, y, meta


# ── DSPy Signature ────────────────────────────────────────────────────────────

class TradeSignalSignature(dspy.Signature):
    """
    DSPy-сигнатура: принимает признаки сделки, возвращает score (0-100) и binary вердикт.

    Вход: числовые признаки сделки (pnl_pct, price_change_pct, is_long, ...)
    Выход: score (0-100) и decision (ENTER/SKIP).
    """
    features: str = dspy.InputField(desc="Признаки сделки: JSON с pnl_pct, price_change_pct, is_long, side_num, strategy_type, abs_pnl, size")
    score: float = dspy.OutputField(desc="Score сигнала от 0 до 100, где >50 = сигнал к входу")
    decision: str = dspy.OutputField(desc="ENTER или SKIP — вердикт по входу в позицию")


class TradeSignalModule(dspy.Module):
    """
    DSPy-модуль для оценки торгового сигнала.
    Использует ChainOfThought для рассуждения о признаках.
    """

    def __init__(self):
        super().__init__()
        self.analyze = dspy.ChainOfThought(TradeSignalSignature)

    def forward(self, features: str) -> dspy.Prediction:
        """Прямой проход: признаки → score + decision."""
        return self.analyze(features=features)


# ── Оптимизация ──────────────────────────────────────────────────────────────

def train_dspy(
    lm_model: str = 'openai/gpt-4o-mini',
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    optimize: bool = True,
) -> Optional[dspy.Module]:
    """
    Обучить и оптимизировать DSPy-модель на исторических данных.

    Args:
        lm_model: модель LLM для DSPy (LM Studio: 'openai/...')
        api_key: API ключ (если None — из env OPENAI_API_KEY)
        api_base: базовый URL (если None — из env OPENAI_BASE_URL)
        optimize: выполнять ли MIPROv2-оптимизацию

    Returns:
        Оптимизированный модуль или None при ошибке.
    """
    import sqlite3

    try:
        log_event('🚀 DSPy: загрузка trade_history...')
    except Exception:
        print('🚀 DSPy: загрузка trade_history...', flush=True)

    X, y, meta = _load_features()

    if len(X) < 5:
        msg = f'❌ DSPy: недостаточно данных ({len(X)} сделок, нужно ≥5)'
        try:
            log_event(msg)
        except Exception:
            print(msg, flush=True)
        return None

    n_profit = int(np.sum(y))
    n_loss = len(y) - n_profit
    msg = f'📊 DSPy: {len(X)} сделок, прибыльных: {n_profit} ({n_profit/len(X)*100:.1f}%), убыточных: {n_loss}'
    try:
        log_event(msg)
    except Exception:
        print(msg, flush=True)

    if n_profit < 3:
        msg = f'❌ DSPy: слишком мало прибыльных сделок ({n_profit}, нужно ≥3)'
        try:
            log_event(msg)
        except Exception:
            print(msg, flush=True)
        return None

    # ── Конфигурация LM ──
    if api_key is None:
        api_key = os.getenv('OPENAI_API_KEY')
    if api_base is None:
        api_base = os.getenv('OPENAI_BASE_URL')

    if not api_key or api_key == '***':
        msg = '⚠️ DSPy: OPENAI_API_KEY не задан — обучение пропущено'
        try:
            log_event(msg)
        except Exception:
            print(msg, flush=True)
        return None

    if not api_base:
        msg = '⚠️ DSPy: OPENAI_BASE_URL не задан — обучение пропущено'
        try:
            log_event(msg)
        except Exception:
            print(msg, flush=True)
        return None

    try:
        lm = dspy.LM(
            model=lm_model,
            api_key=api_key,
            api_base=api_base,
            temperature=0.0,
            max_tokens=256,
        )
        dspy.configure(lm=lm)
        log_event(f'🔧 DSPy: настроен LM={lm_model}, base={api_base}')
    except Exception as e:
        msg = f'⚠️ DSPy: не удалось настроить LM ({e}), продолжаем без LLM...'
        try:
            log_event(msg)
        except Exception:
            print(msg, flush=True)

    # ── Готовим обучающие примеры ──
    feature_keys = ['pnl_pct', 'price_change_pct', 'is_long', 'side_num',
                    'strategy_type', 'abs_pnl', 'size']

    trainset = []
    for i in range(len(X)):
        feat_dict = {feature_keys[j]: float(X[i][j]) for j in range(len(feature_keys))}
        feat_json = json.dumps(feat_dict)

        # Определяем score и decision по факту
        if y[i] == 1:
            score = 75.0 + np.random.uniform(0, 25)  # прибыльный → высокий score
            decision = 'ENTER'
        else:
            score = 25.0 + np.random.uniform(0, 25)  # убыточный → низкий score
            decision = 'SKIP'

        example = dspy.Example(
            features=feat_json,
            score=round(score, 1),
            decision=decision,
        ).with_inputs('features')
        trainset.append(example)

    # ── Создаём и обучаем программу ──
    program = TradeSignalModule()

    if not optimize or len(trainset) < 10:
        log_event('⚠️ DSPy: пропуск оптимизации (мало данных или optimize=False)')
        # Сохраняем неоптимизированную модель
        _save_program(program, X, y, meta, feature_keys, n_profit, n_loss, optimized=False)
        return program

    try:
        log_event('🔧 DSPy: BootstrapFewShot (k=3)...')

        # BootstrapFewShot — генерирует few-shot примеры
        bootstrap_optimizer = dspy.BootstrapFewShot(
            metric=dspy.evaluate.answer_exact_match,
            max_bootstrapped_demos=3,
            max_labeled_demos=5,
        )
        program = bootstrap_optimizer.compile(program, trainset=trainset)

        log_event('🔧 DSPy: MIPROv2 оптимизация...')

        # MIPROv2 — оптимизация промптов и весов
        mipro_optimizer = dspy.MIPROv2(
            metric=dspy.evaluate.answer_exact_match,
            num_candidates=5,
            init_temperature=0.5,
        )
        optimized = mipro_optimizer.compile(program, trainset=trainset)

        log_event('✅ DSPy: оптимизация завершена')
    except Exception as e:
        log_event(f'⚠️ DSPy: ошибка оптимизации ({e}), сохраняем базовую модель')

    # ── Сохраняем модель ──
    _save_program(program, X, y, meta, feature_keys, n_profit, n_loss,
                  optimized=optimize)

    return program


def _save_program(
    program: dspy.Module,
    X: np.ndarray,
    y: np.ndarray,
    meta: list[dict],
    feature_keys: list[str],
    n_profit: int,
    n_loss: int,
    optimized: bool = False,
) -> None:
    """Сохранить DSPy-программу и метаданные."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # Сохраняем через dspy.Module.save() — JSON state + cloudpickle при save_program=True
    try:
        # save_program=True: полное сохранение (архитектура + состояние) в директорию
        program.save(str(DSPY_MODEL_PATH), save_program=True)
        # Подписываем главный JSON-файл из директории
        json_path = DSPY_MODEL_PATH / 'metadata.json' if DSPY_MODEL_PATH.is_dir() else DSPY_MODEL_PATH
        if json_path.exists():
            _sign_file(json_path)
        log_event(f'✅ DSPy: программа сохранена в {DSPY_MODEL_PATH}')
    except Exception as e:
        log_event(f'⚠️ DSPy: ошибка сохранения программы: {e}')

    # Сохраняем метаданные
    with open(DSPY_FEATURES_PATH, 'w') as f:
        json.dump({
            'feature_keys': feature_keys,
            'n_samples': int(len(X)),
            'n_profit': n_profit,
            'n_loss': n_loss,
            'profit_ratio': round(n_profit / max(1, len(X)), 3),
            'optimized': optimized,
            'model_type': 'DSPy TradeSignalModule',
            'dspy_version': dspy.__version__,
            'created_at': int(time.time()),
        }, f, indent=2, ensure_ascii=False)


# ── Инференс ──────────────────────────────────────────────────────────────────

def predict(signal_data: dict) -> Optional[float]:
    """
    Предсказать score (0-100) для нового сигнала через DSPy.

    Args:
        signal_data: словарь с признаками сигнала.
            Ожидаемые ключи: pnl_pct, price_change_pct, is_long, side_num,
            strategy_type, abs_pnl, size

    Returns:
        Score от 0 до 100, или None если модель недоступна.
    """
    if not DSPY_ENABLED:
        return None

    if not DSPY_MODEL_PATH.exists():
        return None

    try:
        # Проверяем HMAC-подпись (файл metadata.json внутри директории программы)
        hmac_check_path = DSPY_MODEL_PATH / 'metadata.json' if DSPY_MODEL_PATH.is_dir() else DSPY_MODEL_PATH
        if not hmac_check_path.exists():
            hmac_check_path = DSPY_MODEL_PATH
        if hmac_check_path.exists() and hmac_check_path.suffix == '.json':
            if not _verify_file(hmac_check_path):
                log_event('⚠️ DSPy: HMAC mismatch — модель могла быть изменена')
                return None

        # Загружаем через dspy.load (родной загрузчик)
        program = dspy.load(str(DSPY_MODEL_PATH))

        feature_keys = [
            'pnl_pct', 'price_change_pct', 'is_long', 'side_num',
            'strategy_type', 'abs_pnl', 'size'
        ]

        # Извлекаем признаки из signal_data
        feat_dict = {}
        for k in feature_keys:
            feat_dict[k] = float(signal_data.get(k, 0.0))

        feat_json = json.dumps(feat_dict)

        result = program(features=feat_json)
        try:
            score = float(getattr(result, 'score', 50.0))
        except (ValueError, TypeError):
            score = 50.0

        return round(max(0.0, min(100.0, score)), 1)

    except Exception as e:
        log_event(f'⚠️ DSPy: ошибка predict: {e}')
        return None


def dspy_gate_pass(signal_data: dict) -> tuple[bool, Optional[float]]:
    """
    DSPy-гейт: проверяет, стоит ли входить в сигнал.

    Args:
        signal_data: словарь признаков сигнала (те же что для predict)

    Returns:
        (passed: bool, dspy_score: float | None)
        passed=True если score ≥ 50, иначе False.
        Если модель недоступна → always pass (True, None).
    """
    if not DSPY_ENABLED:
        return True, None

    dspy_score = predict(signal_data)
    if dspy_score is None:
        return True, None  # модель недоступна → полагаемся на эвристику

    return dspy_score >= 50, dspy_score


def dspy_adjusted_score(original_score: float, signal_data: dict) -> float:
    """
    Корректирует исходный score с учётом DSPy-предсказания.

    Формула: 0.6 × original + 0.4 × dspy_score
    Если DSPy недоступен → возвращает original_score.

    Args:
        original_score: исходный score (0-100)
        signal_data: признаки сигнала

    Returns:
        Скорректированный score (0-100).
    """
    if not DSPY_ENABLED:
        return original_score

    dspy_score = predict(signal_data)
    if dspy_score is None:
        return original_score

    adjusted = 0.6 * original_score + 0.4 * dspy_score
    return round(adjusted, 1)


# ── Интеграция с ml_scorer: голосование ─────────────────────────────────────

def combined_score(signal_data: dict) -> float:
    """
    Комбинированный score: голосование RF (ml_scorer) + DSPy.
    Вызывается из ml_scorer.ml_adjusted_score или напрямую.

    Args:
        signal_data: признаки сигнала (полный словарь из gridsignal_scanner)

    Returns:
        Комбинированный score (0-100).
    """
    original = float(signal_data.get('score', 50.0))

    # RF score
    rf_score = original
    try:
        # Импортируем здесь чтобы избежать циклической зависимости
        from .ml_scorer import ml_adjusted_score as _rf_adjusted
        rf_score = _rf_adjusted(signal_data)
    except ImportError:
        try:
            from ml_scorer import ml_adjusted_score as _rf_adjusted
            rf_score = _rf_adjusted(signal_data)
        except ImportError:
            pass

    # DSPy score
    dspy_sc = predict(signal_data)
    if dspy_sc is not None:
        # Взвешенное среднее: 50% RF + 50% DSPy
        return round(0.5 * rf_score + 0.5 * dspy_sc, 1)

    return rf_score


# ── Логирование ──────────────────────────────────────────────────────────────

def log_event(msg: str) -> None:
    """Логировать событие через alerts.log_event (fallback: print)."""
    try:
        from .alerts import log_event as _log
        _log(msg)
    except ImportError:
        try:
            from alerts import log_event as _log
            _log(msg)
        except ImportError:
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] [DSPy] {msg}', flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='DSPy Optimizer для bybit-ws')
    parser.add_argument('--train', action='store_true', help='Обучить DSPy-модель')
    parser.add_argument('--info', action='store_true', help='Информация о модели')
    parser.add_argument('--test', action='store_true', help='Тестовый прогон на исторических данных')
    parser.add_argument('--model', default='openai/gpt-4o-mini', help='Модель LLM для DSPy')
    parser.add_argument('--api-base', default=None, help='API base URL')
    parser.add_argument('--no-optimize', action='store_true', help='Пропустить MIPROv2-оптимизацию')

    args = parser.parse_args()

    if args.train:
        train_dspy(
            lm_model=args.model,
            api_base=args.api_base,
            optimize=not args.no_optimize,
        )
    elif args.info:
        if DSPY_FEATURES_PATH.exists():
            with open(DSPY_FEATURES_PATH) as f:
                info = json.load(f)
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print('Модель DSPy не обучена. Запустите --train')
    elif args.test:
        X, y, meta = _load_features()
        print(f'Загружено {len(X)} сделок')
        print(f'Прибыльных: {int(np.sum(y))}, убыточных: {len(y) - int(np.sum(y))}')
        if DSPY_MODEL_PATH.exists():
            print(f'Модель существует: {DSPY_MODEL_PATH}')
            print(f'Размер: {DSPY_MODEL_PATH.stat().st_size} байт')
        else:
            print('Модель не обучена')
    else:
        parser.print_help()
