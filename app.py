"""
Render デプロイ用 Flask バックエンド（PC不要・24/7稼働版）
- Yahoo Finance から GOLD 30分足データを自動取得
- RSI クロスオーバー + Gemini AI ダマシ判定
- WebSocket でスマホアプリにリアルタイム配信
- Supabase REST API にシグナル履歴保存
"""

import os
import json
import time
import threading
from datetime import datetime, timezone

import requests as req
import yfinance as yf
from flask import Flask, jsonify, request
from flask_socketio import SocketIO

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

import numpy as np
import pandas as pd
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, messaging, firestore as fb_firestore

# ==================== 環境変数 ====================
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
PUSH_SECRET     = os.environ.get("PUSH_SECRET", "goldtrader_push_2026")

# 動的設定（APIで変更可能）
TIMEFRAME_MINUTES = int(os.environ.get("TIMEFRAME_MINUTES", "30"))
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
CROSSOVER_MODE = os.environ.get("CROSSOVER_MODE", "RSI")  # "RSI", "MACD", "RSI_MACD", "COMPOSITE"

# トレード自動化設定
TRADING_MODE = os.environ.get("TRADING_MODE", "MANUAL")  # MANUAL / SEMI_AUTO / FULL_AUTO
AUTO_CONFIDENCE_THRESHOLD = int(os.environ.get("AUTO_CONFIDENCE_THRESHOLD", "70"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "goldtrader_webhook_2026")
MT5_WEBHOOK_URL = os.environ.get("MT5_WEBHOOK_URL", "")
MT5_WEBHOOK_TIMEOUT = 5

# 設定変更時にシグナルループのスリープを即座に中断するイベント
settings_changed = threading.Event()

# Gemini API クールダウン（同じシグナルへの重複呼び出し防止）
_last_gemini_call_time = 0.0
_last_gemini_signal_key = ""
GEMINI_COOLDOWN_SEC = 300  # 5分クールダウン（クォータ節約）

# ==================== 初期化 ====================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "goldtrader_secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-3.8-flash")

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Push-Secret"
    return response

# Firebase Admin SDK 初期化
_firebase_cert = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
if _firebase_cert:
    _cred = credentials.Certificate(json.loads(_firebase_cert))
    firebase_admin.initialize_app(_cred)
    FCM_ENABLED = True
else:
    FCM_ENABLED = False
    print("⚠️  FIREBASE_SERVICE_ACCOUNT_JSON 未設定 → FCMプッシュ無効")

# FCMトークン一覧（メモリ + Firestore永続化）
fcm_tokens: set[str] = set()
_firestore_db = None

def _get_firestore():
    global _firestore_db
    if _firestore_db is None and FCM_ENABLED:
        try:
            _firestore_db = fb_firestore.client()
        except Exception as e:
            print(f"⚠️  Firestore初期化エラー: {e}")
    return _firestore_db

def load_fcm_tokens():
    """Firestoreから起動時にFCMトークンを復元"""
    db = _get_firestore()
    if not db:
        return
    try:
        docs = db.collection("fcm_tokens").stream()
        for doc in docs:
            fcm_tokens.add(doc.id)
        if fcm_tokens:
            print(f"✓ FCMトークン復元: {len(fcm_tokens)}件")
    except Exception as e:
        print(f"⚠️  Firestoreトークン読み込みエラー: {e}")

def persist_fcm_token(token: str):
    """FCMトークンをFirestoreに保存"""
    db = _get_firestore()
    if not db:
        return
    try:
        db.collection("fcm_tokens").document(token).set(
            {"updated_at": datetime.now(timezone.utc).isoformat()},
            merge=True
        )
    except Exception as e:
        print(f"⚠️  Firestoreトークン保存エラー: {e}")

def delete_fcm_token(token: str):
    """無効なFCMトークンをFirestoreから削除"""
    db = _get_firestore()
    if not db:
        return
    try:
        db.collection("fcm_tokens").document(token).delete()
    except Exception as e:
        print(f"⚠️  Firestoreトークン削除エラー: {e}")

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

# ==================== MT5 Webhook 自動注文 ====================
def send_mt5_order(signal_id, direction, entry_price, sl_pips=20, tp_pips=40):
    """MT5 Webhook サーバーに自動注文を送信し、Supabaseにトレードを記録する"""
    if not MT5_WEBHOOK_URL:
        print("⚠️  MT5_WEBHOOK_URL 未設定 → 自動注文スキップ")
        return {"success": False, "error": "Webhook URL not configured"}

    if direction == "BUY":
        sl_price = round(entry_price - sl_pips * 0.1, 2)
        tp_price = round(entry_price + tp_pips * 0.1, 2)
    else:
        sl_price = round(entry_price + sl_pips * 0.1, 2)
        tp_price = round(entry_price - tp_pips * 0.1, 2)

    payload = {
        "signal_id": signal_id,
        "direction": direction,
        "entry_price": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "volume": 0.1,
        "magic_number": 20260928,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    try:
        response = req.post(
            MT5_WEBHOOK_URL,
            json=payload,
            timeout=MT5_WEBHOOK_TIMEOUT,
            headers={"X-Webhook-Secret": WEBHOOK_SECRET}
        )
        if response.status_code == 200:
            result = response.json()
            print(f"✅ MT5注文成功: {direction} @{entry_price} SL={sl_price} TP={tp_price}")
            # Supabase にトレード記録
            req.post(
                f"{SUPABASE_URL}/rest/v1/trades",
                json={
                    "signal_id": signal_id,
                    "direction": direction,
                    "entry_price": entry_price,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "status": "OPEN",
                    "auto_executed": True,
                    "sl": sl_price,
                    "tp": tp_price,
                },
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                timeout=10
            )
            return {"success": True, **result}
        else:
            print(f"❌ MT5 Webhook エラー: {response.status_code} {response.text}")
            return {"success": False, "error": response.text}
    except Exception as e:
        print(f"❌ MT5 注文送信エラー: {e}")
        return {"success": False, "error": str(e)}

def check_auto_execution(signal_data):
    """FULL_AUTO モード時に信頼度を確認して自動実行判定"""
    if TRADING_MODE != "FULL_AUTO":
        return {"should_execute": False, "reason": f"モード={TRADING_MODE}（FULL_AUTOのみ自動実行）"}
    if not signal_data.get("crossover"):
        return {"should_execute": False, "reason": "クロスオーバーなし"}
    if not signal_data.get("ai_valid"):
        return {"should_execute": False, "reason": "AI判定=ダマシ"}
    conf = signal_data.get("ai_confidence") or 0
    if conf < AUTO_CONFIDENCE_THRESHOLD:
        return {"should_execute": False, "reason": f"信頼度{conf}% < 閾値{AUTO_CONFIDENCE_THRESHOLD}%"}
    direction = "BUY" if signal_data["crossover"] == "UP_CROSS" else "SELL"
    return {
        "should_execute": True,
        "direction": direction,
        "reason": f"信頼度{conf}% ≥ 閾値{AUTO_CONFIDENCE_THRESHOLD}% → 自動実行"
    }

# ==================== RSI 計算 ====================
def calculate_rsi(close_prices, period=14):
    close = pd.Series(close_prices).astype(float)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ==================== Yahoo Finance データ取得 ====================
def fetch_yahoo_data(tf_minutes=None):
    """Yahoo Finance から GOLD データを取得（時間足可変）"""
    if tf_minutes is None:
        tf_minutes = TIMEFRAME_MINUTES
    interval = f"{tf_minutes}m"
    period = "7d" if tf_minutes <= 1 else "60d"
    try:
        ticker = yf.Ticker("GC=F")  # ゴールド先物
        df = ticker.history(period=period, interval=interval)
        if df is None or len(df) < 15:
            print("⚠️  Yahoo Finance: データ不足")
            return None
        df = df.reset_index()
        # カラム名を統一
        df.columns = [c.lower() for c in df.columns]
        for col in ['datetime', 'date']:
            if col in df.columns:
                df = df.rename(columns={col: 'time'})
                break
        for col in ['open', 'high', 'low', 'close']:
            df[col] = df[col].astype(float)
        # タイムゾーン情報を除去して統一
        if hasattr(df['time'].dtype, 'tz') and df['time'].dtype.tz is not None:
            df['time'] = df['time'].dt.tz_localize(None)
        print(f"✓ Yahoo Finance: {len(df)} 本取得 TF={tf_minutes}m 最新={df['time'].iloc[-1]}")
        return df
    except Exception as e:
        print(f"⚠️  Yahoo Finance エラー: {e}")
        return None

# ==================== MACD 計算 ====================
def calculate_macd(close_prices, fast=12, slow=26, signal=9):
    close = pd.Series(close_prices).astype(float)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

# ==================== 追加テクニカル指標 ====================
def calculate_ema(close_prices, period):
    return pd.Series(close_prices).astype(float).ewm(span=period, adjust=False).mean()

def calculate_bollinger(close_prices, period=20, std_dev=2.0):
    close = pd.Series(close_prices).astype(float)
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    return mid + std_dev * std, mid, mid - std_dev * std

def calculate_stochastic(high, low, close, k_period=14, d_period=3):
    h = pd.Series(high).astype(float)
    l = pd.Series(low).astype(float)
    c = pd.Series(close).astype(float)
    lowest = l.rolling(k_period).min()
    highest = h.rolling(k_period).max()
    k = 100 * (c - lowest) / (highest - lowest + 1e-10)
    return k, k.rolling(d_period).mean()

def calculate_adx(high, low, close, period=14):
    h = pd.Series(high).astype(float)
    l = pd.Series(low).astype(float)
    c = pd.Series(close).astype(float)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    up = h.diff()
    down = -l.diff()
    dm_p = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=h.index, dtype=float)
    dm_m = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=h.index, dtype=float)
    atr = tr.ewm(span=period, adjust=False).mean()
    di_p = 100 * dm_p.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    di_m = 100 * dm_m.ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    dx = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-10)
    return dx.ewm(span=period, adjust=False).mean(), di_p, di_m

def calculate_atr(high, low, close, period=14):
    h = pd.Series(high).astype(float)
    l = pd.Series(low).astype(float)
    c = pd.Series(close).astype(float)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

# ==================== 複合シグナル計算 ====================
def compute_signal_composite(df):
    """EMA/MACD/RSI/Stochastic/BB/ADX の7指標スコアリングによる複合シグナル"""
    if df is None or len(df) < 60:
        return None

    close = df['close'].values
    high = df['high'].values if 'high' in df.columns else close
    low = df['low'].values if 'low' in df.columns else close

    rsi_s = calculate_rsi(close, 14)
    macd_line, macd_sig = calculate_macd(close)
    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    long_p = min(200, len(close) - 1)
    ema_long = calculate_ema(close, long_p)
    bb_upper, _, bb_lower = calculate_bollinger(close, 20, 2.0)
    stoch_k, stoch_d = calculate_stochastic(high, low, close, 14, 3)
    adx_s, di_p, di_m = calculate_adx(high, low, close, 14)
    atr_s = calculate_atr(high, low, close, 14)

    def safe(s, idx=-1):
        try:
            v = float(s.iloc[idx])
            return v if not np.isnan(v) else 0.0
        except Exception:
            return 0.0

    cur_rsi = safe(rsi_s)
    cur_macd, cur_macd_sig = safe(macd_line), safe(macd_sig)
    prv_macd, prv_macd_sig = safe(macd_line, -2), safe(macd_sig, -2)
    cur_ema20, cur_ema50, cur_ema_long = safe(ema20), safe(ema50), safe(ema_long)
    cur_close = float(df['close'].iloc[-1])
    cur_bb_upper, cur_bb_lower = safe(bb_upper), safe(bb_lower)
    cur_stoch_k, cur_stoch_d = safe(stoch_k), safe(stoch_d)
    prv_stoch_k, prv_stoch_d = safe(stoch_k, -2), safe(stoch_d, -2)
    cur_adx = safe(adx_s)
    cur_di_p, cur_di_m = safe(di_p), safe(di_m)
    cur_atr = safe(atr_s)

    buy_score = 0
    sell_score = 0
    buy_reasons: list = []
    sell_reasons: list = []

    # 1. EMAトレンド
    if cur_ema20 > cur_ema50:
        buy_score += 1; buy_reasons.append("短期EMA↑")
    else:
        sell_score += 1; sell_reasons.append("短期EMA↓")
    if cur_close > cur_ema_long:
        buy_score += 1; buy_reasons.append("長期EMA上方")
    else:
        sell_score += 1; sell_reasons.append("長期EMA下方")

    # 2. MACD
    if prv_macd < prv_macd_sig and cur_macd > cur_macd_sig:
        buy_score += 2; buy_reasons.append("MACDゴールデンクロス")
    elif prv_macd > prv_macd_sig and cur_macd < cur_macd_sig:
        sell_score += 2; sell_reasons.append("MACDデッドクロス")
    elif cur_macd > cur_macd_sig:
        buy_score += 1; buy_reasons.append("MACD買い優勢")
    else:
        sell_score += 1; sell_reasons.append("MACD売り優勢")

    # 3. RSI
    if cur_rsi < 30:
        buy_score += 2; buy_reasons.append(f"RSI売られすぎ({cur_rsi:.0f})")
    elif 40 <= cur_rsi <= 65:
        buy_score += 1; buy_reasons.append(f"RSI買い圏({cur_rsi:.0f})")
    if cur_rsi > 70:
        sell_score += 2; sell_reasons.append(f"RSI買われすぎ({cur_rsi:.0f})")
    elif 35 <= cur_rsi < 60:
        sell_score += 1; sell_reasons.append(f"RSI売り圏({cur_rsi:.0f})")

    # 4. Stochastic
    if prv_stoch_k < prv_stoch_d and cur_stoch_k > cur_stoch_d:
        buy_score += 2; buy_reasons.append(f"ストキャスGC({cur_stoch_k:.0f})")
    elif prv_stoch_k > prv_stoch_d and cur_stoch_k < cur_stoch_d:
        sell_score += 2; sell_reasons.append(f"ストキャスDC({cur_stoch_k:.0f})")
    elif cur_stoch_k > cur_stoch_d:
        buy_score += 1; buy_reasons.append("ストキャス買い優勢")
    else:
        sell_score += 1; sell_reasons.append("ストキャス売り優勢")

    # 5. ボリンジャーバンド
    bb_range = cur_bb_upper - cur_bb_lower
    if bb_range > 0:
        bb_pos = (cur_close - cur_bb_lower) / bb_range
        if bb_pos < 0.25:
            buy_score += 1; buy_reasons.append("BB下限付近")
        elif bb_pos > 0.75:
            sell_score += 1; sell_reasons.append("BB上限付近")

    # 6. ADX方向
    if cur_di_p > cur_di_m and cur_adx > 20:
        buy_score += 1; buy_reasons.append(f"DI+優勢(ADX{cur_adx:.0f})")
    elif cur_di_m > cur_di_p and cur_adx > 20:
        sell_score += 1; sell_reasons.append(f"DI-優勢(ADX{cur_adx:.0f})")

    THRESHOLD = 3
    crossover = None
    if buy_score >= THRESHOLD and buy_score > sell_score + 1:
        crossover = "UP_CROSS"
    elif sell_score >= THRESHOLD and sell_score > buy_score + 1:
        crossover = "DOWN_CROSS"

    return {
        'rsi': round(cur_rsi, 2),
        'signal_line': round(cur_macd_sig, 4),
        'main_line': round(cur_macd, 4),
        'crossover': crossover,
        'crossover_mode': 'COMPOSITE',
        'latest_close': round(cur_close, 2),
        'time': str(df['time'].iloc[-1]),
        'composite': {
            'buy_score': buy_score,
            'sell_score': sell_score,
            'buy_reasons': buy_reasons,
            'sell_reasons': sell_reasons,
            'ema20': round(cur_ema20, 2),
            'ema50': round(cur_ema50, 2),
            'ema_long': round(cur_ema_long, 2),
            'bb_upper': round(cur_bb_upper, 2),
            'bb_lower': round(cur_bb_lower, 2),
            'stoch_k': round(cur_stoch_k, 2),
            'stoch_d': round(cur_stoch_d, 2),
            'adx': round(cur_adx, 2),
            'di_plus': round(cur_di_p, 2),
            'di_minus': round(cur_di_m, 2),
            'atr': round(cur_atr, 4),
            'is_trending': cur_adx > 25,
        }
    }

# ==================== シグナル計算 ====================
def compute_signal(df):
    if df is None or len(df) < 35:
        return None

    if CROSSOVER_MODE == "COMPOSITE":
        return compute_signal_composite(df)

    close = df['close'].values
    cur_rsi = round(float(calculate_rsi(close).iloc[-1]), 2)

    if CROSSOVER_MODE == "MACD":
        # MACDライン vs MACDシグナルライン のクロス
        macd_line, sig_line = calculate_macd(close)
        cur_main, cur_sig = macd_line.iloc[-1], sig_line.iloc[-1]
        prv_main, prv_sig = macd_line.iloc[-2], sig_line.iloc[-2]

    elif CROSSOVER_MODE == "RSI_MACD":
        # RSI(青) vs MACDシグナル正規化(赤) のクロス
        rsi_series = calculate_rsi(close)
        _, macd_sig = calculate_macd(close)
        # MACDシグナルを0〜100に正規化（直近100本ベース）
        rolling_min = macd_sig.rolling(100, min_periods=20).min()
        rolling_max = macd_sig.rolling(100, min_periods=20).max()
        macd_sig_norm = (macd_sig - rolling_min) / (rolling_max - rolling_min + 1e-10) * 100
        cur_main, cur_sig = rsi_series.iloc[-1], macd_sig_norm.iloc[-1]
        prv_main, prv_sig = rsi_series.iloc[-2], macd_sig_norm.iloc[-2]

    else:  # RSI
        # RSI vs RSIの9期間移動平均 のクロス
        rsi_series = calculate_rsi(close)
        sig_series = pd.Series(rsi_series).rolling(window=9).mean()
        cur_main, cur_sig = rsi_series.iloc[-1], sig_series.iloc[-1]
        prv_main, prv_sig = rsi_series.iloc[-2], sig_series.iloc[-2]

    if any(np.isnan(v) for v in [cur_main, cur_sig, prv_main, prv_sig]):
        return None

    crossover = None
    if prv_main < prv_sig and cur_main > cur_sig:
        crossover = "UP_CROSS"
    elif prv_main > prv_sig and cur_main < cur_sig:
        crossover = "DOWN_CROSS"

    return {
        'rsi': cur_rsi,
        'signal_line': round(float(cur_sig), 4),
        'main_line': round(float(cur_main), 4),
        'crossover': crossover,
        'crossover_mode': CROSSOVER_MODE,
        'latest_close': round(float(df['close'].iloc[-1]), 2),
        'time': str(df['time'].iloc[-1])
    }

# ==================== Gemini ダマシ判定 ====================
def _gemini_should_call(signal):
    """クールダウン中 or 同一シグナルへの重複呼び出しをスキップ"""
    global _last_gemini_call_time, _last_gemini_signal_key
    now = time.time()
    key = f"{signal.get('crossover')}_{signal.get('latest_close')}_{signal.get('time')}"
    if key == _last_gemini_signal_key:
        return False
    if now - _last_gemini_call_time < GEMINI_COOLDOWN_SEC:
        remaining = int(GEMINI_COOLDOWN_SEC - (now - _last_gemini_call_time))
        print(f"⏳ Gemini クールダウン中 残り{remaining}秒 → スキップ")
        return False
    _last_gemini_call_time = now
    _last_gemini_signal_key = key
    return True

def _gemini_generate(prompt, max_retries=1):
    """429クォータエラー時にリトライするGemini呼び出しラッパー"""
    for attempt in range(max_retries + 1):
        try:
            return gemini_model.generate_content(prompt).text.strip()
        except Exception as e:
            err = str(e)
            is_quota = "429" in err or "quota" in err.lower() or "Resource has been exhausted" in err
            if is_quota and attempt < max_retries:
                wait = 15 * (attempt + 1)
                print(f"⏳ Gemini 429クォータ制限 → {wait}秒後リトライ ({attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            if is_quota:
                print(f"❌ Gemini クォータ超過（リトライ限界）: {e}")
                raise Exception("クォータ制限中 - 数分後に自動回復します")
            raise

def gemini_validate(df, signal):
    if signal['crossover'] is None:
        return {'valid': False, 'confidence': 0, 'reason': 'シグナルなし'}
    recent = df.tail(20)[['time', 'open', 'high', 'low', 'close']].copy()
    recent['time'] = recent['time'].astype(str)
    direction = "買い（ロング）" if signal['crossover'] == "UP_CROSS" else "売り（ショート）"
    prompt = f"""
あなたはゴールド（XAUUSD）の専門トレーダーです。
以下の情報を分析し、このシグナルがダマシかどうか判定してください。

シグナル方向: {direction}
RSI: {signal['rsi']}
シグナルライン: {signal['signal_line']}
直近終値: {signal['latest_close']}
直近20本の価格データ（M30）:
{recent.to_dict(orient='records')}

以下の JSON のみで回答してください：
{{"valid": true or false, "confidence": 0-100, "reason": "50文字以内"}}
"""
    try:
        text = _gemini_generate(prompt)
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            'valid': bool(result.get('valid', False)),
            'confidence': int(result.get('confidence', 0)),
            'reason': str(result.get('reason', ''))
        }
    except Exception as e:
        print(f"❌ Gemini エラー詳細: {e}")
        return {'valid': False, 'confidence': 0, 'reason': str(e)[:200]}

def gemini_composite_analyze(df, signal):
    """COMPOSITEモード専用の深層Gemini分析（SL/TP提案付き）"""
    if signal['crossover'] is None:
        return {'valid': False, 'confidence': 0, 'reason': 'シグナルなし',
                'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}
    comp = signal.get('composite', {})
    direction = "買い（ロング）" if signal['crossover'] == "UP_CROSS" else "売り（ショート）"
    recent = df.tail(20)[['time', 'open', 'high', 'low', 'close']].copy()
    recent['time'] = recent['time'].astype(str)
    prompt = f"""あなたはゴールド（XAUUSD）の上級テクニカルアナリストです。
複合テクニカル指標を総合分析し、このシグナルの有効性を判定してください。

【シグナル】方向: {direction} / 価格: {signal['latest_close']} / 時刻: {signal['time']}
【スコア】買い{comp.get('buy_score',0)}点 vs 売り{comp.get('sell_score',0)}点
買い根拠: {', '.join(comp.get('buy_reasons', []))}
売り根拠: {', '.join(comp.get('sell_reasons', []))}
【指標】EMA20={comp.get('ema20')} EMA50={comp.get('ema50')} EMA長={comp.get('ema_long')}
RSI={signal['rsi']} MACD={signal['main_line']} MACDシグナル={signal['signal_line']}
ストキャスK={comp.get('stoch_k')} D={comp.get('stoch_d')}
BB上={comp.get('bb_upper')} BB下={comp.get('bb_lower')}
ADX={comp.get('adx')} DI+={comp.get('di_plus')} DI-={comp.get('di_minus')} ATR={comp.get('atr')}
【直近20本価格（M30）】{json.dumps(recent.to_dict(orient='records'), ensure_ascii=False)}

以下のJSON形式のみで回答:
{{"valid": true/false, "confidence": 0-100, "reason": "100文字以内", "sl_suggestion": SL価格(数値)またはnull, "tp_suggestion": TP価格(数値)またはnull, "key_level": "注目水準50文字以内"}}"""
    try:
        text = _gemini_generate(prompt)
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            'valid': bool(result.get('valid', False)),
            'confidence': int(result.get('confidence', 0)),
            'reason': str(result.get('reason', '')),
            'sl_suggestion': result.get('sl_suggestion'),
            'tp_suggestion': result.get('tp_suggestion'),
            'key_level': str(result.get('key_level', '')),
        }
    except Exception as e:
        print(f"❌ Gemini Composite エラー: {e}")
        return {'valid': False, 'confidence': 0, 'reason': str(e)[:200],
                'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}

# ==================== Supabase 保存 ====================
def save_signal_to_supabase(signal_data):
    """シグナルをSupabaseに保存し、挿入されたIDを返す"""
    try:
        tf = signal_data.get('timeframe', TIMEFRAME_MINUTES)
        resp = req.post(
            f"{SUPABASE_URL}/rest/v1/signals",
            json={
                "symbol": "GOLD",
                "timeframe": f"M{tf}",
                "timeframe_minutes": tf,
                "test_mode": signal_data.get('test_mode', TEST_MODE),
                "crossover_mode": signal_data.get('crossover_mode', CROSSOVER_MODE),
                "crossover": signal_data.get('crossover'),
                "rsi": signal_data.get('rsi'),
                "signal_line": signal_data.get('signal_line'),
                "main_line": signal_data.get('main_line'),
                "latest_close": signal_data.get('latest_close'),
                "ai_valid": signal_data.get('ai_valid'),
                "ai_confidence": signal_data.get('ai_confidence'),
                "ai_reason": signal_data.get('ai_reason'),
                "created_at": signal_data.get('generated_at', datetime.now(timezone.utc).isoformat())
            },
            headers={**supabase_headers(), "Prefer": "return=representation"},
            timeout=10
        )
        resp.raise_for_status()
        rows = resp.json()
        db_id = rows[0]['id'] if rows else None
        print(f"✓ Supabase 保存完了 id={db_id}")
        return db_id
    except Exception as e:
        print(f"⚠️  Supabase 保存エラー: {e}")
        return None

# ==================== シグナルループ（24/7自動稼働）====================
def signal_loop():
    """Yahoo Finance からデータ取得し、時間足ごとにシグナルを計算・配信"""
    print(f"🔄 シグナルループ開始 TF={TIMEFRAME_MINUTES}m MODE={'TEST' if TEST_MODE else 'PROD'}")
    while True:
        try:
            tf = TIMEFRAME_MINUTES
            df = fetch_yahoo_data(tf)
            signal = compute_signal(df)

            if signal is None:
                print("⚠️  シグナル計算失敗。スキップします。")
                settings_changed.wait(timeout=tf * 60)
                settings_changed.clear()
                continue

            if signal['crossover']:
                print(f"🎯 クロスオーバー検出: {signal['crossover']}")
                if _gemini_should_call(signal):
                    if CROSSOVER_MODE == "COMPOSITE":
                        ai = gemini_composite_analyze(df, signal)
                    else:
                        ai = gemini_validate(df, signal)
                else:
                    ai = {'valid': None, 'confidence': None, 'reason': 'クールダウン中（重複スキップ）',
                          'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}
                if TEST_MODE:
                    print(f"🧪 TEST: RSI={signal['rsi']}, TF={tf}m, AI={ai['valid']}({ai['confidence']}%)")
            else:
                ai = {'valid': None, 'confidence': None, 'reason': None,
                      'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}
                print(f"⏸️  クロスオーバーなし RSI={signal['rsi']} TF={tf}m")

            signal_data = {
                **signal,
                'ai_valid': ai['valid'],
                'ai_confidence': ai['confidence'],
                'ai_reason': ai['reason'],
                'ai_sl_suggestion': ai.get('sl_suggestion'),
                'ai_tp_suggestion': ai.get('tp_suggestion'),
                'ai_key_level': ai.get('key_level', ''),
                'timeframe': tf,
                'test_mode': TEST_MODE,
                'generated_at': datetime.now(timezone.utc).isoformat()
            }

            # Supabase保存 → db_id取得後に配信（1回のみ）
            db_id = save_signal_to_supabase(signal_data)
            if db_id:
                signal_data['db_id'] = db_id

            # FULL_AUTO モード: 信頼度が閾値以上なら自動実行
            auto_result = check_auto_execution(signal_data)
            if auto_result["should_execute"]:
                print(f"🤖 FULL_AUTO実行: {auto_result['reason']}")
                mt5_result = send_mt5_order(
                    signal_id=db_id,
                    direction=auto_result["direction"],
                    entry_price=signal_data["latest_close"]
                )
                signal_data["auto_executed"] = mt5_result.get("success", False)
                signal_data["auto_result"] = mt5_result
            else:
                signal_data["auto_executed"] = False

            signal_data["trading_mode"] = TRADING_MODE

            socketio.emit('signal', signal_data)
            print(f"📡 シグナル配信完了: close={signal_data['latest_close']} db_id={db_id} mode={TRADING_MODE}")

            if signal_data.get('crossover'):
                # Geminiが実際に分析した場合のみFCM送信（0%クォータ通知を排除）
                ai_conf = signal_data.get('ai_confidence')
                ai_reason = signal_data.get('ai_reason', '')
                gemini_ran = (ai_conf is not None and 'クールダウン' not in ai_reason and 'クォータ' not in ai_reason)
                if gemini_ran:
                    send_fcm_push(signal_data)

        except Exception as e:
            print(f"❌ シグナルループエラー: {e}")

        # 設定変更イベントがセットされたら即座にループ再開、なければ通常スリープ
        settings_changed.wait(timeout=TIMEFRAME_MINUTES * 60)
        settings_changed.clear()

# ==================== FCM プッシュ送信 ====================
def send_fcm_push(signal_data):
    if not FCM_ENABLED or not fcm_tokens:
        return []
    direction = "📈 買いシグナル" if signal_data.get('crossover') == "UP_CROSS" else "📉 売りシグナル"
    confidence = signal_data.get('ai_confidence')
    reason = signal_data.get('ai_reason', '')
    sl = signal_data.get('ai_sl_suggestion')
    tp = signal_data.get('ai_tp_suggestion')
    sl_tp = f" | SL:{sl} TP:{tp}" if sl and tp else ""
    if confidence is not None:
        body = f"{'✅' if signal_data.get('ai_valid') else '⚠️'} 信頼度:{confidence}% {reason}{sl_tp}"
    else:
        body = f"シグナル検出{sl_tp}"
    invalid_tokens = set()
    results = []
    for token in list(fcm_tokens):
        try:
            msg = messaging.Message(
                notification=messaging.Notification(
                    title=f"GOLD {direction}",
                    body=body,
                ),
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id="gold-trading",
                        notification_count=1,
                        vibrate_timings_millis=[0, 2000, 300, 2000, 300, 2000, 300, 2000, 300, 2000],
                        default_vibrate_timings=False,
                    ),
                ),
                token=token,
            )
            msg_id = messaging.send(msg)
            print(f"✓ FCMプッシュ送信完了 msg_id={msg_id}: {token[:20]}...")
            results.append({"token": token[:20], "status": "ok", "msg_id": msg_id})
        except Exception as e:
            print(f"⚠️  FCM送信エラー ({token[:20]}...): {e}")
            results.append({"token": token[:20], "status": "error", "error": str(e)})
            if "registration-token-not-registered" in str(e) or "invalid-argument" in str(e):
                invalid_tokens.add(token)
    fcm_tokens.difference_update(invalid_tokens)
    for t in invalid_tokens:
        delete_fcm_token(t)
    return results

# ==================== REST エンドポイント ====================
@app.route("/push-signal", methods=["POST"])
def push_signal():
    """PCからの手動プッシュ（オプション）"""
    if request.headers.get("X-Push-Secret", "") != PUSH_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    data['received_at'] = datetime.now(timezone.utc).isoformat()
    socketio.emit('signal', data)
    save_signal_to_supabase(data)
    return jsonify({"status": "ok"})

@app.route("/test-fcm", methods=["POST"])
def test_fcm():
    """FCMプッシュ通知テスト"""
    if request.headers.get("X-Push-Secret", "") != PUSH_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    token_count = len(fcm_tokens)
    if token_count == 0:
        return jsonify({"status": "no_tokens", "message": "FCMトークン未登録"})
    results = send_fcm_push({
        "crossover": "UP_CROSS",
        "latest_close": 4153.30,
        "ai_valid": True,
        "ai_confidence": 99,
        "ai_reason": "FCMテスト通知 - 画面オフでも届いてますか？",
        "test_mode": True,
    })
    return jsonify({"status": "sent", "tokens": token_count, "results": results})

@app.route("/register-token", methods=["POST", "OPTIONS"])
def register_token():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    token = data.get("token") if data else None
    if not token:
        return jsonify({"error": "No token"}), 400
    fcm_tokens.add(token)
    persist_fcm_token(token)
    print(f"📱 FCMトークン登録: {token[:20]}... (合計: {len(fcm_tokens)}台)")
    return jsonify({"status": "ok"})

@app.route("/api/settings/timeframe", methods=["POST", "OPTIONS"])
def set_timeframe():
    global TIMEFRAME_MINUTES
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    tf = int(data.get("timeframe", 30))
    if tf not in [1, 5, 15, 30, 60]:
        return jsonify({"error": f"Invalid timeframe: {tf}"}), 400
    TIMEFRAME_MINUTES = tf
    settings_changed.set()
    print(f"📊 時間足変更: {tf}分足（ループ即座再開）")
    return jsonify({"status": "ok", "timeframe": tf})

@app.route("/api/settings/mode", methods=["POST", "OPTIONS"])
def set_mode():
    global TEST_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    mode = data.get("mode", "PRODUCTION")
    TEST_MODE = (mode == "TEST")
    settings_changed.set()
    print(f"{'🧪 TEST_MODE ON' if TEST_MODE else '🚀 PRODUCTION ON'}（ループ即座再開）")
    return jsonify({"status": "ok", "mode": mode, "test_mode": TEST_MODE})

@app.route("/api/settings/crossover", methods=["POST", "OPTIONS"])
def set_crossover():
    global CROSSOVER_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    mode = data.get("crossover_mode", "RSI")
    if mode not in ["RSI", "MACD", "RSI_MACD", "COMPOSITE"]:
        return jsonify({"error": f"Invalid crossover_mode: {mode}"}), 400
    CROSSOVER_MODE = mode
    settings_changed.set()
    print(f"📊 クロスオーバー方式変更: {mode}（ループ即座再開）")
    return jsonify({"status": "ok", "crossover_mode": mode})

@app.route("/api/settings/trading-mode", methods=["POST", "OPTIONS"])
def set_trading_mode():
    global TRADING_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    mode = data.get("trading_mode", "MANUAL")
    if mode not in ["MANUAL", "SEMI_AUTO", "FULL_AUTO"]:
        return jsonify({"error": f"Invalid trading_mode: {mode}"}), 400
    TRADING_MODE = mode
    print(f"🔄 トレードモード変更: {mode}")
    return jsonify({"status": "ok", "trading_mode": mode})

@app.route("/api/settings/auto-threshold", methods=["POST", "OPTIONS"])
def set_auto_threshold():
    global AUTO_CONFIDENCE_THRESHOLD
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    threshold = int(data.get("threshold", 70))
    if not (0 <= threshold <= 100):
        return jsonify({"error": "threshold must be 0-100"}), 400
    AUTO_CONFIDENCE_THRESHOLD = threshold
    print(f"🎯 自動実行閾値変更: {threshold}%")
    return jsonify({"status": "ok", "threshold": threshold})

@app.route("/api/execute-order", methods=["POST", "OPTIONS"])
def execute_order():
    """SEMI_AUTO: スマホのボタンから手動トリガーで MT5 に注文送信"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    signal_id = data.get("signal_id")
    direction = data.get("direction", "BUY")
    entry_price = float(data.get("entry_price", 0))
    sl_pips = int(data.get("sl_pips", 20))
    tp_pips = int(data.get("tp_pips", 40))

    if not entry_price:
        return jsonify({"error": "entry_price required"}), 400
    if direction not in ["BUY", "SELL"]:
        return jsonify({"error": "direction must be BUY or SELL"}), 400

    result = send_mt5_order(signal_id, direction, entry_price, sl_pips, tp_pips)
    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code

@app.route("/api/settings/current", methods=["GET"])
def get_current_settings():
    return jsonify({
        "timeframe": TIMEFRAME_MINUTES,
        "mode": "TEST" if TEST_MODE else "PRODUCTION",
        "test_mode": TEST_MODE,
        "crossover_mode": CROSSOVER_MODE,
        "trading_mode": TRADING_MODE,
        "auto_confidence_threshold": AUTO_CONFIDENCE_THRESHOLD,
        "mt5_webhook_configured": bool(MT5_WEBHOOK_URL)
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(), "mode": "yahoo-finance"})

@app.route("/latest-signal", methods=["GET"])
def latest_signal():
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            params={"select": "*", "order": "created_at.desc", "limit": "1"},
            headers=supabase_headers(),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return jsonify(data[0] if data else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/signal-history", methods=["GET"])
def signal_history():
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            params={"select": "*", "order": "created_at.desc", "limit": "50"},
            headers=supabase_headers(),
            timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== トレード管理 API ====================

@app.route("/api/trades", methods=["POST", "OPTIONS"])
def create_trade():
    """手動注文を記録"""
    global CROSSOVER_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    try:
        payload = {
            "signal_id": data.get("signal_id"),
            "direction": data.get("direction", "BUY"),
            "entry_price": float(data["entry_price"]),
            "entry_time": data.get("entry_time", datetime.now(timezone.utc).isoformat()),
            "status": "OPEN",
        }
        resp = req.post(
            f"{SUPABASE_URL}/rest/v1/trades",
            json=payload,
            headers={**supabase_headers(), "Prefer": "return=representation"},
            timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json()[0]), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/trades/<int:trade_id>", methods=["PATCH", "OPTIONS"])
def close_trade(trade_id):
    """トレードを決済（PATCH）"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    try:
        exit_price = float(data["exit_price"])
        # 既存トレードを取得してentry_priceを取る
        existing = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"id": f"eq.{trade_id}", "select": "entry_price,direction"},
            headers=supabase_headers(), timeout=10
        ).json()
        if not existing:
            return jsonify({"error": "Trade not found"}), 404
        entry_price = float(existing[0]['entry_price'])
        direction = existing[0].get('direction', 'BUY')
        profit_loss = round((exit_price - entry_price) * (1 if direction == 'BUY' else -1), 2)
        pips = round(profit_loss * 10, 1)  # GOLD: 1$ = 10 pips
        status = "CLOSED_PROFIT" if profit_loss >= 0 else "CLOSED_LOSS"

        payload = {
            "exit_price": exit_price,
            "exit_time": data.get("exit_time", datetime.now(timezone.utc).isoformat()),
            "profit_loss": profit_loss,
            "pips": pips,
            "status": data.get("status", status),
            "notes": data.get("notes", ""),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        resp = req.patch(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"id": f"eq.{trade_id}"},
            json=payload,
            headers={**supabase_headers(), "Prefer": "return=representation"},
            timeout=10
        )
        resp.raise_for_status()
        return jsonify(resp.json()[0])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/trades", methods=["GET"])
def get_trades():
    """トレード一覧取得"""
    status_filter = request.args.get("status")
    params = {"select": "*,signals(crossover,rsi,ai_confidence,timeframe_minutes)",
              "order": "created_at.desc", "limit": "50"}
    if status_filter:
        params["status"] = f"eq.{status_filter}"
    try:
        resp = req.get(f"{SUPABASE_URL}/rest/v1/trades",
                       params=params, headers=supabase_headers(), timeout=10)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats/today", methods=["GET"])
def stats_today():
    """本日の統計"""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        trades_resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"select": "status,profit_loss,pips,signals(ai_confidence)",
                    "entry_time": f"gte.{today}T00:00:00Z"},
            headers=supabase_headers(), timeout=10
        )
        trades = trades_resp.json() if trades_resp.ok else []
        signals_resp = req.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            params={"select": "id,crossover", "created_at": f"gte.{today}T00:00:00Z"},
            headers=supabase_headers(), timeout=10
        )
        signals = signals_resp.json() if signals_resp.ok else []
        closed = [t for t in trades if t['status'] in ('CLOSED_PROFIT', 'CLOSED_LOSS')]
        wins = [t for t in closed if t['status'] == 'CLOSED_PROFIT']
        total_pips = sum(t.get('pips') or 0 for t in closed)
        conf_values = [t['signals']['ai_confidence'] for t in closed
                       if t.get('signals') and t['signals'].get('ai_confidence')]
        return jsonify({
            "date": today,
            "total_signals": len(signals),
            "total_trades": len(trades),
            "open_trades": len([t for t in trades if t['status'] == 'OPEN']),
            "win_count": len(wins),
            "loss_count": len(closed) - len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pips": round(total_pips, 1),
            "avg_profit": round(sum(t.get('profit_loss') or 0 for t in closed) / len(closed), 2) if closed else 0,
            "confidence_avg": round(sum(conf_values) / len(conf_values), 1) if conf_values else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats/history", methods=["GET"])
def stats_history():
    """過去N日の日別統計"""
    days = int(request.args.get("days", 30))
    try:
        from datetime import timedelta
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        trades_resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"select": "status,profit_loss,pips,entry_time",
                    "entry_time": f"gte.{start}", "order": "entry_time.asc"},
            headers=supabase_headers(), timeout=10
        )
        trades = trades_resp.json() if trades_resp.ok else []
        # 日別集計
        from collections import defaultdict
        daily: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "pips": 0.0})
        for t in trades:
            day = t['entry_time'][:10]
            daily[day]["trades"] += 1
            if t['status'] == 'CLOSED_PROFIT':
                daily[day]["wins"] += 1
                daily[day]["pips"] += t.get('pips') or 0
            elif t['status'] == 'CLOSED_LOSS':
                daily[day]["pips"] += t.get('pips') or 0
        result = []
        for day in sorted(daily.keys()):
            d = daily[day]
            closed = d["trades"]
            result.append({
                "date": day,
                "total_trades": closed,
                "win_count": d["wins"],
                "win_rate": round(d["wins"] / closed * 100, 1) if closed else 0,
                "total_pips": round(d["pips"], 1),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/analysis/correlation", methods=["GET"])
def correlation_analysis():
    """信頼度 vs 勝率の相関分析"""
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"select": "status,signals(ai_confidence)",
                    "status": "in.(CLOSED_PROFIT,CLOSED_LOSS)"},
            headers=supabase_headers(), timeout=10
        )
        trades = resp.json() if resp.ok else []
        from collections import defaultdict
        buckets: dict = defaultdict(lambda: {"total": 0, "wins": 0})
        for t in trades:
            conf = t.get('signals', {}) and t['signals'].get('ai_confidence')
            if conf is None:
                continue
            bucket = (conf // 10) * 10  # 10%刻み
            buckets[bucket]["total"] += 1
            if t['status'] == 'CLOSED_PROFIT':
                buckets[bucket]["wins"] += 1
        data_points = []
        for b in sorted(buckets.keys()):
            total = buckets[b]["total"]
            wins = buckets[b]["wins"]
            data_points.append({
                "confidence": b,
                "win_rate": round(wins / total * 100, 1) if total else 0,
                "sample_count": total
            })
        # 相関係数（データが十分あれば計算）
        corr = None
        if len(data_points) >= 3:
            xs = [p["confidence"] for p in data_points]
            ys = [p["win_rate"] for p in data_points]
            n = len(xs)
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            den = (sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)) ** 0.5
            corr = round(num / den, 2) if den else None
        total_closed = len(trades)
        recommendation = "データ収集中..." if total_closed < 20 else (
            f"信頼度{max(data_points, key=lambda p: p['win_rate'])['confidence']}%以上で勝率最高"
            if data_points else "データ不足"
        )
        return jsonify({
            "correlation_coefficient": corr,
            "data_points": data_points,
            "total_samples": total_closed,
            "recommendation": recommendation
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== WebSocket イベント ====================
@socketio.on("connect")
def on_connect():
    print(f"📱 クライアント接続: {threading.current_thread().name}")

@socketio.on("disconnect")
def on_disconnect():
    print("📴 クライアント切断")

# ==================== 起動 ====================
if __name__ == "__main__":
    load_fcm_tokens()
    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
