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
_last_gemini_direction = ""    # 最後にGeminiを呼んだクロス方向
_last_gemini_approved = False  # 最後のGemini結果が承認だったか
GEMINI_COOLDOWN_REJECTED = 120   # 却下後クールダウン：トレンド相場（秒）
GEMINI_COOLDOWN_RANGING  = 600   # 却下後クールダウン：レンジ相場 ADX<25（秒）
ADX_TREND_THRESHOLD      = 25.0  # これ以上でトレンド判定
POSITION_MONITOR_INTERVAL = 1800  # ポジション監視間隔: 30分

# EA 状態追跡（監視用）
_server_start_time = time.time()
_ea_last_heartbeat = 0.0
_ea_last_heartbeat_str = ""
_ea_trades: list = []  # 直近50件のEA取引レポート
_position_monitor_last = 0.0  # ポジション監視最終実行時刻
_last_ea_signal_time = 0.0   # EAから/ea-signalを最後に受信した時刻（signal_loopスキップ判定用）
_ea_latest_scores: dict = {}  # EAから受信した最新スコア（ハートビート経由）
_ea_signal_dedup: dict = {}  # 重複防止キャッシュ {crossover: last_time}

# デモ用ルールベースエントリー設定
DEMO_RULE_BASED = os.environ.get("DEMO_RULE_BASED", "false").lower() == "true"
DEMO_RULE_MIN_SCORE = int(os.environ.get("DEMO_RULE_MIN_SCORE", "4"))  # ルールベース発動の最低スコア
DEMO_RULE_MIN_ADX   = float(os.environ.get("DEMO_RULE_MIN_ADX", "20.0"))  # ルールベース発動の最低ADX

# Status Logging設定
STATUS_LOG_INTERVAL = 300  # 5分ごと
_status_log_thread = None

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
    """Supabaseから起動時にFCMトークンを復元"""
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/fcm_tokens",
            params={"select": "token"},
            headers=supabase_headers(),
            timeout=10
        )
        if resp.ok:
            before = len(fcm_tokens)
            for row in resp.json():
                fcm_tokens.add(row["token"])
            added = len(fcm_tokens) - before
            print(f"✓ FCMトークン復元: Supabase={added}件 / メモリ合計={len(fcm_tokens)}件")
        else:
            print(f"⚠️  FCMトークン読み込みエラー: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"⚠️  FCMトークン読み込みエラー: {e}")

def persist_fcm_token(token: str):
    """FCMトークンをSupabaseに保存"""
    try:
        req.post(
            f"{SUPABASE_URL}/rest/v1/fcm_tokens",
            json={"token": token, "updated_at": datetime.now(timezone.utc).isoformat()},
            headers={**supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            timeout=10
        )
    except Exception as e:
        print(f"⚠️  FCMトークン保存エラー: {e}")

def delete_fcm_token(token: str):
    """無効なFCMトークンをSupabaseから削除"""
    try:
        req.delete(
            f"{SUPABASE_URL}/rest/v1/fcm_tokens",
            params={"token": f"eq.{token}"},
            headers=supabase_headers(),
            timeout=10
        )
    except Exception as e:
        print(f"⚠️  FCMトークン削除エラー: {e}")

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
    """
    ・方向変化              → 必ずGemini呼ぶ
    ・同方向OK済み          → 次の方向変化まで待機（呼ばない）
    ・同方向NG + ADX≥25    → クールダウン無視で即実行（トレンド発生）
    ・同方向NG + ADX<25    → 10分クールダウン（レンジ相場・クォータ節約）
    """
    global _last_gemini_call_time, _last_gemini_signal_key, _last_gemini_direction, _last_gemini_approved
    now = time.time()
    direction = signal.get('crossover', '')
    key = f"{direction}_{signal.get('latest_close')}_{signal.get('time')}"

    if key == _last_gemini_signal_key:
        return False

    # ADX取得（COMPOSITEモードのみ有効。他モードはトレンド扱い）
    cur_adx = float(signal.get('composite', {}).get('adx', 99.0)) if signal.get('composite') else 99.0
    is_trending = cur_adx >= ADX_TREND_THRESHOLD

    # 方向が変わった → 必ずGemini呼ぶ
    if direction != _last_gemini_direction and direction:
        print(f"🔄 方向変化({_last_gemini_direction}→{direction}) → Gemini実行")
        _last_gemini_call_time = now
        _last_gemini_signal_key = key
        _last_gemini_direction = direction
        _last_gemini_approved = False
        return True

    # 同方向・前回承認済み → ポジション保有中 → 次の方向変化まで待機
    if _last_gemini_approved:
        print(f"✅ 承認済みポジション保有中 → Geminiスキップ")
        return False

    # ADX≥25（トレンド発生）→ クールダウン残り無視で即実行
    if is_trending:
        print(f"📈 ADX{cur_adx:.0f}≥{ADX_TREND_THRESHOLD:.0f} トレンド発生 → クールダウン無視でGemini実行")
        _last_gemini_call_time = now
        _last_gemini_signal_key = key
        return True

    # ADX<25（レンジ相場）→ 10分クールダウン
    if now - _last_gemini_call_time < GEMINI_COOLDOWN_RANGING:
        remaining = int(GEMINI_COOLDOWN_RANGING - (now - _last_gemini_call_time))
        print(f"⏳ レンジ相場クールダウン中(ADX{cur_adx:.0f}<{ADX_TREND_THRESHOLD:.0f}) 残り{remaining}秒 → スキップ")
        return False

    print(f"🔁 前回却下 → 再試行（ADX{cur_adx:.0f}）")
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

    # 過去取引実績をプロンプトに追加
    stats = get_recent_trade_stats()
    trade_context = ""
    if stats and stats['total'] > 0:
        trade_context = f"""
【過去{stats['total']}件の取引実績】
勝率: {stats['win_rate']}%（{stats['wins']}勝{stats['losses']}敗）/ 累計損益: {stats['total_pl']:+.0f}円
直近5件: {stats['recent5']}
※ 負けが続いている場合は特に慎重に判定してください。"""

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
【直近20本価格（M15）】{json.dumps(recent.to_dict(orient='records'), ensure_ascii=False)}{trade_context}

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

# ==================== EA リアルタイムシグナル Gemini 分析（Phase 9）====================
def gemini_analyze_ea_signal(ea_data):
    """EAからのMT5リアルタイム指標データをGemini分析（Yahoo Finance不要）"""
    crossover = ea_data.get('crossover', '')
    if not crossover:
        return {'valid': False, 'confidence': 0, 'reason': 'シグナルなし',
                'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}
    direction = "買い（ロング）" if crossover == "UP_CROSS" else "売り（ショート）"

    # 過去取引実績をプロンプトに追加
    stats = get_recent_trade_stats()
    trade_context = ""
    if stats and stats['total'] > 0:
        trade_context = f"""
【過去{stats['total']}件の取引実績】
勝率: {stats['win_rate']}%（{stats['wins']}勝{stats['losses']}敗）/ 累計損益: {stats['total_pl']:+.0f}$
直近5件: {stats['recent5']}
※ 負けが続いている場合は特に慎重に判定してください。"""

    prompt = f"""あなたはゴールド（XAUUSD）の上級テクニカルアナリストです。
MT5のリアルタイムデータから計算された複合テクニカル指標を総合分析し、このシグナルの有効性を判定してください。

【シグナル】方向: {direction} / 価格: {ea_data.get('latest_close')}
【スコア】買い{ea_data.get('buy_score', 0)}点 vs 売り{ea_data.get('sell_score', 0)}点
買い根拠: {ea_data.get('buy_reasons', '')}
売り根拠: {ea_data.get('sell_reasons', '')}
【指標（MT5リアルタイム）】
EMA20={ea_data.get('ema20')} EMA50={ea_data.get('ema50')} EMA200={ea_data.get('ema_long')}
RSI={ea_data.get('rsi')} MACD={ea_data.get('macd')} MACDシグナル={ea_data.get('macd_signal')}
ストキャスK={ea_data.get('stoch_k')} D={ea_data.get('stoch_d')}
BB上={ea_data.get('bb_upper')} BB下={ea_data.get('bb_lower')}
ADX={ea_data.get('adx')} DI+={ea_data.get('di_plus')} DI-={ea_data.get('di_minus')} ATR={ea_data.get('atr')}{trade_context}

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
        err_str = str(e)
        print(f"❌ Gemini EAシグナル分析エラー: {err_str[:100]}")
        is_quota = "クォータ制限中" in err_str or "429" in err_str or "quota" in err_str.lower()
        if is_quota:
            # クォータ時: valid=None/confidence=None で「判定不能」扱い（ダマシ扱いしない）
            return {'valid': None, 'confidence': None, 'reason': 'クォータ制限中 - 数分後に自動回復します',
                    'sl_suggestion': None, 'tp_suggestion': None, 'key_level': '', 'quota_error': True}
        return {'valid': False, 'confidence': 0, 'reason': err_str[:200],
                'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}

# ==================== ポジション監視 Gemini 評価（Phase 7）====================
def gemini_monitor_position(position, df):
    """保有ポジションの危険度をGeminiが評価する（Phase 7）"""
    direction = position.get('direction', 'BUY')
    entry_price = float(position.get('entry_price', 0))
    sl = position.get('sl')
    tp = position.get('tp')
    entry_time = position.get('entry_time', '')
    current_price = float(df['close'].iloc[-1])
    unrealized_pl = round(
        (current_price - entry_price) * (1 if direction == 'BUY' else -1), 2
    )

    comp = compute_signal_composite(df)
    comp_data = comp.get('composite', {}) if comp else {}
    direction_ja = "買い（ロング）" if direction == 'BUY' else "売り（ショート）"
    recent = df.tail(20)[['time', 'open', 'high', 'low', 'close']].copy()
    recent['time'] = recent['time'].astype(str)

    prompt = f"""あなたはゴールド（XAUUSD）の上級リスク管理の専門家です。
現在保有中のポジションを分析し、リスクを評価してください。

【保有ポジション】
方向: {direction_ja}
エントリー価格: {entry_price}
現在価格: {current_price}
含み損益: {unrealized_pl:+.2f}ドル
SL: {sl or '未設定'} / TP: {tp or '未設定'}
エントリー時刻: {entry_time}

【テクニカル指標（最新）】
RSI={comp.get('rsi') if comp else 'N/A'} / ADX={comp_data.get('adx', 'N/A')}
EMA20={comp_data.get('ema20', 'N/A')} / EMA50={comp_data.get('ema50', 'N/A')}
買いスコア: {comp_data.get('buy_score', 'N/A')} vs 売りスコア: {comp_data.get('sell_score', 'N/A')}
買い根拠: {', '.join(comp_data.get('buy_reasons', []))}
売り根拠: {', '.join(comp_data.get('sell_reasons', []))}
【直近20本の価格データ（M15）】{json.dumps(recent.to_dict(orient='records'), ensure_ascii=False)}

以下のJSON形式のみで回答してください:
{{"risk": "HIGH/MEDIUM/LOW", "action": "CLOSE/HOLD", "reason": "100文字以内"}}
- HIGH: 即座に決済を強く推奨（逆行リスク大・SLブレイクの危険）
- MEDIUM: 注意が必要（状況悪化の可能性、監視継続）
- LOW: ポジション維持問題なし"""

    try:
        text = _gemini_generate(prompt, max_retries=0)
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            'risk': str(result.get('risk', 'LOW')).upper(),
            'action': str(result.get('action', 'HOLD')).upper(),
            'reason': str(result.get('reason', '')),
            'current_price': current_price,
            'unrealized_pl': unrealized_pl,
        }
    except Exception as e:
        print(f"❌ Geminiポジション監視エラー: {e}")
        return {
            'risk': 'LOW', 'action': 'HOLD',
            'reason': f"評価エラー: {str(e)[:80]}",
            'current_price': current_price,
            'unrealized_pl': unrealized_pl,
        }

# ==================== 取引実績統計（Geminiプロンプト用） ====================
def get_recent_trade_stats(limit=20):
    """Supabaseから直近の取引結果を取得して統計を返す"""
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={
                "status": "neq.OPEN",
                "order": "entry_time.desc",
                "limit": limit,
                "select": "direction,profit_loss,pips,status,entry_time"
            },
            headers=supabase_headers(),
            timeout=5
        )
        if resp.status_code != 200 or not resp.json():
            return None
        trades = resp.json()
        wins   = [t for t in trades if (t.get('profit_loss') or 0) > 0]
        losses = [t for t in trades if (t.get('profit_loss') or 0) <= 0]
        total_pl   = sum(t.get('profit_loss') or 0 for t in trades)
        win_rate   = round(len(wins) / len(trades) * 100, 1) if trades else 0
        recent5    = [{"dir": t.get('direction',''), "pl": round(t.get('profit_loss') or 0, 0),
                       "pips": round(t.get('pips') or 0, 1)} for t in trades[:5]]
        return {
            "total": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": win_rate, "total_pl": round(total_pl, 0), "recent5": recent5
        }
    except Exception as e:
        print(f"⚠️ 取引統計取得エラー: {e}")
        return None

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

# ==================== Status Logging（ダッシュボード用）====================
def status_logging_loop():
    """5分ごとに /status データを Supabase に記録"""
    while True:
        try:
            time.sleep(STATUS_LOG_INTERVAL)

            now = time.time()
            ea_alive = (_ea_last_heartbeat > 0 and now - _ea_last_heartbeat < 90)

            status_log = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "server_uptime_hours": round((now - _server_start_time) / 3600, 1),
                "settings_timeframe": TIMEFRAME_MINUTES,
                "settings_crossover_mode": CROSSOVER_MODE,
                "settings_trading_mode": TRADING_MODE,
                "test_mode": TEST_MODE,
                "gemini_state": "承認済み" if _last_gemini_approved else "待機中",
                "gemini_last_direction": _last_gemini_direction or "なし",
                "gemini_approved": _last_gemini_approved,
                "gemini_last_call_ago_sec": round(now - _last_gemini_call_time) if _last_gemini_call_time else None,
                "ea_alive": ea_alive,
                "ea_last_heartbeat_ago_sec": round(now - _ea_last_heartbeat) if _ea_last_heartbeat else None,
                "ea_last_signal_push_ago_sec": round(now - _last_ea_signal_time) if _last_ea_signal_time else None,
                "ea_signal_loop_mode": "EA_PUSH" if ea_alive else "Yahoo Finance",
                "ea_buy_score": _ea_latest_scores.get("buy_score"),
                "ea_sell_score": _ea_latest_scores.get("sell_score"),
                "ea_adx": _ea_latest_scores.get("adx"),
                "fcm_token_count": len(fcm_tokens),
                "recent_trade_count": len(_ea_trades),
                "recent_trades_json": _ea_trades[-5:],
                "total_signals_today": 0,
                "total_trades_today": 0,
                "open_trades_count": 0,
                "today_win_count": 0,
                "today_loss_count": 0,
                "today_total_pips": 0,
                "heartbeat_interval_sec": round(now - _ea_last_heartbeat) if _ea_last_heartbeat > 0 else None,
                "signal_interval_sec": round(now - _last_ea_signal_time) if _last_ea_signal_time > 0 else None,
            }

            save_status_log(status_log)
            print(f"✅ Status log saved: {datetime.now(timezone.utc).isoformat()}")

        except Exception as e:
            print(f"❌ Status logging error: {e}")

def save_status_log(status_data):
    """Supabase の status_logs に INSERT"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/status_logs"
        resp = req.post(
            url,
            json=status_data,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            timeout=10
        )
        if resp.status_code not in [200, 201]:
            print(f"⚠️  Status log save failed: {resp.status_code} {resp.text}")
        return resp.status_code in [200, 201]
    except Exception as e:
        print(f"⚠️  Status log save error: {e}")
        return False

def start_background_jobs():
    """バックグラウンドジョブ開始"""
    global _status_log_thread
    _status_log_thread = threading.Thread(target=status_logging_loop, daemon=True)
    _status_log_thread.start()
    print("✅ Status logging thread started")

# ==================== Status Dashboard API ====================
@app.route("/api/status-logs/latest", methods=["GET"])
def get_latest_status_logs():
    """最新のステータスログを取得（ダッシュボード用）"""
    try:
        limit = int(request.args.get("limit", "10"))
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/status_logs",
            params={"order": "recorded_at.desc", "limit": str(limit)},
            headers=supabase_headers(),
            timeout=10
        )
        if resp.ok:
            return jsonify(resp.json())
        else:
            return jsonify({"error": f"Supabase error: {resp.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== シグナルループ（24/7自動稼働）====================
def signal_loop():
    """Yahoo Finance からデータ取得し、時間足ごとにシグナルを計算・配信
    EA稼働中（ハートビートあり）はスキップ → EAが/ea-signalでリアルタイムプッシュ
    YAHOO_FINANCE_ENABLED=false の場合は完全停止（EA運用時）"""
    yahoo_enabled = os.environ.get("YAHOO_FINANCE_ENABLED", "true").lower() == "true"
    if not yahoo_enabled:
        print("⏸️  YAHOO_FINANCE_ENABLED=false → Yahoo Financeシグナルループを無効化")
        return  # スレッドを終了（コードは残したまま）
    print(f"🔄 シグナルループ開始 TF={TIMEFRAME_MINUTES}m MODE={'TEST' if TEST_MODE else 'PROD'}")
    # サーバー起動直後: EAが再接続する猶予を120秒与える（Renderデプロイ後の競合防止）
    print("⏳ 起動待機: EA接続猶予120秒（Yahoo Finance開始を遅延）")
    settings_changed.wait(timeout=120)
    settings_changed.clear()
    print("🔄 EA接続猶予終了 → シグナルループ本処理開始")
    while True:
        # EA稼働中はYahoo Finance処理をスキップ（EAがMT5リアルタイムデータをプッシュするため）
        if _ea_last_heartbeat > 0 and time.time() - _ea_last_heartbeat < 300:
            print("⏸️  EA稼働中 → Yahoo Financeシグナルループをスキップ（EAからのプッシュ待機）")
            settings_changed.wait(timeout=TIMEFRAME_MINUTES * 60)
            settings_changed.clear()
            continue
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
                    # Gemini結果を承認状態に反映（次回呼び出し判断に使用）
                    _last_gemini_approved = bool(ai.get('valid'))
                    cur_adx = signal.get('composite', {}).get('adx', 0) if signal.get('composite') else 0
                    next_action = '待機モード' if _last_gemini_approved else (f'即時再試行(ADX{cur_adx:.0f}≥{ADX_TREND_THRESHOLD:.0f})' if cur_adx >= ADX_TREND_THRESHOLD else f'10分クールダウン(ADX{cur_adx:.0f}<{ADX_TREND_THRESHOLD:.0f})')
                    print(f"💡 Gemini結果: {'✅承認' if _last_gemini_approved else '❌却下'} → {next_action}")
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

            # EA稼働中は保存・配信をスキップ（Yahoo Finance計算中にEAが接続した場合の対策）
            if _ea_last_heartbeat > 0 and time.time() - _ea_last_heartbeat < 300:
                print("⏸️  EA稼働中（保存前チェック）→ Yahoo Financeシグナルの保存・配信をスキップ")
                settings_changed.wait(timeout=TIMEFRAME_MINUTES * 60)
                settings_changed.clear()
                continue

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
                # クールダウン中(ai_conf=None)のみFCMスキップ
                # クォータ制限・実分析結果はどちらも通知する
                ai_conf = signal_data.get('ai_confidence')
                if ai_conf is not None:
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

# ==================== ポジション警告FCM ====================
def send_position_alert_push(title: str, body: str):
    """ポジション監視専用のFCM通知（カスタムタイトル・本文）"""
    if not FCM_ENABLED or not fcm_tokens:
        return []
    invalid_tokens = set()
    results = []
    for token in list(fcm_tokens):
        try:
            msg = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
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
            print(f"✓ ポジション警告FCM送信完了 msg_id={msg_id}: {token[:20]}...")
            results.append({"token": token[:20], "status": "ok", "msg_id": msg_id})
        except Exception as e:
            print(f"⚠️  ポジション警告FCM送信エラー ({token[:20]}...): {e}")
            results.append({"token": token[:20], "status": "error", "error": str(e)})
            if "registration-token-not-registered" in str(e) or "invalid-argument" in str(e):
                invalid_tokens.add(token)
    fcm_tokens.difference_update(invalid_tokens)
    for t in invalid_tokens:
        delete_fcm_token(t)
    return results

# ==================== ポジション監視ループ（Phase 7）====================
def position_monitor_loop():
    """30分ごとに保有ポジションをGeminiで監視し、危険なら FCM 通知を送る"""
    global _position_monitor_last
    print(f"🔍 ポジション監視ループ開始（{POSITION_MONITOR_INTERVAL // 60}分間隔）")
    # 起動直後は1サイクル待ってから開始
    time.sleep(POSITION_MONITOR_INTERVAL)
    while True:
        try:
            resp = req.get(
                f"{SUPABASE_URL}/rest/v1/trades",
                params={"status": "eq.OPEN", "select": "*", "order": "entry_time.asc"},
                headers=supabase_headers(),
                timeout=10
            )
            if not resp.ok:
                print(f"⚠️ ポジション監視: Supabase取得失敗 {resp.status_code}")
            else:
                open_positions = resp.json()
                if not open_positions:
                    print("🔍 ポジション監視: OPENポジションなし → スキップ")
                else:
                    print(f"🔍 ポジション監視: {len(open_positions)}件を分析中...")
                    df = fetch_yahoo_data(15)
                    if df is None:
                        print("⚠️ ポジション監視: 価格データ取得失敗")
                    else:
                        for pos in open_positions[:3]:  # 最大3件（クォータ節約）
                            result = gemini_monitor_position(pos, df)
                            risk = result['risk']
                            direction = pos.get('direction', '?')
                            unrealized_pl = result['unrealized_pl']
                            print(
                                f"🔍 監視結果: {direction} @{pos.get('entry_price')} "
                                f"現在{result['current_price']} 含み損益{unrealized_pl:+.2f}$ "
                                f"→ リスク[{risk}] {result['reason']}"
                            )
                            if risk in ('HIGH', 'MEDIUM'):
                                direction_icon = "📈" if direction == 'BUY' else "📉"
                                risk_icon = "🚨" if risk == 'HIGH' else "⚠️"
                                title = f"{risk_icon} ポジション警告 [{risk}]"
                                body = (
                                    f"{direction_icon} {direction} @{pos.get('entry_price')} → "
                                    f"現在{result['current_price']} 含み損益:{unrealized_pl:+.2f}$ | "
                                    f"{result['reason']}"
                                )
                                send_position_alert_push(title, body)
            _position_monitor_last = time.time()
        except Exception as e:
            print(f"❌ ポジション監視ループエラー: {e}")
        time.sleep(POSITION_MONITOR_INTERVAL)

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
    if tf != TIMEFRAME_MINUTES:
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
    new_test = (mode == "TEST")
    if new_test != TEST_MODE:
        TEST_MODE = new_test
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
    if mode != CROSSOVER_MODE:
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

# ==================== 監視・EA報告 API ====================

@app.route("/status", methods=["GET"])
def get_status():
    """システム全体の状態（Claude監視・外出先確認用）"""
    now = time.time()
    ea_alive = (_ea_last_heartbeat > 0 and now - _ea_last_heartbeat < 90)
    gemini_ago = round(now - _last_gemini_call_time) if _last_gemini_call_time else None
    if _last_gemini_approved:
        gemini_state = f"✅ 承認済み待機中（方向:{_last_gemini_direction}）"
    elif _last_gemini_call_time and now - _last_gemini_call_time < GEMINI_COOLDOWN_RANGING:
        elapsed = now - _last_gemini_call_time
        remaining_ranging = int(GEMINI_COOLDOWN_RANGING - elapsed)
        remaining_trend = max(0, int(GEMINI_COOLDOWN_REJECTED - elapsed))
        if remaining_trend > 0:
            gemini_state = f"⏳ クールダウン中（レンジ残り{remaining_ranging}秒 / ADX≥{ADX_TREND_THRESHOLD:.0f}で即時解除）"
        else:
            gemini_state = f"⏳ レンジ相場クールダウン中（残り{remaining_ranging}秒 / ADX≥{ADX_TREND_THRESHOLD:.0f}で即時解除）"
    else:
        gemini_state = "🟢 次のクロス待ち（準備完了）"
    return jsonify({
        "server": {
            "alive": True,
            "uptime_hours": round((now - _server_start_time) / 3600, 1),
            "time_utc": datetime.now(timezone.utc).isoformat(),
        },
        "settings": {
            "timeframe": TIMEFRAME_MINUTES,
            "crossover_mode": CROSSOVER_MODE,
            "trading_mode": TRADING_MODE,
            "test_mode": TEST_MODE,
        },
        "gemini": {
            "state": gemini_state,
            "last_direction": _last_gemini_direction or "なし",
            "approved": _last_gemini_approved,
            "last_call_ago_sec": gemini_ago,
        },
        "ea": {
            "alive": ea_alive,
            "status": "🟢 稼働中" if ea_alive else "🔴 未接続（MT5停止の可能性）",
            "last_heartbeat_utc": _ea_last_heartbeat_str or "未受信",
            "last_heartbeat_ago_sec": round(now - _ea_last_heartbeat) if _ea_last_heartbeat else None,
            "last_signal_push_ago_sec": round(now - _last_ea_signal_time) if _last_ea_signal_time else None,
            "signal_loop_mode": "EA_PUSH（Yahoo Finance停止中）" if ea_alive else "Yahoo Finance（EA未接続）",
            "latest_scores": _ea_latest_scores or None,
            "recent_trades": _ea_trades[-5:],
        },
        "fcm": {
            "tokens": len(fcm_tokens),
            "enabled": FCM_ENABLED,
        },
    })

@app.route("/ea-heartbeat", methods=["POST"])
def ea_heartbeat():
    """MT5 EAからの定期ハートビート（生存確認・1分ごと）+ 最新スコア受信（v1.23）"""
    global _ea_last_heartbeat, _ea_last_heartbeat_str, _ea_latest_scores
    _ea_last_heartbeat = time.time()
    _ea_last_heartbeat_str = datetime.now(timezone.utc).isoformat()
    data = request.get_json(silent=True)
    if data:
        _ea_latest_scores = {
            "buy_score":  data.get("buy_score",  0),
            "sell_score": data.get("sell_score", 0),
            "rsi":        data.get("rsi",   0.0),
            "adx":        data.get("adx",   0.0),
            "close":      data.get("close", 0.0),
            "crossover":  data.get("crossover", "NONE"),
            "updated_at": _ea_last_heartbeat_str,
        }
    return jsonify({"status": "ok"})

# ==================== EA リアルタイムシグナル受信（Phase 9）====================
@app.route("/ea-signal", methods=["POST", "OPTIONS"])
def ea_signal_push():
    """MT5 EAからリアルタイム指標データを受信 → Gemini分析 → Supabase保存 → FCM通知"""
    global _last_ea_signal_time, _last_gemini_approved, _last_gemini_direction, _ea_signal_dedup
    if request.method == "OPTIONS":
        return jsonify({}), 200

    ea_data = request.get_json()
    if not ea_data:
        return jsonify({"error": "No data"}), 400

    crossover = ea_data.get('crossover', '')
    if crossover not in ('UP_CROSS', 'DOWN_CROSS'):
        return jsonify({"error": f"Invalid crossover: {crossover}"}), 400

    # ① 重複防止: 60秒以内の同方向シグナルはスキップ（EA複数インスタンス・リトライ対策）
    now = time.time()
    last_recv = _ea_signal_dedup.get(crossover, 0)
    if now - last_recv < 60:
        print(f"⏭️ /ea-signal 重複スキップ: {crossover} ({int(now - last_recv)}秒前に受信済み)")
        return jsonify({"status": "skipped", "reason": "duplicate_within_60s"}), 200
    _ea_signal_dedup[crossover] = now

    print(f"📡 EA→サーバー シグナル受信: {crossover} "
          f"close={ea_data.get('latest_close')} "
          f"買い{ea_data.get('buy_score')}点 vs 売り{ea_data.get('sell_score')}点 "
          f"ADX={ea_data.get('adx')}")

    # Gemini判断（_gemini_should_callをバイパスしてEAプッシュは必ず実行）
    ai = gemini_analyze_ea_signal(ea_data)
    _last_gemini_approved = bool(ai.get('valid'))
    _last_gemini_direction = crossover

    # ② デモ用ルールベースエントリー（DEMO_RULE_BASED=true かつ Geminiクォータ時）
    if ai.get('quota_error') and DEMO_RULE_BASED:
        buy_score  = int(ea_data.get('buy_score',  0))
        sell_score = int(ea_data.get('sell_score', 0))
        adx        = float(ea_data.get('adx', 0))
        score_ok = (crossover == 'UP_CROSS'   and buy_score  >= DEMO_RULE_MIN_SCORE) or \
                   (crossover == 'DOWN_CROSS' and sell_score >= DEMO_RULE_MIN_SCORE)
        if score_ok and adx >= DEMO_RULE_MIN_ADX:
            ai['valid']      = True
            ai['confidence'] = 50  # EA の MIN_CONFIDENCE=50 に合わせてエントリーさせる
            ai['reason']     = f'ルールベース(Gemini制限中) score={buy_score if crossover=="UP_CROSS" else sell_score}pt ADX={adx:.0f}'
            print(f"🤖 デモルールベースエントリー: {crossover} score={buy_score}/{sell_score} ADX={adx:.1f}")
        else:
            print(f"⏸️ デモルールベース: スコア不足 buy={buy_score} sell={sell_score} ADX={adx:.1f}")

    # signal_dataをサーバー共通フォーマットで構築
    comp = {
        'buy_score':    ea_data.get('buy_score',  0),
        'sell_score':   ea_data.get('sell_score', 0),
        'buy_reasons':  [r for r in ea_data.get('buy_reasons',  '').split(',') if r],
        'sell_reasons': [r for r in ea_data.get('sell_reasons', '').split(',') if r],
        'ema20':        ea_data.get('ema20'),
        'ema50':        ea_data.get('ema50'),
        'ema_long':     ea_data.get('ema_long'),
        'bb_upper':     ea_data.get('bb_upper'),
        'bb_lower':     ea_data.get('bb_lower'),
        'stoch_k':      ea_data.get('stoch_k'),
        'stoch_d':      ea_data.get('stoch_d'),
        'adx':          ea_data.get('adx'),
        'di_plus':      ea_data.get('di_plus'),
        'di_minus':     ea_data.get('di_minus'),
        'atr':          ea_data.get('atr'),
        'is_trending':  float(ea_data.get('adx', 0)) > 25,
    }
    signal_data = {
        'crossover':        crossover,
        'crossover_mode':   'COMPOSITE',
        'rsi':              ea_data.get('rsi'),
        'main_line':        ea_data.get('macd'),
        'signal_line':      ea_data.get('macd_signal'),
        'latest_close':     ea_data.get('latest_close'),
        'time':             datetime.now(timezone.utc).isoformat(),
        'composite':        comp,
        'ai_valid':         ai['valid'],
        'ai_confidence':    ai['confidence'],
        'ai_reason':        ai['reason'],
        'ai_sl_suggestion': ai.get('sl_suggestion'),
        'ai_tp_suggestion': ai.get('tp_suggestion'),
        'ai_key_level':     ai.get('key_level', ''),
        'timeframe':        15,
        'test_mode':        TEST_MODE,
        'source':           'EA_PUSH',
        'generated_at':     datetime.now(timezone.utc).isoformat(),
    }

    _last_ea_signal_time = time.time()

    # Supabase保存 → db_id取得
    db_id = save_signal_to_supabase(signal_data)
    if db_id:
        signal_data['db_id'] = db_id

    # FCM通知（クールダウン中 = confidence=None のみスキップ）
    if ai['confidence'] is not None:
        send_fcm_push(signal_data)

    # WebSocket配信（スマホアプリ向け）
    socketio.emit('signal', signal_data)

    print(f"✅ EAシグナル処理完了: {crossover} "
          f"AI={'✅承認' if ai['valid'] else '❌却下'} {ai['confidence']}% "
          f"db_id={db_id}")

    return jsonify({
        "status":           "ok",
        "db_id":            db_id,
        "ai_valid":         ai['valid'],
        "ai_confidence":    ai['confidence'],
        "ai_reason":        ai['reason'],
        "ai_sl_suggestion": ai.get('sl_suggestion'),
        "ai_tp_suggestion": ai.get('tp_suggestion'),
    })

@app.route("/ea-trade", methods=["POST"])
def ea_trade_report():
    """MT5 EAが注文・決済したとき内容をサーバーに記録 + Supabase保存"""
    global _ea_trades
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    data["reported_at"] = datetime.now(timezone.utc).isoformat()
    _ea_trades.append(data)
    if len(_ea_trades) > 50:
        _ea_trades.pop(0)
    action = data.get("action", "?")
    direction = data.get("direction", "?")
    price = data.get("price", "?")
    ticket = data.get("ticket", "?")
    print(f"📊 EA報告: {action} {direction} @{price} ticket={ticket}")

    # Supabase に保存
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        if action == "ORDER":
            # 新規注文 → trades テーブルに INSERT
            payload = {
                "direction": direction,
                "entry_price": float(price),
                "entry_time": now_iso,
                "status": "OPEN",
                "auto_executed": True,
                "sl": data.get("sl"),
                "tp": data.get("tp"),
                "notes": f"EA ticket={ticket} lot={data.get('lot')} balance={data.get('balance')}",
            }
            resp = req.post(
                f"{SUPABASE_URL}/rest/v1/trades",
                json=payload,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                timeout=10
            )
            if resp.ok:
                row = resp.json()
                db_id = row[0]["id"] if row else None
                print(f"✅ EA注文 Supabase保存完了 trade_id={db_id}")
            else:
                print(f"⚠️  EA注文 Supabase保存失敗: {resp.text}")

        elif action == "CLOSE":
            # 決済 → ticket でマッチする OPEN レコードを更新
            close_dir = direction.replace("_CLOSE", "")
            existing = req.get(
                f"{SUPABASE_URL}/rest/v1/trades",
                params={"status": "eq.OPEN", "direction": f"eq.{close_dir}",
                        "order": "entry_time.desc", "limit": "1"},
                headers=supabase_headers(), timeout=10
            ).json()
            if existing:
                trade_id = existing[0]["id"]
                entry_price = float(existing[0]["entry_price"])
                profit_loss = round(
                    (float(price) - entry_price) * (1 if close_dir == "BUY" else -1), 2
                )
                pips = round(profit_loss * 10, 1)
                status = "CLOSED_PROFIT" if profit_loss >= 0 else "CLOSED_LOSS"
                patch = {
                    "exit_price": float(price),
                    "exit_time": now_iso,
                    "profit_loss": profit_loss,
                    "pips": pips,
                    "status": status,
                    "updated_at": now_iso,
                }
                resp = req.patch(
                    f"{SUPABASE_URL}/rest/v1/trades",
                    params={"id": f"eq.{trade_id}"},
                    json=patch,
                    headers={**supabase_headers(), "Prefer": "return=minimal"},
                    timeout=10
                )
                print(f"✅ EA決済 Supabase更新完了 trade_id={trade_id} P/L={profit_loss} ({status})")
            else:
                print("⚠️  EA決済: 対応するOPENトレードが見つかりません")
    except Exception as e:
        print(f"⚠️  EA取引Supabase保存エラー: {e}")

    return jsonify({"status": "ok"})

# ==================== FCM診断・トークンリロード ====================
@app.route("/debug/fcm", methods=["GET", "POST"])
def debug_fcm():
    """FCMトークンの状態診断（メモリ vs Firestore）"""
    if request.headers.get("X-Push-Secret", "") != PUSH_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # メモリ上のトークン
    memory_tokens = list(fcm_tokens)

    # Supabaseの実データを直接確認
    supabase_tokens = []
    supabase_error = None
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/fcm_tokens",
            params={"select": "token,updated_at"},
            headers=supabase_headers(),
            timeout=10
        )
        if resp.ok:
            for row in resp.json():
                supabase_tokens.append({
                    "token": row["token"][:20] + "...",
                    "updated_at": row.get("updated_at")
                })
        else:
            supabase_error = f"{resp.status_code} {resp.text}"
    except Exception as e:
        supabase_error = str(e)

    # POST の場合はSupabaseからリロードも実行
    reloaded = False
    if request.method == "POST":
        load_fcm_tokens()
        reloaded = True

    return jsonify({
        "fcm_enabled": FCM_ENABLED,
        "memory": {
            "count": len(memory_tokens),
            "tokens": [t[:20] + "..." for t in memory_tokens],
        },
        "supabase": {
            "count": len(supabase_tokens),
            "tokens": supabase_tokens,
            "error": supabase_error,
        },
        "reloaded_from_supabase": reloaded,
    })

# ==================== ポジション監視 手動トリガー（Phase 7）====================
@app.route("/api/position-monitor/trigger", methods=["POST", "OPTIONS"])
def trigger_position_monitor():
    """ポジション監視を手動で即時実行（テスト・デバッグ用）"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if request.headers.get("X-Push-Secret", "") != PUSH_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"status": "eq.OPEN", "select": "*", "order": "entry_time.asc"},
            headers=supabase_headers(), timeout=10
        )
        if not resp.ok:
            return jsonify({"error": f"Supabase error: {resp.status_code}"}), 500
        open_positions = resp.json()
        if not open_positions:
            return jsonify({"status": "ok", "message": "OPENポジションなし", "results": []})

        df = fetch_yahoo_data(15)
        if df is None:
            return jsonify({"error": "価格データ取得失敗"}), 500

        results = []
        for pos in open_positions[:3]:
            result = gemini_monitor_position(pos, df)
            risk = result['risk']
            alerted = False
            if risk in ('HIGH', 'MEDIUM'):
                direction = pos.get('direction', '?')
                direction_icon = "📈" if direction == 'BUY' else "📉"
                risk_icon = "🚨" if risk == 'HIGH' else "⚠️"
                title = f"{risk_icon} ポジション警告 [{risk}] (手動テスト)"
                body = (
                    f"{direction_icon} {direction} @{pos.get('entry_price')} → "
                    f"現在{result['current_price']} 含み損益:{result['unrealized_pl']:+.2f}$ | "
                    f"{result['reason']}"
                )
                send_position_alert_push(title, body)
                alerted = True
            results.append({
                "trade_id": pos.get("id"),
                "direction": pos.get("direction"),
                "entry_price": pos.get("entry_price"),
                "current_price": result["current_price"],
                "unrealized_pl": result["unrealized_pl"],
                "risk": result["risk"],
                "action": result["action"],
                "reason": result["reason"],
                "alerted": alerted,
            })
        return jsonify({"status": "ok", "positions_checked": len(results), "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== Phase 8: 週次自己分析 ====================
_weekly_analysis_last_monday = ""  # 最後に実行した月曜日の日付

def gemini_weekly_analysis(trades, signals):
    """過去7日間の取引データをGeminiが分析して改善提案を返す"""
    total = len(trades)
    if total == 0:
        return None
    wins   = [t for t in trades if t.get('status') == 'CLOSED_PROFIT']
    losses = [t for t in trades if t.get('status') == 'CLOSED_LOSS']
    total_pl   = round(sum(t.get('profit_loss') or 0 for t in trades), 2)
    win_rate   = round(len(wins) / total * 100, 1) if total else 0
    avg_conf   = None
    conf_vals  = [t.get('signals', {}).get('ai_confidence') for t in trades
                  if t.get('signals') and t['signals'].get('ai_confidence') is not None]
    if conf_vals:
        avg_conf = round(sum(conf_vals) / len(conf_vals), 1)

    # 信頼度別勝率
    buckets: dict = {}
    for t in trades:
        conf = (t.get('signals') or {}).get('ai_confidence')
        if conf is None:
            continue
        b = (int(conf) // 10) * 10
        if b not in buckets:
            buckets[b] = {"wins": 0, "total": 0}
        buckets[b]["total"] += 1
        if t.get('status') == 'CLOSED_PROFIT':
            buckets[b]["wins"] += 1
    conf_breakdown = [
        {"confidence": k, "win_rate": round(v["wins"]/v["total"]*100,1), "count": v["total"]}
        for k, v in sorted(buckets.items())
    ]

    recent5 = [{"dir": t.get('direction'), "pl": round(t.get('profit_loss') or 0, 2),
                "conf": (t.get('signals') or {}).get('ai_confidence')} for t in trades[:5]]

    prompt = f"""あなたはGOLD自動売買システムの上級アナリストです。
過去7日間の取引実績を分析し、来週に向けた改善提案をしてください。

【週間実績サマリー】
総取引数: {total}件 / 勝率: {win_rate}% ({len(wins)}勝{len(losses)}敗) / 累計損益: {total_pl:+.2f}$
平均信頼度: {avg_conf or 'N/A'}%

【信頼度別勝率】
{json.dumps(conf_breakdown, ensure_ascii=False)}

【直近5件の取引】
{json.dumps(recent5, ensure_ascii=False)}

【現在の設定】
MIN_CONFIDENCE=50% / MIN_TRADE_INTERVAL=900秒(15分) / RISK_PERCENT=2%

以下のJSON形式のみで回答してください:
{{"summary": "週間総評（100文字以内）", "recommended_min_confidence": 推奨最低信頼度(整数0-100), "recommended_interval_minutes": 推奨最小取引間隔(分・整数), "insight": "最重要な気づき（80文字以内）", "action": "来週すべき最優先アクション（60文字以内）"}}"""

    try:
        text = _gemini_generate(prompt, max_retries=1)
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            "summary": str(result.get("summary", "")),
            "recommended_min_confidence": int(result.get("recommended_min_confidence", 50)),
            "recommended_interval_minutes": int(result.get("recommended_interval_minutes", 15)),
            "insight": str(result.get("insight", "")),
            "action": str(result.get("action", "")),
            "stats": {
                "total": total, "wins": len(wins), "losses": len(losses),
                "win_rate": win_rate, "total_pl": total_pl, "avg_confidence": avg_conf,
                "conf_breakdown": conf_breakdown,
            }
        }
    except Exception as e:
        print(f"❌ Gemini週次分析エラー: {e}")
        return None

def run_weekly_analysis():
    """週次分析を実行してFCM通知を送る"""
    from datetime import timedelta
    print("📊 週次自己分析 開始...")
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={
                "select": "direction,profit_loss,pips,status,entry_time,signals(ai_confidence)",
                "status": "in.(CLOSED_PROFIT,CLOSED_LOSS)",
                "entry_time": f"gte.{start}",
                "order": "entry_time.desc",
                "limit": "100"
            },
            headers=supabase_headers(), timeout=10
        )
        trades = resp.json() if resp.ok else []
        signals_resp = req.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            params={"select": "id,crossover,ai_confidence,created_at",
                    "created_at": f"gte.{start}", "limit": "200"},
            headers=supabase_headers(), timeout=10
        )
        signals = signals_resp.json() if signals_resp.ok else []

        result = gemini_weekly_analysis(trades, signals)
        if result is None:
            print("📊 週次分析: 取引データなし → スキップ")
            return None

        print(f"📊 週次分析完了: 勝率{result['stats']['win_rate']}% 推奨信頼度{result['recommended_min_confidence']}%")

        # FCM通知
        title = f"📊 週次レポート | 勝率{result['stats']['win_rate']}% 損益{result['stats']['total_pl']:+.2f}$"
        body  = f"{result['summary']} | 推奨MIN_CONFIDENCE:{result['recommended_min_confidence']}% | {result['action']}"
        send_position_alert_push(title, body)
        return result
    except Exception as e:
        print(f"❌ 週次分析エラー: {e}")
        return None

def weekly_analysis_loop():
    """毎週月曜日 9:00 JST（0:00 UTC）に週次分析を実行"""
    global _weekly_analysis_last_monday
    print("📅 週次分析ループ開始（月曜日 0:00 UTC に実行）")
    while True:
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        # 月曜日(weekday=0) かつ 当日まだ未実行
        if now.weekday() == 0 and today_str != _weekly_analysis_last_monday:
            _weekly_analysis_last_monday = today_str
            run_weekly_analysis()
        time.sleep(3600)  # 1時間ごとにチェック

@app.route("/api/weekly-analysis/trigger", methods=["POST", "OPTIONS"])
def trigger_weekly_analysis():
    """週次分析を手動で即時実行（テスト用）"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if request.headers.get("X-Push-Secret", "") != PUSH_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    result = run_weekly_analysis()
    if result is None:
        return jsonify({"status": "ok", "message": "取引データなし（分析スキップ）"})
    return jsonify({"status": "ok", "result": result})

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
    start_background_jobs()
    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()
    t2 = threading.Thread(target=position_monitor_loop, daemon=True)
    t2.start()
    t3 = threading.Thread(target=weekly_analysis_loop, daemon=True)
    t3.start()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
