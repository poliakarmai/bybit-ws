"""
lstm_regime.py — LSTM-классификатор рыночного режима (Фаза 5.4).

Замена эвристического regime.py на нейросетевой предиктор.
Предсказывает режим на следующие 7 дней по 30-дневной истории D-свечей.

Классы:
  TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL

Метки — forward-looking (lookahead 7 дней):
  TRENDING_UP:   return > +5%
  TRENDING_DOWN: return < -5%
  HIGH_VOL:      avg daily range > 4%
  LOW_VOL:       avg daily range < 1.5%
  RANGING:       иначе

Архитектура:
  Input(30, 8) → LSTM(64) → LSTM(32) → Dense(32) → Dense(5) softmax

Использование:
  python lstm_regime.py --train   # обучить модель
  python lstm_regime.py --predict # предсказать текущий режим
"""

import hashlib
import hmac
import json
import math
import os
import pickle  # только для обратной совместимости, новые модели через json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    torch = None
    nn = None
    optim = None

DATA_DIR = Path.home() / '.local' / 'share' / 'bybit-ws'
MODEL_DIR = DATA_DIR / 'models'
MODEL_PATH = MODEL_DIR / 'lstm_regime.pt'
SCALER_PATH = MODEL_DIR / 'lstm_regime_scaler.pkl'
FEATURES_PATH = MODEL_DIR / 'lstm_regime_features.json'
CACHE_PATH = DATA_DIR / 'lstm_regime_cache.json'

# ── Production guard ──
_FALLBACK_KEY = 'bybit-ws-model-integrity-dev'
_HMAC_RAW = os.getenv('BYBIT_HMAC_SECRET')
if not _HMAC_RAW:
    if os.getenv('BYBIT_WS_PRODUCTION') == '1':
        sys.exit('FATAL: BYBIT_HMAC_SECRET not set in production')
    else:
        print('WARNING: using fallback HMAC key (dev mode). Set BYBIT_HMAC_SECRET for production.', flush=True)
        _HMAC_RAW = _FALLBACK_KEY
HMAC_SECRET: bytes = _HMAC_RAW.encode()


def _sign_lstm_file(path: Path):
    """Подписать файл HMAC-SHA256."""
    sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    sig = hmac.new(HMAC_SECRET, sha.encode(), hashlib.sha256).hexdigest()
    open(str(path) + '.hmac', 'w').write(sig)


def _verify_lstm_file(path: Path) -> bool:
    """Проверить HMAC-подпись файла."""
    sig_path = str(path) + '.hmac'
    if not os.path.exists(sig_path):
        return False
    sha = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    expected = hmac.new(HMAC_SECRET, sha.encode(), hashlib.sha256).hexdigest()
    actual = open(sig_path).read().strip()
    return hmac.compare_digest(expected, actual)

SEQUENCE_LENGTH = 30
LOOKAHEAD = 7
N_CLASSES = 5
N_FEATURES = 11   # 8 технических + 3 макро (BTC.D, ETH/BTC, Fear&Greed)

CLASS_NAMES = ['TRENDING_UP', 'TRENDING_DOWN', 'RANGING', 'HIGH_VOL', 'LOW_VOL']
CLASS_IDX = {name: i for i, name in enumerate(CLASS_NAMES)}

# ── Фаза 5.4: Feature flag авто-переключения LONG/SHORT ──
REGIME_AUTO_ENABLED = os.getenv('BYBIT_REGIME_AUTO', '0') == '1'

# ── Макро-признаки (кеш) ──
_macro_cache = {'data': None, 'ts': 0}
_MACRO_CACHE_TTL = 3600  # 1 час

# ── Признаки ─────────────────────────────────────────────────

def _calc_features(closes, highs, lows, volumes):
    """Вычислить 8 признаков для одной свечи."""
    n = len(closes)
    if n < 20:
        return None

    # Текущая свеча
    close = closes[-1]
    high = highs[-1]
    low = lows[-1]
    vol = volumes[-1]

    if close == 0:
        return None

    # 1. Дневная доходность
    daily_return = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0

    # 2. High-Low range %
    hl_range = (high - low) / close * 100

    # 3. BB% положение (20-дневный)
    bb_window = closes[-20:]
    sma = sum(bb_window) / 20
    std = (sum((x - sma) ** 2 for x in bb_window) / 20) ** 0.5
    bb_pct = (close - (sma - 2 * std)) / (4 * std) * 100 if std > 0 else 50

    # 4. BB ширина %
    bb_width = (4 * std) / sma * 100 if sma > 0 else 10

    # 5. RSI(14)
    gains = 0.0
    losses = 0.0
    rsi_period = min(14, n - 1)
    for i in range(-rsi_period, 0):
        chg = closes[i + 1] - closes[i] if i + 1 < 0 else close - closes[-2]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    avg_gain = gains / rsi_period
    avg_loss = losses / rsi_period
    rsi = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss > 0 else 100

    # 6. ATR(14)/close
    trs = []
    for i in range(-min(14, n - 1), 0):
        h = highs[i] if i >= -len(highs) else high
        l = lows[i] if i >= -len(lows) else low
        prev_c = closes[i - 1] if i - 1 >= -len(closes) else closes[-2]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0
    atr_pct = atr / close * 100 if close > 0 else 0

    # 7. Объём / 20-дневное среднее
    avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else sum(volumes[:-1]) / max(1, len(volumes) - 1)
    vol_ratio = vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    # 8. Momentum: отношение close к close 5 дней назад
    mom_5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else 0

    return [daily_return, hl_range, bb_pct, bb_width, rsi, atr_pct, vol_ratio, mom_5]


# ── Макро-признаки (Фаза 5.4) ─────────────────────────────────

def _fetch_btc_dominance() -> Optional[float]:
    """Получить BTC Dominance % через CoinGecko API."""
    import urllib.request, json
    try:
        url = 'https://api.coingecko.com/api/v3/global'
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        btc_dom = data.get('data', {}).get('market_cap_percentage', {}).get('btc')
        if btc_dom is not None:
            return float(btc_dom)
    except Exception:
        pass
    # Fallback: оценить через объёмы из Bybit API
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        try:
            from bybit_ws.api import bybit
        except ImportError:
            from api import bybit
        resp = bybit('GET', '/v5/market/tickers?category=linear&symbol=BTCUSDT')
        if resp and resp.get('retCode') == 0:
            tickers = resp['result'].get('list', [])
            if tickers:
                btc_vol = float(tickers[0].get('turnover24h', 0))
                # Грубая оценка: BTC.D ~ 40-60% в нормальных условиях
                # Используем нейтральное значение при недоступности API
                return 50.0
    except Exception:
        pass
    return None


def _fetch_eth_btc_ratio() -> Optional[float]:
    """Получить ETH/BTC соотношение через Bybit API."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        try:
            from bybit_ws.api import bybit
        except ImportError:
            from api import bybit
        # ETHUSDT
        resp_eth = bybit('GET', '/v5/market/tickers?category=linear&symbol=ETHUSDT')
        resp_btc = bybit('GET', '/v5/market/tickers?category=linear&symbol=BTCUSDT')
        if (resp_eth and resp_eth.get('retCode') == 0 and
                resp_btc and resp_btc.get('retCode') == 0):
            eth_price = float(resp_eth['result']['list'][0]['lastPrice'])
            btc_price = float(resp_btc['result']['list'][0]['lastPrice'])
            if btc_price > 0:
                return eth_price / btc_price
    except Exception:
        pass
    return None


def _fetch_fear_greed() -> Optional[float]:
    """Получить Fear & Greed индекс через alternative.me API (0-100)."""
    import urllib.request, json
    try:
        url = 'https://api.alternative.me/fng/?limit=1'
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        fg_value = data.get('data', [{}])[0].get('value')
        if fg_value is not None:
            return float(fg_value)
    except Exception:
        pass
    return None


def _get_macro_features() -> list:
    """Получить 3 макро-признака с кешированием.
    Возвращает [btc_dominance, eth_btc_ratio, fear_greed].
    """
    global _macro_cache
    now = time.time()
    if _macro_cache['data'] is not None and (now - _macro_cache['ts']) < _MACRO_CACHE_TTL:
        return _macro_cache['data']

    btc_dom = _fetch_btc_dominance()
    eth_btc = _fetch_eth_btc_ratio()
    fng = _fetch_fear_greed()

    # Нормализация:
    # BTC.D: обычно 35-70%, нормируем к ~0-1 (делим на 100)
    # ETH/BTC: обычно 0.02-0.10, нормируем к ~0-1 (×10)
    # F&G: 0-100, нормируем к 0-1 (делим на 100)
    result = [
        btc_dom / 100.0 if btc_dom is not None else 0.5,
        eth_btc * 10.0 if eth_btc is not None else 0.5,
        fng / 100.0 if fng is not None else 0.5,
    ]
    _macro_cache = {'data': result, 'ts': now}
    return result


def _label_regime(closes, highs, lows, start_idx, lookahead=LOOKAHEAD):
    """Метка режима на основе будущих свечей."""
    end = start_idx + lookahead
    if end >= len(closes):
        return None

    future_closes = closes[start_idx:end]
    future_highs = highs[start_idx:end]
    future_lows = lows[start_idx:end]

    # Доходность за lookahead дней
    total_return = (future_closes[-1] / closes[start_idx - 1] - 1) * 100 if start_idx > 0 else 0

    # Средний дневной диапазон
    ranges = [(future_highs[i] - future_lows[i]) / future_closes[i] * 100
              for i in range(len(future_closes)) if future_closes[i] > 0]
    avg_range = sum(ranges) / len(ranges) if ranges else 0

    # Классификация
    if total_return > 5.0:
        return 'TRENDING_UP'
    elif total_return < -5.0:
        return 'TRENDING_DOWN'
    elif avg_range > 4.0:
        return 'HIGH_VOL'
    elif avg_range < 1.5:
        return 'LOW_VOL'
    else:
        return 'RANGING'


# ── Модель ───────────────────────────────────────────────────

if HAS_TORCH:
    class LSTMModel(nn.Module):
        def __init__(self, input_size=N_FEATURES, hidden_size=64, num_layers=2, num_classes=N_CLASSES, dropout=0.3):
            super().__init__()
            self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                batch_first=True, dropout=dropout if num_layers > 1 else 0)
            self.fc1 = nn.Linear(hidden_size, 32)
            self.relu = nn.ReLU()
            self.dropout = nn.Dropout(dropout)
            self.fc2 = nn.Linear(32, num_classes)

        def forward(self, x):
            # x: (batch, seq_len, input_size)
            lstm_out, (hn, cn) = self.lstm(x)
            last_out = lstm_out[:, -1, :]  # последний временной шаг
            out = self.relu(self.fc1(last_out))
            out = self.dropout(out)
            out = self.fc2(out)
            return out


# ── Данные ───────────────────────────────────────────────────

def fetch_training_data(symbols=('BTCUSDT', 'ETHUSDT'), days=365):
    """
    Загрузить исторические D-свечи через Bybit API.
    Возвращает list[dict]: symbol, closes, highs, lows, volumes.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from bybit_ws.api import bybit
    except ImportError:
        from api import bybit

    all_data = []
    for sym in symbols:
        closes, highs, lows, volumes = [], [], [], []
        # Bybit отдаёт максимум 200 свечей за запрос
        fetched = 0
        end_ms = None
        while fetched < days:
            limit = min(200, days - fetched)
            url = f'/v5/market/kline?category=linear&symbol={sym}&interval=D&limit={limit}'
            if end_ms:
                url += f'&end={end_ms}'
            resp = bybit('GET', url)
            if not resp or resp.get('retCode') != 0:
                break
            candles = resp['result'].get('list', [])
            if not candles:
                break
            # candles: [timestamp, open, high, low, close, volume, turnover] — newest first
            for c in reversed(candles):
                closes.append(float(c[4]))
                highs.append(float(c[2]))
                lows.append(float(c[3]))
                volumes.append(float(c[5]))
            fetched += len(candles)
            if len(candles) < limit:
                break
            end_ms = candles[0][0]  # временная метка последней полученной свечи
            time.sleep(0.15)  # rate limit
        if closes:
            all_data.append({'symbol': sym, 'closes': closes, 'highs': highs, 'lows': lows, 'volumes': volumes})
    return all_data


def build_dataset(all_data, seq_len=SEQUENCE_LENGTH):
    """Построить датасет (X, y) из загруженных данных с макро-признаками."""
    X_list, y_list = [], []

    # Получаем макро-признаки (одинаковы для всех сэмплов — текущие значения)
    macro = _get_macro_features()

    for sym_data in all_data:
        closes = sym_data['closes']
        highs = sym_data['highs']
        lows = sym_data['lows']
        volumes = sym_data['volumes']

        for i in range(seq_len, len(closes) - LOOKAHEAD):
            # Признаки за последние seq_len дней
            features = []
            for j in range(i - seq_len, i):
                feat = _calc_features(closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1])
                if feat is None:
                    break
                # Добавляем макро-признаки к каждому временному шагу
                features.append(feat + macro)
            if len(features) != seq_len:
                continue

            # Метка (forward-looking)
            label = _label_regime(closes, highs, lows, i)
            if label is None:
                continue

            X_list.append(features)
            y_list.append(CLASS_IDX[label])

    if not X_list:
        return None, None
    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int64)
    # Замена NaN/Inf
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return X, y


# ── Обучение ─────────────────────────────────────────────────

class FeatureScaler:
    """MinMax scaler с сохранением параметров."""
    def __init__(self):
        self.min_ = None
        self.max_ = None

    def fit(self, X):
        self.min_ = np.min(X, axis=(0, 1))
        self.max_ = np.max(X, axis=(0, 1))
        # Избегаем деления на ноль
        self.max_[self.max_ == self.min_] = self.min_[self.max_ == self.min_] + 1

    def transform(self, X):
        return (X - self.min_) / (self.max_ - self.min_)

    def fit_transform(self, X):
        self.fit(X)
        return self.transform(X)


def train_model(force=False):
    """Обучить LSTM-модель и сохранить на диск."""
    if not HAS_TORCH:
        print("❌ PyTorch не установлен. pip install torch")
        return None

    # Загрузка данных
    print("📡 Загрузка исторических данных...")
    data = fetch_training_data(['BTCUSDT', 'ETHUSDT'], days=365)
    if not data:
        print("❌ Не удалось загрузить данные")
        return None

    print(f"   BTC: {len(data[0]['closes'])} дней, ETH: {len(data[1]['closes'])} дней")
    print("🔨 Построение датасета...")
    X, y = build_dataset(data)
    if X is None:
        print("❌ Не удалось построить датасет")
        return None

    print(f"   Сэмплов: {len(X)}, форма: {X.shape}")

    # Баланс классов
    unique, counts = np.unique(y, return_counts=True)
    for cls_idx, cnt in zip(unique, counts):
        print(f"   {CLASS_NAMES[cls_idx]}: {cnt} ({cnt/len(y)*100:.1f}%)")

    if len(unique) < 3:
        print("❌ Недостаточно классов для обучения (нужно ≥3)")
        return None

    # Масштабирование
    scaler = FeatureScaler()
    X_scaled = scaler.fit_transform(X)

    # Train/val split
    split = int(len(X) * 0.8)
    X_train = torch.tensor(X_scaled[:split], dtype=torch.float32)
    y_train = torch.tensor(y[:split], dtype=torch.long)
    X_val = torch.tensor(X_scaled[split:], dtype=torch.float32)
    y_val = torch.tensor(y[split:], dtype=torch.long)

    # Class weights для дисбаланса (все N_CLASSES, даже если не представлены)
    weight_array = np.ones(N_CLASSES, dtype=np.float32)
    for cls_idx, cnt in zip(unique, counts):
        weight_array[cls_idx] = len(y) / (len(unique) * max(cnt, 1))
    class_weights = torch.tensor(weight_array, dtype=torch.float32)

    # Модель
    model = LSTMModel()
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=5, factor=0.5)

    # Обучение
    batch_size = 32
    epochs = 100
    best_val_acc = 0

    print(f"\n🏋️ Обучение ({epochs} эпох)...")
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        n_batches = 0
        indices = torch.randperm(len(X_train))

        for i in range(0, len(X_train), batch_size):
            batch_idx = indices[i:i + batch_size]
            xb = X_train[batch_idx]
            yb = y_train[batch_idx]

            optimizer.zero_grad()
            out = model(xb)
            loss = criterion(out, yb)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            n_batches += 1

        # Validation
        model.eval()
        with torch.no_grad():
            val_out = model(X_val)
            val_loss = criterion(val_out, y_val).item()
            val_pred = val_out.argmax(dim=1)
            val_acc = (val_pred == y_val).float().mean().item()

        scheduler.step(val_loss)

        if val_acc > best_val_acc:
            best_val_acc = val_acc

        if (epoch + 1) % 20 == 0:
            print(f"   Epoch {epoch+1:3d}: loss={total_loss/n_batches:.4f} val_acc={val_acc:.3f}")

    print(f"\n✅ Обучение завершено. Best val acc: {best_val_acc:.3f}")

    # Сохранение
    os.makedirs(MODEL_DIR, exist_ok=True)
    torch.save(model.state_dict(), MODEL_PATH)
    _sign_lstm_file(MODEL_PATH)
    with open(SCALER_PATH, 'wb') as f:
        pickle.dump(scaler, f)
    _sign_lstm_file(SCALER_PATH)
    # Макро-значения для логов
    macro_vals = _get_macro_features()
    with open(FEATURES_PATH, 'w') as f:
        json.dump({
            'n_samples': len(X),
            'n_features': N_FEATURES,
            'seq_length': SEQUENCE_LENGTH,
            'classes': CLASS_NAMES,
            'macro_features': ['btc_dominance', 'eth_btc_ratio', 'fear_greed'],
            'macro_values': {
                'btc_dominance': round(macro_vals[0] * 100, 1),
                'eth_btc_ratio': round(macro_vals[1] / 10, 4),
                'fear_greed': round(macro_vals[2] * 100, 0),
            },
            'val_accuracy': round(best_val_acc, 3),
            'trained_at': datetime.now().isoformat(),
            'symbols': ['BTCUSDT', 'ETHUSDT'],
        }, f, indent=2)

    print(f"   Модель: {MODEL_PATH}")
    print(f"   Скейлер: {SCALER_PATH}")

    return model


# ── Инференс ─────────────────────────────────────────────────

def _fetch_recent_klines(symbol, days=SEQUENCE_LENGTH + 5):
    """Загрузить последние N дней свечей."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from bybit_ws.api import bybit
    except ImportError:
        from api import bybit

    url = f'/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit={days}'
    resp = bybit('GET', url)
    if not resp or resp.get('retCode') != 0:
        return None

    candles = resp['result'].get('list', [])
    if len(candles) < SEQUENCE_LENGTH:
        return None

    closes, highs, lows, volumes = [], [], [], []
    for c in reversed(candles):
        closes.append(float(c[4]))
        highs.append(float(c[2]))
        lows.append(float(c[3]))
        volumes.append(float(c[5]))

    return {'symbol': symbol, 'closes': closes, 'highs': highs, 'lows': lows, 'volumes': volumes}


def predict_regime(symbols=('BTCUSDT', 'ETHUSDT')) -> Optional[dict]:
    """
    Предсказать текущий рыночный режим.
    Возвращает dict с классом и вероятностями или None.
    """
    if not HAS_TORCH or not MODEL_PATH.exists():
        return None

    try:
        # Загрузка модели
        model = LSTMModel()
        if not _verify_lstm_file(MODEL_PATH):
            print(f'⚠️ HMAC mismatch for {MODEL_PATH} — skipping LSTM', flush=True)
            return None
        model.load_state_dict(torch.load(MODEL_PATH, map_location='cpu', weights_only=True))
        model.eval()

        if SCALER_PATH.suffix == '.json':
            if not _verify_lstm_file(SCALER_PATH):
                print(f'⚠️ HMAC mismatch for {SCALER_PATH} — skipping LSTM', flush=True)
                return None
            import json
            with open(SCALER_PATH, 'r') as f:
                scaler_data = json.load(f)
            from sklearn.preprocessing import StandardScaler
            scaler = StandardScaler()
            scaler.mean_ = np.array(scaler_data['mean_'])
            scaler.scale_ = np.array(scaler_data['scale_'])
            scaler.var_ = np.array(scaler_data['var_'])
            scaler.n_features_in_ = scaler_data['n_features_in_']
        else:
            with open(SCALER_PATH, 'rb') as f:
                scaler = pickle.load(f)

        # Данные по каждому символу
        all_probs = []
        macro = _get_macro_features()  # макро-признаки для предсказания
        for sym in symbols:
            data = _fetch_recent_klines(sym)
            if not data:
                continue

            # Извлечение признаков
            features = []
            closes = data['closes']
            highs = data['highs']
            lows = data['lows']
            volumes = data['volumes']
            n = len(closes)

            zero_features = 0
            for j in range(n - SEQUENCE_LENGTH, n):
                feat = _calc_features(closes[:j + 1], highs[:j + 1], lows[:j + 1], volumes[:j + 1])
                if feat is None:
                    features.append([0] * N_FEATURES)
                    zero_features += 1
                else:
                    features.append(feat + macro)  # добавляем макро-признаки
            if zero_features > SEQUENCE_LENGTH // 2:
                print(f'⚠️ LSTM: {zero_features}/{SEQUENCE_LENGTH} нулевых признаков — прогноз ненадёжен')

            if len(features) != SEQUENCE_LENGTH:
                continue

            X = np.array([features], dtype=np.float32)
            X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

            if scaler.min_ is not None:
                X = scaler.transform(X)

            X_t = torch.tensor(X, dtype=torch.float32)
            with torch.no_grad():
                logits = model(X_t)
                probs = torch.softmax(logits, dim=1).numpy()[0]
            all_probs.append(probs)

        if not all_probs:
            return None

        # Среднее по символам
        avg_probs = np.mean(all_probs, axis=0)
        predicted_class = int(np.argmax(avg_probs))

        # Confidence = вероятность предсказанного класса
        confidence = round(float(avg_probs[predicted_class]) * 100)

        result = {
            'regime': CLASS_NAMES[predicted_class],
            'confidence': confidence,
            'probabilities': {
                CLASS_NAMES[i]: round(float(avg_probs[i]), 3)
                for i in range(N_CLASSES)
            },
            'timestamp': datetime.now().isoformat(),
        }

        # Кеширование
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_PATH, 'w') as f:
            json.dump(result, f, indent=2)

        return result

    except Exception as e:
        print(f'[LSTM] predict error: {e}', file=sys.stderr)
        return None


def get_cached_prediction() -> Optional[dict]:
    """Получить закешированное предсказание (без API-вызовов)."""
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ── Фаза 5.4: Авто-переключение LONG/SHORT по режиму ──────────

def get_regime_strategy(regime: str) -> dict:
    """Возвращает {LONG_ENABLED, SHORT_ENABLED} на основе режима рынка.

    Правила:
      TRENDING_UP    → только LONG (SHORT запрещён)
      TRENDING_DOWN  → только SHORT (LONG запрещён)
      RANGING        → оба разрешены (боковик)
      HIGH_VOL       → оба разрешены (волатильность даёт входы в обе стороны)
      LOW_VOL        → оба разрешены
      NEUTRAL/другое → оба разрешены (дефолт)

    Feature flag: BYBIT_REGIME_AUTO=1 должен быть установлен.
    Без флага всегда возвращает оба True.
    """
    if not REGIME_AUTO_ENABLED:
        return {'LONG_ENABLED': True, 'SHORT_ENABLED': True}

    strategy_map = {
        'TRENDING_UP':    {'LONG_ENABLED': True,  'SHORT_ENABLED': False},
        'TRENDING_DOWN':  {'LONG_ENABLED': False, 'SHORT_ENABLED': True},
        'RANGING':        {'LONG_ENABLED': True,  'SHORT_ENABLED': True},
        'HIGH_VOL':       {'LONG_ENABLED': True,  'SHORT_ENABLED': True},
        'LOW_VOL':        {'LONG_ENABLED': True,  'SHORT_ENABLED': True},
        'CHOPPY':         {'LONG_ENABLED': True,  'SHORT_ENABLED': True},
    }
    return strategy_map.get(regime, {'LONG_ENABLED': True, 'SHORT_ENABLED': True})


def get_current_regime_strategy() -> dict:
    """Получить текущую стратегию LONG/SHORT на основе предсказанного режима.

    Вызывается из main_async.py на каждом тяжёлом цикле.
    Возвращает {'LONG_ENABLED': bool, 'SHORT_ENABLED': bool, 'regime': str, 'confidence': int}.
    """
    regime = 'NEUTRAL'
    confidence = 50

    # Пробуем LSTM
    try:
        data = predict_regime()
        if not data:
            data = get_cached_prediction()
        if data:
            regime = data.get('regime', 'NEUTRAL')
            confidence = data.get('confidence', 50)
    except Exception:
        pass

    # Fallback на эвристический regime.py
    if regime == 'NEUTRAL':
        try:
            from .regime import check_regime as _check_regime
            result = _check_regime()
            regime = result.get('regime', 'NEUTRAL')
            confidence = result.get('confidence', 50)
        except Exception:
            pass

    strategy = get_regime_strategy(regime)
    return {
        'LONG_ENABLED': strategy['LONG_ENABLED'],
        'SHORT_ENABLED': strategy['SHORT_ENABLED'],
        'regime': regime,
        'confidence': confidence,
    }


# ── CLI ─────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='LSTM Market Regime Classifier')
    parser.add_argument('--train', action='store_true', help='Обучить модель')
    parser.add_argument('--predict', action='store_true', help='Предсказать текущий режим')
    parser.add_argument('--info', action='store_true', help='Информация о модели')
    args = parser.parse_args()

    if args.train:
        train_model()
    elif args.predict:
        result = predict_regime()
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            cached = get_cached_prediction()
            if cached:
                print(f'⚠️ Использую кеш: {json.dumps(cached, indent=2, ensure_ascii=False)}')
            else:
                print('❌ Не удалось предсказать режим')
    elif args.info:
        if FEATURES_PATH.exists():
            with open(FEATURES_PATH) as f:
                info = json.load(f)
            print(json.dumps(info, indent=2))
            if MODEL_PATH.exists():
                size_kb = MODEL_PATH.stat().st_size / 1024
                print(f'\nМодель: {MODEL_PATH} ({size_kb:.0f} KB)')
            else:
                print('\n⚠️ Файл модели не найден')
        else:
            print('Модель не обучена. Запустите --train')
    else:
        parser.print_help()
