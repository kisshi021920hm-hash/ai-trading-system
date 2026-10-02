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

# ==================== ハイブリッドSL設定（v1.25新機能） ====================
# アプリから動的に調整可能
_hybrid_sl_config = {
    "initial_sl_usd": 2.0,       # 初期SL（-$2）- 保険・ノイズ対策
    "trailing_trigger_usd": 2.0, # トレーリング開始条件（+$2）
    "trailing_sl_usd": 0.5,      # トレーリング後のSL（+$0.5）- スプレッド対応
    "enabled": True
}

# トレード自動化設定
# MANUAL: スマホのみ配信（ユーザー判断）
# SEMI_AUTO: スマホのボタンで MT5 注文
# FULL_AUTO: Gemini 信頼度で自動エントリー（レガシー）
# AI_CLOSE_MODE: エントリーは信頼度判定 + ポジション監視で決済判定を実行
TRADING_MODE = os.environ.get("TRADING_MODE", "MANUAL")
AUTO_CONFIDENCE_THRESHOLD = int(os.environ.get("AUTO_CONFIDENCE_THRESHOLD", "70"))
ENTRY_CONFIDENCE_THRESHOLD = int(os.environ.get("ENTRY_CONFIDENCE_THRESHOLD", "60"))
CLOSE_CONFIDENCE_THRESHOLD = int(os.environ.get("CLOSE_CONFIDENCE_THRESHOLD", "70"))
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
_force_close_pending = False   # AI_CLOSE_MODE: Gemini決済指示フラグ
_force_close_set_time = 0.0    # フラグをセットした時刻（120秒後に自動リセット）
_last_gemini_close_call_time = 0.0   # AI_CLOSE_MODE: クロスオーバー決済判断の最終呼び出し時刻（60秒クールダウン用）
_last_gemini_any_call_time = 0.0     # グローバルGemini呼び出し最終時刻（30秒間隔制限用）

# デモ用ルールベースエントリー設定
DEMO_RULE_BASED = os.environ.get("DEMO_RULE_BASED", "false").lower() == "true"
DEMO_RULE_MIN_SCORE = int(os.environ.get("DEMO_RULE_MIN_SCORE", "4"))  # ルールベース発動の最低スコア
DEMO_RULE_MIN_ADX   = float(os.environ.get("DEMO_RULE_MIN_ADX", "20.0"))  # ルールベース発動の最低ADX

# Status Logging設定
STATUS_LOG_INTERVAL = 300  # 5分ごと
_status_log_thread = None

# AI 決済判定の履歴記録（最後の判定情報）
_last_ai_decision = {
    "timestamp": None,              # AI 判定実行時刻
    "decision_type": None,          # ENTRY / CLOSE / HOLD
    "crossover_direction": None,    # UP_CROSS / DOWN_CROSS
    "confidence_score": None,       # 信頼度スコア（0-100）
    "decision_reason": "",          # AI 判定理由（Gemini から返却）
    "executed_action": "",          # 実際の実行内容（SELL 0.02, CLOSE, など）
}

# ポジション監視用：前回のシグナル記憶（{position_id: crossover}）
_last_position_signal = {}

# システムログ記録（最大 100 行までメモリ保持）
_system_logs = []
_system_logs_lock = threading.Lock()

def log_system(level, message):
    """システムログを記録（Supabase + メモリ）"""
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {"timestamp": timestamp, "level": level, "message": message}

    # メモリに保持
    with _system_logs_lock:
        _system_logs.append(log_entry)
        if len(_system_logs) > 100:
            _system_logs.pop(0)

    # Supabase に非同期保存
    try:
        req.post(
            f"{SUPABASE_URL}/rest/v1/system_logs",
            json=log_entry,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            timeout=5
        )
    except Exception as e:
        pass  # ログ保存失敗時は無視

# ==================== Gemini モデルプール（フォールバック対応）====================
GEMINI_MODELS = [
    "gemini-3.8-flash",        # 主力・最新
    "gemini-3.7-flash",        # フォールバック1
    "gemini-3.5-flash",        # フォールバック2
    "gemini-3.5-flash-lite",   # フォールバック3（軽量・高速）
]
_current_gemini_model_index = 0  # 現在使用中のモデルインデックス
_gemini_model_fallback_count = 0  # フォールバック実行回数（監視用）
GEMINI_MIN_CALL_INTERVAL = 30    # Gemini呼び出しの最低間隔（秒）- 連続エラー防止

# ==================== 初期化 ====================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "goldtrader_secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel(GEMINI_MODELS[_current_gemini_model_index])

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
def log_position_status():
    """保有中のすべてのポジション含み益をシステムログに記録"""
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"status": "eq.OPEN"},
            headers=supabase_headers(),
            timeout=5
        )
        open_positions = resp.json() if resp.ok else []
        if not open_positions:
            return

        # 現在価格を取得（複数の方法で試す）
        current_close = None
        if _ea_latest_scores and 'close' in _ea_latest_scores:
            current_close = float(_ea_latest_scores.get('close', 0))

        # EA最新スコアから close が取得できない場合はポジション情報のみ記録
        for pos in open_positions:
            try:
                entry_price = float(pos.get('entry_price', 0))
                direction = pos.get('direction', 'BUY')

                if current_close and current_close > 0:
                    pnl = current_close - entry_price if direction == 'BUY' else entry_price - current_close
                    pnl_pips = abs(pnl)
                    pnl_sign = "📈 含み益" if pnl > 0 else "📉 含み損"

                    log_system("INFO",
                        f"📊 ポジション監視: {direction} @{entry_price}円 → 現在{current_close}円 | "
                        f"{pnl_sign}={pnl_pips:.2f}pips | "
                        f"SL={pos.get('sl')}円 TP={pos.get('tp')}円")
                else:
                    # 現在価格がない場合でもポジション存在を記録
                    log_system("INFO",
                        f"📊 ポジション監視: {direction} @{entry_price}円 | "
                        f"SL={pos.get('sl')}円 TP={pos.get('tp')}円 | "
                        f"ロット={pos.get('volume')}lot (価格更新待機中)")
            except Exception as e:
                print(f"⚠️  ポジションログエラー: {e}")
    except Exception as e:
        print(f"⚠️  ポジション監視エラー: {e}")

def send_mt5_order(signal_id, direction, entry_price, sl_price=None, tp_price=None, sl_pips=20, tp_pips=40, trailing_stop_pips=15):
    """MT5 Webhook サーバーに自動注文を送信し、Supabaseにトレードを記録する

    Args:
        sl_price, tp_price: Gemini提案値（指定されている場合はこれを使用）
        sl_pips, tp_pips: フォールバック値（提案がない場合に使用）
        trailing_stop_pips: トレーリングストップ幅（デフォルト15pips）
    """
    if not MT5_WEBHOOK_URL:
        print("⚠️  MT5_WEBHOOK_URL 未設定 → 自動注文スキップ")
        return {"success": False, "error": "Webhook URL not configured"}

    # Gemini提案値がなければ、pipsから計算
    if sl_price is None or tp_price is None:
        if direction == "BUY":
            sl_price = round(entry_price - sl_pips * 0.1, 2) if sl_price is None else sl_price
            tp_price = round(entry_price + tp_pips * 0.1, 2) if tp_price is None else tp_price
        else:
            sl_price = round(entry_price + sl_pips * 0.1, 2) if sl_price is None else sl_price
            tp_price = round(entry_price - tp_pips * 0.1, 2) if tp_price is None else tp_price
    else:
        # Gemini提案値を使用
        sl_price = round(sl_price, 2)
        tp_price = round(tp_price, 2)

    # トレーリングストップの初期値を計算
    trailing_sl = entry_price - (trailing_stop_pips * 0.1) if direction == "BUY" else entry_price + (trailing_stop_pips * 0.1)

    payload = {
        "signal_id": signal_id,
        "direction": direction,
        "entry_price": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "trailing_sl": round(trailing_sl, 2),
        "trailing_stop_pips": trailing_stop_pips,
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
            print(f"✅ MT5注文成功: {direction} @{entry_price} SL={sl_price} TP={tp_price} Trailing={trailing_stop_pips}pips")
            log_system("INFO", f"✅ エントリー実行: {direction} @{entry_price}円 SL={sl_price}円 TP={tp_price}円 TrailingSL={trailing_sl}円 (Signal#{signal_id})")
            log_system("INFO", f"📊 トレーリングストップ: {trailing_stop_pips}pips 設定完了（初期値={trailing_sl}円）")
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
                    "trailing_sl": trailing_sl,
                    "trailing_pips": trailing_stop_pips,
                },
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                timeout=10
            )
            log_system("INFO", f"📊 トレード記録保存: {direction} エントリー価格={entry_price} (SL={sl_price}, TP={tp_price}, TrailingSL={trailing_sl})")
            return {"success": True, **result}
        else:
            print(f"❌ MT5 Webhook エラー: {response.status_code} {response.text}")
            return {"success": False, "error": response.text}
    except Exception as e:
        print(f"❌ MT5 注文送信エラー: {e}")
        return {"success": False, "error": str(e)}

def check_auto_execution(signal_data):
    """
    FULL_AUTO / AI_CLOSE_MODE 時に信頼度を確認して自動実行判定
    ❌ダマシシグナル（ai_valid=False）は絶対に実行しない
    """
    if TRADING_MODE not in ["FULL_AUTO", "AI_CLOSE_MODE"]:
        return {"should_execute": False, "reason": f"モード={TRADING_MODE}（自動実行対象外）"}
    if not signal_data.get("crossover"):
        return {"should_execute": False, "reason": "クロスオーバーなし"}

    # ❌ ダマシシグナルは絶対に実行しない
    if not signal_data.get("ai_valid"):
        print(f"🛑 ダマシシグナル検出: ai_valid=False → 自動実行禁止")
        return {"should_execute": False, "reason": "AI判定=ダマシ（自動実行禁止）"}

    conf = signal_data.get("ai_confidence")
    if conf is None:
        return {"should_execute": False, "reason": "信頼度なし（クールダウン中）"}

    # 信頼度が低すぎる場合は実行しない
    threshold = ENTRY_CONFIDENCE_THRESHOLD if TRADING_MODE == "AI_CLOSE_MODE" else AUTO_CONFIDENCE_THRESHOLD
    if conf < threshold:
        print(f"⚠️ 信頼度不足: {conf}% < 閾値{threshold}%（実行禁止）")
        return {"should_execute": False, "reason": f"信頼度{conf}% < 閾値{threshold}%"}

    direction = "BUY" if signal_data["crossover"] == "UP_CROSS" else "SELL"
    return {
        "should_execute": True,
        "direction": direction,
        "reason": f"信頼度{conf}% ≥ 閾値{threshold}% → 自動実行"
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
    """
    Gemini API 自動フォールバック
    - 429（上限）・404（廃止）・503（障害）→ 次のモデルへ即切り替え
    - 一時的なエラー（5xx以外）→ リトライ後に切り替え
    - 全モデル失敗でエラー返却
    - グローバル30秒間隔制限: 連続呼び出しによる429エラーを防止
    """
    global _current_gemini_model_index, _gemini_model_fallback_count, gemini_model, _last_gemini_any_call_time

    # グローバルGemini呼び出し間隔制限（30秒以内は429エラー扱いでスキップ）
    now_check = time.time()
    if _last_gemini_any_call_time > 0 and now_check - _last_gemini_any_call_time < GEMINI_MIN_CALL_INTERVAL:
        remaining = GEMINI_MIN_CALL_INTERVAL - (now_check - _last_gemini_any_call_time)
        print(f"⏭️ Gemini グローバルクールダウン中（あと{remaining:.0f}秒）→ スキップ")
        raise Exception(f"Geminiクールダウン中（{remaining:.0f}秒後に再試行可能）- 429クォータ制限中")
    _last_gemini_any_call_time = time.time()

    models_attempted = 0

    while models_attempted < len(GEMINI_MODELS):
        for attempt in range(max_retries + 1):
            try:
                current_model_name = GEMINI_MODELS[_current_gemini_model_index]
                print(f"🤖 Gemini 呼び出し: {current_model_name}")
                result = gemini_model.generate_content(prompt).text.strip()
                return result
            except Exception as e:
                err = str(e)
                # 即切り替え対象: 上限・廃止・サービス障害
                should_switch = (
                    "429" in err or
                    "quota" in err.lower() or
                    "Resource has been exhausted" in err or
                    "404" in err or
                    "no longer available" in err.lower() or
                    "deprecated" in err.lower() or
                    "503" in err
                )

                if should_switch:
                    old_model = GEMINI_MODELS[_current_gemini_model_index]
                    _current_gemini_model_index = (_current_gemini_model_index + 1) % len(GEMINI_MODELS)
                    _gemini_model_fallback_count += 1
                    new_model = GEMINI_MODELS[_current_gemini_model_index]
                    gemini_model = genai.GenerativeModel(new_model)
                    print(f"⚠️ {old_model} → {new_model} に切り替え（フォールバック#{_gemini_model_fallback_count}）理由: {err[:80]}")
                    models_attempted += 1
                    break  # 次のモデルを試す
                else:
                    # 一時的エラー → リトライ
                    if attempt < max_retries:
                        wait = 15 * (attempt + 1)
                        print(f"⏳ Gemini エラー → {wait}秒後リトライ ({attempt+1}/{max_retries}): {err[:80]}")
                        time.sleep(wait)
                        continue
                    # リトライ限界 → 次のモデルへ
                    old_model = GEMINI_MODELS[_current_gemini_model_index]
                    _current_gemini_model_index = (_current_gemini_model_index + 1) % len(GEMINI_MODELS)
                    _gemini_model_fallback_count += 1
                    new_model = GEMINI_MODELS[_current_gemini_model_index]
                    gemini_model = genai.GenerativeModel(new_model)
                    print(f"❌ {old_model} リトライ限界 → {new_model} に切り替え（フォールバック#{_gemini_model_fallback_count}）")
                    models_attempted += 1
                    break

    print(f"❌ すべての Gemini モデルで失敗")
    raise Exception("すべての Gemini モデルが利用不可 - 数分後に自動回復します")

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
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
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
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
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
def gemini_analyze_ea_signal(ea_data, open_positions=None):
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

    # 現在のポジション情報をプロンプトに追加（ポジション保有中の判定用）
    position_context = ""
    if open_positions:
        current_price = float(ea_data.get('latest_close', 0))
        for pos in open_positions:
            entry_price = float(pos.get('entry_price', 0))
            direction_jp = "買いポジション" if pos.get('direction') == 'BUY' else "売りポジション"
            pnl = current_price - entry_price if pos.get('direction') == 'BUY' else entry_price - current_price
            pnl_pips = abs(pnl)
            profit_loss_text = "含み益" if pnl > 0 else "含み損"
            position_context += f"""
【保有中のポジション】
種類: {direction_jp} / エントリー価格: {entry_price}
現在価格: {current_price} / {profit_loss_text}: {pnl_pips:.2f}pips
※ 現在のポジションがある中での新シグナル判定です。含み損がある場合は特に慎重に、含み益がある場合は利益確保を優先に判定してください。"""

    # テクニカル指標の詳細分析コンテキスト
    technical_analysis = ""

    # RSI トレンド分析
    rsi_value = float(ea_data.get('rsi', 50))
    rsi_trend = "⚠️ オーバーバイ域（反転警戒）" if rsi_value > 70 else ("⚠️ オーバーソールド域（反発期待）" if rsi_value < 30 else "✅ 中立")
    technical_analysis += f"\n【RSI 分析】{rsi_value:.1f} {rsi_trend}"

    # ボリンジャーバンド乖離度
    current_price = float(ea_data.get('latest_close', 0))
    bb_upper = float(ea_data.get('bb_upper', current_price))
    bb_lower = float(ea_data.get('bb_lower', current_price))
    bb_middle = (bb_upper + bb_lower) / 2
    bb_width = bb_upper - bb_lower
    if bb_width > 0:
        bb_position = (current_price - bb_lower) / bb_width * 100
        bb_status = "上限接近（反転警戒）" if bb_position > 80 else ("下限接近（反発期待）" if bb_position < 20 else "中立")
        technical_analysis += f"\n【ボリンジャーバンド】位置{bb_position:.0f}% {bb_status} / 幅={bb_width:.2f}"

    # MACD トレンド
    macd_val = float(ea_data.get('macd', 0))
    macd_sig = float(ea_data.get('macd_signal', 0))
    macd_status = "📈 上昇勢力" if macd_val > macd_sig else "📉 下降勢力"
    macd_histogram = macd_val - macd_sig
    technical_analysis += f"\n【MACD】{macd_status} (ヒストグラム={macd_histogram:+.4f})"

    # ADX トレンド強度
    adx_val = float(ea_data.get('adx', 20))
    adx_strength = "💪 強トレンド" if adx_val > 25 else ("⚠️ 弱トレンド" if adx_val < 20 else "普通")
    technical_analysis += f"\n【ADX】{adx_strength} (値={adx_val:.1f})"

    # ローソク足の高値安値の幅（ボラティリティ）
    current_high = float(ea_data.get('current_high', current_price))
    current_low = float(ea_data.get('current_low', current_price))
    candle_range = current_high - current_low
    technical_analysis += f"\n【現在足ボラティリティ】高値-安値={candle_range:.2f}pips (高値={current_high} 安値={current_low})"

    # 過去ローソク足の方向性判定
    candle_history = ea_data.get('candle_history', [])
    if candle_history and len(candle_history) >= 5:
        closes = [float(c.get('close', 0)) for c in candle_history[-5:]]
        is_uptrend = closes[-1] > closes[0]
        trend_direction = "📈 上昇傾向" if is_uptrend else "📉 下降傾向"
        technical_analysis += f"\n【過去5足の方向】{trend_direction}"

    prompt = f"""あなたはゴールド（XAUUSD）の上級テクニカルアナリストです。
MT5のリアルタイムデータから計算された複合テクニカル指標を総合分析し、このシグナルの有効性を判定してください。

【シグナル】方向: {direction} / 価格: {ea_data.get('latest_close')}
【スコア】買い{ea_data.get('buy_score', 0)}点 vs 売り{ea_data.get('sell_score', 0)}点
買い根拠: {ea_data.get('buy_reasons', '')}
売り根拠: {ea_data.get('sell_reasons', '')}

【指標（MT5リアルタイム）】
EMA20={ea_data.get('ema20')} EMA50={ea_data.get('ema50')} EMA200={ea_data.get('ema_long')}
ストキャスK={ea_data.get('stoch_k')} D={ea_data.get('stoch_d')}
DI+={ea_data.get('di_plus')} DI-={ea_data.get('di_minus')} ATR={ea_data.get('atr')}{technical_analysis}{trade_context}{position_context}

【判定基準】
1. RSI が 70 以上（買い信号時）または 30 以下（売り信号時）の場合は反転警戒
2. MACD ヒストグラムがシグナルと逆方向に向かっていたら勢い衰退の兆候
3. ADX < 20 の場合は弱いトレンドなので信頼度を下げる
4. ボラティリティが極度に高い場合は危険性を考慮

以下のJSON形式のみで回答:
{{"valid": true/false, "confidence": 0-100, "reason": "100文字以内", "sl_suggestion": SL価格(数値)またはnull, "tp_suggestion": TP価格(数値)またはnull, "key_level": "注目水準50文字以内"}}"""

    try:
        text = _gemini_generate(prompt)
        if "```" in text:
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
        result = json.loads(text)

        # Gemini 判定結果をシステムログに記録
        valid = bool(result.get('valid', False))
        confidence = int(result.get('confidence', 0))
        reason = str(result.get('reason', ''))
        sl_sugg = result.get('sl_suggestion')
        tp_sugg = result.get('tp_suggestion')

        log_system("INFO", f"🤖 Gemini判定: {direction} → 有効={valid} 信頼度={confidence}% (SL={sl_sugg}, TP={tp_sugg})")
        log_system("INFO", f"📝 判定理由: {reason}")

        return {
            'valid': valid,
            'confidence': confidence,
            'reason': reason,
            'sl_suggestion': sl_sugg,
            'tp_suggestion': tp_sugg,
            'key_level': str(result.get('key_level', '')),
        }
    except Exception as e:
        err_str = str(e)
        print(f"❌ Gemini EAシグナル分析エラー: {err_str[:100]}")
        log_system("ERROR", f"❌ Gemini API エラー: {err_str[:150]}")
        is_quota = "クォータ制限中" in err_str or "429" in err_str or "quota" in err_str.lower()
        if is_quota:
            # クォータ時: valid=None/confidence=None で「判定不能」扱い（ダマシ扱いしない）
            log_system("WARNING", f"⏳ Gemini クォータ制限: {direction}シグナルは一時スキップ")
            return {'valid': None, 'confidence': None, 'reason': 'クォータ制限中 - 数分後に自動回復します',
                    'sl_suggestion': None, 'tp_suggestion': None, 'key_level': '', 'quota_error': True}
        return {'valid': False, 'confidence': 0, 'reason': err_str[:200],
                'sl_suggestion': None, 'tp_suggestion': None, 'key_level': ''}

# ==================== ポジション決済判定 Gemini 分析（AI_CLOSE_MODE用）====================
def gemini_position_close_decision(position, current_scores):
    """
    OPEN ポジションの決済判定を Gemini で実施（AI_CLOSE_MODE 専用）

    Args:
        position: Supabase trades テーブルの OPEN ポジション
        current_scores: 最新の EA スコア {buy_score, sell_score, adx, rsi, ...}

    Returns:
        {
            "should_close": bool,
            "confidence": 0-100,  # 決済信頼度
            "reason": str
        }
    """
    direction = position.get('direction', 'BUY')
    entry_price = float(position.get('entry_price', 0))
    direction_ja = "買いポジション（ロング）" if direction == 'BUY' else "売りポジション（ショート）"

    current_price = float(current_scores.get('close', entry_price))
    price_distance = abs(current_price - entry_price)

    prompt = f"""あなたはゴールド（XAUUSD）の上級テクニカルアナリストです。
保有中のポジションについて、今このタイミングで決済（損切・利確）すべきかを判定してください。

【保有ポジション】
方向: {direction_ja}
エントリー価格: {entry_price}
現在価格: {current_price:.2f}
損益: {price_distance:.2f}pips {'利益' if (current_price > entry_price and direction == 'BUY') or (current_price < entry_price and direction == 'SELL') else '損失'}

【最新テクニカル指標】
買いスコア: {current_scores.get('buy_score', 0)}点
売りスコア: {current_scores.get('sell_score', 0)}点
ADX: {current_scores.get('adx', 0):.1f}（トレンド強度）
RSI: {current_scores.get('rsi', 50):.1f}（過買売）

【プロの判定基準】
- {direction_ja}を持っている
- 「決済すべき」と判定する場合は以下のいずれかに該当:
  1. 逆方向のスコアが圧倒的に高い（例: BUY持ちなのに売りスコア≥5）
  2. ADX が低い（<20）レンジ相場での含み益 → 反転リスク高い
  3. サポート/レジスタンスレベルに接近している
     - SELL中に支持線（サポート）手前 → 買い戻しリスク
     - BUY中に抵抗線（レジスタンス）手前 → 売り圧力
  4. RSI が極端な過買売状態（RSI≥85 or RSI≤15）

【JSON回答形式（以下のみ）】
{{"should_close": true/false, "confidence": 0-100, "reason": "50文字以内"}}

決済信頼度:
- 100: 即座に決済すべき（逆方向がきわめて強い、またはレベル接近）
- 70-90: 決済を強く推奨（シグナル反転、レンジ含み益）
- 50-69: 決済を検討（リスク警告、レベル警戒）
- 30-49: 監視継続推奨（まだホールド）
- 0-29: 継続保有推奨（トレンド継続）
"""

    try:
        text = _gemini_generate(prompt, max_retries=1)
        if "```" in text:
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
        result = json.loads(text)
        should_close = bool(result.get('should_close', False))
        confidence = int(result.get('confidence', 0))
        reason = str(result.get('reason', ''))

        print(f"🔍 決済判定: {direction}ポジション → {'🔴決済' if should_close else '🟢保持'} (信頼度{confidence}%)")
        return {
            'should_close': should_close,
            'confidence': confidence,
            'reason': reason,
        }
    except Exception as e:
        err_str = str(e)
        print(f"⚠️ Gemini決済判定エラー: {err_str[:100]}")
        is_quota = "クォータ制限中" in err_str or "429" in err_str or "quota" in err_str.lower()
        if is_quota:
            return {'should_close': False, 'confidence': None, 'reason': 'Geminiクォータ制限中'}
        return {'should_close': False, 'confidence': 0, 'reason': f'判定エラー: {err_str[:80]}'}

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
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
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
                "gemini_model": GEMINI_MODELS[_current_gemini_model_index],
                "gemini_model_fallback_count": _gemini_model_fallback_count,
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
                # AI 決済判定の履歴
                "ai_decision_timestamp": _last_ai_decision.get("timestamp"),
                "ai_decision_type": _last_ai_decision.get("decision_type"),
                "ai_confidence_score": _last_ai_decision.get("confidence_score"),
                "ai_decision_reason": _last_ai_decision.get("decision_reason"),
                "ai_executed_action": _last_ai_decision.get("executed_action"),
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

def position_monitor_loop():
    """
    AI_CLOSE_MODE 用: ポジション監視＆決済判定ループ
    TIMEFRAME ごと（ローソク足確定時）に OPEN ポジションを監視して Gemini で決済判定
    """
    global _position_monitor_last, _last_ai_decision, _force_close_pending, _force_close_set_time
    log_system("INFO", f"🔄 ポジション監視ループ開始（{TIMEFRAME_MINUTES}分間隔）")

    while True:
        try:
            if TRADING_MODE != "AI_CLOSE_MODE":
                settings_changed.wait(timeout=60)
                settings_changed.clear()
                continue

            now = time.time()
            interval_seconds = TIMEFRAME_MINUTES * 60  # TIMEFRAME に応じた間隔
            if now - _position_monitor_last < interval_seconds:
                settings_changed.wait(timeout=60)
                settings_changed.clear()
                continue

            _position_monitor_last = now

            # OPEN ポジション取得
            resp = req.get(
                f"{SUPABASE_URL}/rest/v1/trades",
                params={"status": "eq.OPEN"},
                headers=supabase_headers(),
                timeout=10
            )
            open_positions = resp.json() if resp.ok else []

            if not open_positions:
                print("✅ OPEN ポジションなし")
                settings_changed.wait(timeout=60)
                settings_changed.clear()
                continue

            print(f"📊 ポジション監視: {len(open_positions)}件の OPEN ポジション")

            # 各ポジションに対して決済判定を実施
            for pos in open_positions:
                if not _ea_latest_scores:
                    print("⏳ 最新スコア待機中...")
                    continue

                pos_id = pos.get('id')
                pos_side = pos.get('direction', '').upper()  # tradesテーブルは'direction'フィールド
                crossover = _ea_latest_scores.get('crossover')
                current_close = _ea_latest_scores.get('close', 0.0)
                entry_price = float(pos.get('entry_price', 0.0))
                adx = _ea_latest_scores.get('adx', 0.0)

                # 【優先度1】現在のシグナルが低信頼度 → 即座に決済（最優先）
                # AI が「今危険」と判定したら、ポジション方向に関わらず決済
                if crossover:
                    # 最新シグナルを Supabase から取得
                    latest_signal_resp = req.get(
                        f"{SUPABASE_URL}/rest/v1/signals",
                        params={"order": "created_at.desc", "limit": "1"},
                        headers=supabase_headers(),
                        timeout=10
                    )
                    latest_signals = latest_signal_resp.json() if latest_signal_resp.ok else []
                    latest_signal = latest_signals[0] if latest_signals else None
                    latest_confidence = latest_signal.get('ai_confidence') if latest_signal else None

                    # 現在のシグナルが低信頼度（ダマシ判定）なら即座に決済
                    if latest_confidence is not None and latest_confidence < CLOSE_CONFIDENCE_THRESHOLD:
                        print(f"🚨 危険シグナル検出: 現在の信頼度={latest_confidence}%（< {CLOSE_CONFIDENCE_THRESHOLD}%）")
                        print(f"⚠️ 低信頼度決済: ポジション#{pos_id} ({pos_side}ポジション中に{crossover}だが信頼度={latest_confidence}%で危険)")
                        try:
                            req.patch(
                                f"{SUPABASE_URL}/rest/v1/trades",
                                params={"id": f"eq.{pos_id}"},
                                json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                                headers={**supabase_headers(), "Prefer": "return=minimal"},
                                timeout=10
                            )
                            log_system("INFO", f"🚨 決済: ポジション#{pos_id} 現在のシグナルが危険（信頼度={latest_confidence}%<{CLOSE_CONFIDENCE_THRESHOLD}%）で即座に決済")
                            _last_ai_decision["decision_type"] = "CLOSE"
                            _last_ai_decision["confidence_score"] = 100
                            _last_ai_decision["decision_reason"] = f"現在のシグナルが危険と判定（信頼度{latest_confidence}%<{CLOSE_CONFIDENCE_THRESHOLD}%）。AI警告に従い即座に損切り。"
                            _last_ai_decision["executed_action"] = f"CLOSE_LOW_CONFIDENCE (ポジション#{pos_id})"
                            continue
                        except Exception as e:
                            print(f"❌ 低信頼度決済エラー: {e}")
                            log_system("ERROR", f"低信頼度決済エラー: {e}")

                # 【優先度2】レンジ相場での利益確定判定
                if adx < 20 and current_close > 0 and entry_price > 0:
                    pnl_pips = abs(current_close - entry_price)
                    has_profit = False

                    if pos_side == 'SELL' and entry_price > current_close and pnl_pips > 0:
                        has_profit = True
                    elif pos_side == 'BUY' and current_close > entry_price and pnl_pips > 0:
                        has_profit = True

                    if has_profit:
                        print(f"🎯 レンジ相場での利益確定: ポジション#{pos_id} "
                              f"(ADX={adx:.1f}<20, 含み益={pnl_pips:.2f}pips)")
                        try:
                            req.patch(
                                f"{SUPABASE_URL}/rest/v1/trades",
                                params={"id": f"eq.{pos_id}"},
                                json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                                headers={**supabase_headers(), "Prefer": "return=minimal"},
                                timeout=10
                            )
                            log_system("INFO", f"🎯 決済: ポジション#{pos_id} レンジ相場での利益確定（ADX={adx:.1f}, 含み益={pnl_pips:.2f}pips）")
                            _last_ai_decision = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "decision_type": "CLOSE",
                                "crossover_direction": None,
                                "confidence_score": 95,
                                "decision_reason": f"レンジ相場（ADX={adx:.1f}<20）で含み益あり。反転リスク回避のため決済。",
                                "executed_action": f"CLOSE_PROFIT_LOCK (ポジション#{pos_id})",
                            }
                            continue
                        except Exception as e:
                            print(f"❌ レンジ相場決済エラー: {e}")
                            log_system("ERROR", f"レンジ相場決済エラー: {e}")

                # 【優先度3】シグナル消滅（NONE）での決済判定
                last_signal = _last_position_signal.get(pos_id)
                if last_signal is not None and crossover is None:
                    # 前回はシグナルがあったのに、今回はない = トレンド終了
                    current_pnl = abs(current_close - entry_price) if current_close > 0 and entry_price > 0 else 0
                    print(f"📉 シグナル消滅決済: ポジション#{pos_id} (前回:{last_signal} → 今回:NONE = トレンド終了)")
                    try:
                        req.patch(
                            f"{SUPABASE_URL}/rest/v1/trades",
                            params={"id": f"eq.{pos_id}"},
                            json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                            headers={**supabase_headers(), "Prefer": "return=minimal"},
                            timeout=10
                        )
                        log_system("INFO", f"📉 決済: ポジション#{pos_id} シグナル消滅（前回:{last_signal}→現在:NONE、トレンド終了、損益={current_pnl:.2f}pips）")
                        _last_ai_decision = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "decision_type": "CLOSE",
                            "crossover_direction": None,
                            "confidence_score": 90,
                            "decision_reason": f"シグナル消滅。前回{last_signal}だったがNONEに。トレンド終了判定。",
                            "executed_action": f"CLOSE_SIGNAL_NONE (ポジション#{pos_id})",
                        }
                        del _last_position_signal[pos_id]
                        continue
                    except Exception as e:
                        print(f"❌ シグナル消滅決済エラー: {e}")
                        log_system("ERROR", f"シグナル消滅決済エラー: {e}")

                # 【優先度4】逆方向シグナルでの決済
                if crossover and pos_side:
                    is_reverse = (crossover == "UP_CROSS" and pos_side == "SELL") or \
                                 (crossover == "DOWN_CROSS" and pos_side == "BUY")

                    if is_reverse:
                        current_pnl = abs(current_close - entry_price) if current_close > 0 and entry_price > 0 else 0
                        print(f"🔄 逆方向シグナル決済: ポジション#{pos_id} "
                              f"({pos_side}ポジション中に{crossover})")
                        try:
                            req.patch(
                                f"{SUPABASE_URL}/rest/v1/trades",
                                params={"id": f"eq.{pos_id}"},
                                json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                                headers={**supabase_headers(), "Prefer": "return=minimal"},
                                timeout=10
                            )
                            log_system("INFO", f"🔄 決済: ポジション#{pos_id} 逆方向シグナル（{crossover}でエグジット、損益={current_pnl:.2f}pips）")
                            _last_ai_decision = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "decision_type": "CLOSE",
                                "crossover_direction": crossover,
                                "confidence_score": 100,
                                "decision_reason": f"逆方向シグナル検出。{pos_side}ポジション中に{crossover}が発生。",
                                "executed_action": f"CLOSE_REVERSE_SIGNAL (ポジション#{pos_id})",
                            }
                            del _last_position_signal[pos_id]
                            continue
                        except Exception as e:
                            print(f"❌ 逆方向決済エラー: {e}")
                            log_system("ERROR", f"逆方向決済エラー: {e}")

                # 前回のシグナルを記憶
                if crossover:
                    _last_position_signal[pos_id] = crossover

                # Gemini で決済判定
                close_decision = gemini_position_close_decision(pos, _ea_latest_scores)

                # ✅ AI 決済判定情報を記録
                _last_ai_decision = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "decision_type": "CLOSE" if close_decision['should_close'] else "HOLD",
                    "crossover_direction": None,
                    "confidence_score": close_decision.get('confidence'),
                    "decision_reason": close_decision.get('reason', ''),
                    "executed_action": "",
                }

                # 決済信頼度が閾値以上なら自動決済
                if close_decision['should_close'] and close_decision['confidence'] is not None:
                    if close_decision['confidence'] >= CLOSE_CONFIDENCE_THRESHOLD:
                        print(f"🔴 決済実行: ポジションID={pos.get('id')} "
                              f"信頼度={close_decision['confidence']}%")
                        # EA に force_close 指示を送る（/latest-signal 経由）
                        _force_close_pending = True
                        _force_close_set_time = time.time()
                        log_system("INFO", f"🔴 force_close フラグセット: Gemini決済指示（信頼度={close_decision['confidence']}%）")
                        # Supabase で status を CLOSED に更新
                        req.patch(
                            f"{SUPABASE_URL}/rest/v1/trades",
                            params={"id": f"eq.{pos.get('id')}"},
                            json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                            headers={**supabase_headers(), "Prefer": "return=minimal"},
                            timeout=10
                        )
                        _last_ai_decision["executed_action"] = f"CLOSE (ポジション#{pos.get('id')})"
                        # システムログに記録
                        log_system("INFO", f"AI判定: CLOSE → ポジション#{pos.get('id')} 信頼度={close_decision['confidence']}% 理由={close_decision.get('reason')}")
                    else:
                        print(f"⚠️ 監視継続: ポジションID={pos.get('id')} "
                              f"信頼度={close_decision['confidence']}%（閾値{CLOSE_CONFIDENCE_THRESHOLD}%未満）")
                        _last_ai_decision["executed_action"] = "継続監視"
                        # システムログに記録
                        log_system("INFO", f"AI判定: HOLD → ポジション#{pos.get('id')} 信頼度={close_decision['confidence']}% 理由={close_decision.get('reason')}")
                else:
                    # should_close=False の場合もログに記録
                    log_system("INFO", f"AI判定: HOLD → ポジション#{pos.get('id')} 理由={close_decision.get('reason')}")

            # 外部決済されたポジションのシグナルキャッシュをクリーンアップ
            open_ids = {pos.get('id') for pos in open_positions}
            stale_keys = [k for k in _last_position_signal if k not in open_ids]
            for k in stale_keys:
                del _last_position_signal[k]

            settings_changed.wait(timeout=60)
            settings_changed.clear()

        except Exception as e:
            print(f"❌ ポジション監視エラー: {e}")
            settings_changed.wait(timeout=60)
            settings_changed.clear()

_bg_jobs_started = False

def start_background_jobs():
    """バックグラウンドジョブ開始（gunicorn/直接起動どちらでも1回だけ実行）"""
    global _status_log_thread, _bg_jobs_started
    if _bg_jobs_started:
        return
    _bg_jobs_started = True
    log_system("INFO", "✅ バックグラウンドジョブ開始")

    _status_log_thread = threading.Thread(target=status_logging_loop, daemon=True)
    _status_log_thread.start()
    log_system("INFO", "✅ Status logging thread started")

    t_signal = threading.Thread(target=signal_loop, daemon=True, name="signal_loop")
    t_signal.start()
    log_system("INFO", "✅ Signal loop thread started")

    t_pos = threading.Thread(target=position_monitor_loop, daemon=True, name="position_monitor_loop")
    t_pos.start()
    log_system("INFO", "✅ Position monitor loop thread started")

    t_weekly = threading.Thread(target=weekly_analysis_loop, daemon=True, name="weekly_analysis_loop")
    t_weekly.start()
    log_system("INFO", "✅ Weekly analysis loop thread started")

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

@app.route("/api/system-logs", methods=["GET"])
def get_system_logs():
    """システムログを取得（Supabase から永続保存ログを取得、デフォルト 100 件）"""
    try:
        limit = int(request.args.get('limit', '100'))
        limit = min(limit, 1000)  # 最大 1000 件に制限

        # Supabase から取得（永続保存）
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/system_logs",
            params={"order": "timestamp.desc", "limit": limit},
            headers=supabase_headers(),
            timeout=5
        )
        if resp.ok:
            return jsonify(resp.json())
        else:
            # Supabase 取得失敗時はメモリから返却（フォールバック）
            log_system("WARNING", f"Supabase ログ取得失敗 {resp.status_code} → メモリから返却")
            with _system_logs_lock:
                logs = list(reversed(_system_logs[-limit:]))
            return jsonify(logs)
    except Exception as e:
        log_system("WARNING", f"システムログ取得エラー: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== シグナルループ（24/7自動稼働）====================
def signal_loop():
    """Yahoo Finance からデータ取得し、時間足ごとにシグナルを計算・配信
    EA稼働中（ハートビートあり）はスキップ → EAが/ea-signalでリアルタイムプッシュ
    YAHOO_FINANCE_ENABLED=false の場合は完全停止（EA運用時）"""
    yahoo_enabled = os.environ.get("YAHOO_FINANCE_ENABLED", "true").lower() == "true"
    if not yahoo_enabled:
        log_system("INFO", "⏸️  YAHOO_FINANCE_ENABLED=false → Yahoo Financeシグナルループを無効化")
        return  # スレッドを終了（コードは残したまま）
    log_system("INFO", f"🔄 シグナルループ開始 TF={TIMEFRAME_MINUTES}m MODE={'TEST' if TEST_MODE else 'PROD'}")
    # サーバー起動直後: EAが再接続する猶予を120秒与える（Renderデプロイ後の競合防止）
    log_system("INFO", "⏳ 起動待機: EA接続猶予120秒（Yahoo Finance開始を遅延）")
    settings_changed.wait(timeout=120)
    settings_changed.clear()
    log_system("INFO", "🔄 EA接続猶予終了 → シグナルループ本処理開始")
    while True:
        # EA稼働中はYahoo Finance処理をスキップ（EAがMT5リアルタイムデータをプッシュするため）
        # ハートビートまたはea-signal受信から300秒以内 → EA生存中
        ea_alive = (_ea_last_heartbeat > 0 and time.time() - _ea_last_heartbeat < 300) or \
                   (_last_ea_signal_time > 0 and time.time() - _last_ea_signal_time < 300)
        if ea_alive:
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
                # Gemini提案のSL/TPを使用（あれば）
                sl = signal_data.get('ai_sl_suggestion')
                tp = signal_data.get('ai_tp_suggestion')
                print(f"💡 AI提案SL/TP: SL={sl}, TP={tp}")
                mt5_result = send_mt5_order(
                    signal_id=db_id,
                    direction=auto_result["direction"],
                    entry_price=signal_data["latest_close"],
                    sl_price=sl,
                    tp_price=tp
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
    if not data: return jsonify({"error": "No JSON body"}), 400
    tf = int(data.get("timeframe", 30))
    if tf not in [1, 5, 15, 30, 60]:
        return jsonify({"error": f"Invalid timeframe: {tf}"}), 400
    if tf != TIMEFRAME_MINUTES:
        old_tf = TIMEFRAME_MINUTES
        TIMEFRAME_MINUTES = tf
        settings_changed.set()
        print(f"📊 時間足変更: {tf}分足（ループ即座再開）")
        log_system("INFO", f"⚙️ 設定変更: 時間足 {old_tf}分 → {tf}分")
    return jsonify({"status": "ok", "timeframe": tf})

@app.route("/api/settings/mode", methods=["POST", "OPTIONS"])
def set_mode():
    global TEST_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error": "No JSON body"}), 400
    mode = data.get("mode", "PRODUCTION")
    new_test = (mode == "TEST")
    if new_test != TEST_MODE:
        TEST_MODE = new_test
        settings_changed.set()
        print(f"{'🧪 TEST_MODE ON' if TEST_MODE else '🚀 PRODUCTION ON'}（ループ即座再開）")
        log_system("INFO", f"⚙️ 設定変更: 運用モード {'TEST' if TEST_MODE else 'PRODUCTION'}")
    return jsonify({"status": "ok", "mode": mode, "test_mode": TEST_MODE})

@app.route("/api/settings/crossover", methods=["POST", "OPTIONS"])
def set_crossover():
    global CROSSOVER_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error": "No JSON body"}), 400
    mode = data.get("crossover_mode", "RSI")
    if mode not in ["RSI", "MACD", "RSI_MACD", "COMPOSITE"]:
        return jsonify({"error": f"Invalid crossover_mode: {mode}"}), 400
    if mode != CROSSOVER_MODE:
        old_mode = CROSSOVER_MODE
        CROSSOVER_MODE = mode
        settings_changed.set()
        print(f"📊 クロスオーバー方式変更: {mode}（ループ即座再開）")
        log_system("INFO", f"⚙️ 設定変更: クロスオーバー方式 {old_mode} → {mode}")
    return jsonify({"status": "ok", "crossover_mode": mode})

@app.route("/api/settings/trading-mode", methods=["POST", "OPTIONS"])
def set_trading_mode():
    global TRADING_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error": "No JSON body"}), 400
    mode = data.get("trading_mode", "MANUAL")
    valid_modes = ["MANUAL", "SEMI_AUTO", "FULL_AUTO", "AI_CLOSE_MODE"]
    if mode not in valid_modes:
        return jsonify({"error": f"Invalid trading_mode: {mode}. Valid: {valid_modes}"}), 400
    old_mode = TRADING_MODE
    TRADING_MODE = mode
    print(f"🔄 トレードモード変更: {mode}")
    log_system("INFO", f"⚙️ 設定変更: トレードモード {old_mode} → {mode}")
    settings_changed.set()  # signal_loop の即座再開を通知
    return jsonify({
        "status": "ok",
        "trading_mode": mode,
        "description": {
            "MANUAL": "シグナル配信のみ（ユーザーが手動判断）",
            "SEMI_AUTO": "シグナル配信 + スマホボタンで注文",
            "FULL_AUTO": "Gemini信頼度で自動エントリー",
            "AI_CLOSE_MODE": "信頼度でエントリー + 定期監視で決済判定"
        }.get(mode, "")
    })

@app.route("/api/settings/auto-threshold", methods=["POST", "OPTIONS"])
def set_auto_threshold():
    global AUTO_CONFIDENCE_THRESHOLD
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error": "No JSON body"}), 400
    threshold = int(data.get("threshold", 70))
    if not (0 <= threshold <= 100):
        return jsonify({"error": "threshold must be 0-100"}), 400
    old_threshold = AUTO_CONFIDENCE_THRESHOLD
    AUTO_CONFIDENCE_THRESHOLD = threshold
    print(f"🎯 自動実行閾値変更: {threshold}%")
    log_system("INFO", f"⚙️ 設定変更: 自動実行閾値 {old_threshold}% → {threshold}%")
    return jsonify({"status": "ok", "threshold": threshold})

@app.route("/api/execute-order", methods=["POST", "OPTIONS"])
def execute_order():
    """SEMI_AUTO: スマホのボタンから手動トリガーで MT5 に注文送信"""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    if not data: return jsonify({"error": "No JSON body"}), 400
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
        "mt5_webhook_configured": bool(MT5_WEBHOOK_URL),
        "hybrid_sl": _hybrid_sl_config
    })

@app.route("/api/settings/hybrid-sl", methods=["GET", "POST", "OPTIONS"])
def hybrid_sl_settings():
    """ハイブリッドSL設定（v1.25）- アプリから動的に調整可能"""
    global _hybrid_sl_config

    if request.method == "OPTIONS":
        return jsonify({}), 200

    if request.method == "GET":
        return jsonify(_hybrid_sl_config)

    if request.method == "POST":
        data = request.get_json()
        old_config = _hybrid_sl_config.copy()

        # 設定を更新
        if "initial_sl_usd" in data:
            _hybrid_sl_config["initial_sl_usd"] = float(data["initial_sl_usd"])
        if "trailing_trigger_usd" in data:
            _hybrid_sl_config["trailing_trigger_usd"] = float(data["trailing_trigger_usd"])
        if "trailing_sl_usd" in data:
            _hybrid_sl_config["trailing_sl_usd"] = float(data["trailing_sl_usd"])
        if "enabled" in data:
            _hybrid_sl_config["enabled"] = bool(data["enabled"])

        log_system("INFO", f"⚙️ ハイブリッドSL設定変更: {old_config} → {_hybrid_sl_config}")
        print(f"🎯 ハイブリッドSL設定変更: {_hybrid_sl_config}")

        return jsonify({
            "status": "ok",
            "hybrid_sl": _hybrid_sl_config,
            "message": "ハイブリッドSL設定が更新されました"
        })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(), "mode": "yahoo-finance"})

@app.route("/latest-signal", methods=["GET"])
def latest_signal():
    global _force_close_pending, _force_close_set_time
    try:
        resp = req.get(
            f"{SUPABASE_URL}/rest/v1/signals",
            params={"select": "*", "order": "created_at.desc", "limit": "1"},
            headers=supabase_headers(),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        signal = data[0] if data else {}

        # force_close フラグを付加（AI_CLOSE_MODE でGeminiが決済指示を出した場合）
        # 120秒後に自動リセット（EAが確実に受け取れる猶予）
        if _force_close_pending and time.time() - _force_close_set_time > 120:
            _force_close_pending = False
            print("⏱️ force_close フラグ自動リセット（120秒経過）")
        signal["force_close"] = _force_close_pending

        return jsonify(signal)
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
        rows = resp.json()
        return jsonify(rows[0] if rows else {}), 201
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
        rows = resp.json()
        return jsonify(rows[0] if rows else {}), 200
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

@app.route("/api/health-check", methods=["GET"])
def health_check():
    """システム監視用・健康状態詳細チェック（Claude管理者用）"""
    now = time.time()
    issues = []
    warnings = []

    # ========== 通信状態監視（新機能） ==========
    # 最新ログの鮮度チェック
    latest_log_age = None
    if _system_logs:
        try:
            latest_timestamp_str = _system_logs[-1].get('timestamp', '')
            latest_timestamp = datetime.fromisoformat(latest_timestamp_str.replace('Z', '+00:00')).timestamp()
            latest_log_age = now - latest_timestamp

            if latest_log_age > 300:  # 5分以上古い
                issues.append(f"🚨 ログが古い（{round(latest_log_age/60)}分以上更新なし）→ 通信遅延の可能性")
            elif latest_log_age > 120:  # 2分以上
                warnings.append(f"⚠️ ログ更新が遅い（最新ログ{round(latest_log_age)}秒前）")
        except:
            pass

    # EA接続確認
    ea_alive = (_ea_last_heartbeat > 0 and now - _ea_last_heartbeat < 90)
    if not ea_alive:
        issues.append("❌ EA未接続（MT5が停止している可能性）")
    elif now - _ea_last_heartbeat > 60:
        warnings.append(f"⚠️ EA遅延（最後のハートビート{round(now - _ea_last_heartbeat)}秒前）")

    # Gemini API確認
    if _last_gemini_call_time and now - _last_gemini_call_time > 3600:
        warnings.append("⚠️ Gemini未使用（1時間以上判定なし）")

    # シグナル受信確認
    if _last_ea_signal_time and now - _last_ea_signal_time > 1800:
        warnings.append(f"⚠️ シグナル受信なし（最後{round((now - _last_ea_signal_time)/60)}分前）→ 古い情報の可能性")

    # データキャッシュ検証
    if _ea_latest_scores.get('updated_at'):
        try:
            scores_age_str = _ea_latest_scores.get('updated_at', '')
            scores_age = now - datetime.fromisoformat(scores_age_str.replace('Z', '+00:00')).timestamp()
            if scores_age > 600:  # 10分以上
                warnings.append(f"⚠️ EA スコアが古い（{round(scores_age/60)}分前）→ キャッシュデータの可能性")
        except:
            pass

    # ログエラー確認
    error_count = sum(1 for log in _system_logs if log.get('level') == 'ERROR')
    if error_count > 5:
        issues.append(f"❌ ログエラー多発（最近{error_count}件）")

    # サーバー稼働時間確認
    uptime_hours = (now - _server_start_time) / 3600

    health_score = 100
    if issues:
        health_score -= 50
    if warnings:
        health_score -= 20

    return jsonify({
        "health_score": max(0, health_score),
        "status": "🟢 正常" if health_score >= 80 else ("🟡 注意" if health_score >= 50 else "🔴 問題あり"),
        "issues": issues,
        "warnings": warnings,
        "system": {
            "uptime_hours": round(uptime_hours, 1),
            "ea_alive": ea_alive,
            "ea_last_heartbeat_sec_ago": round(now - _ea_last_heartbeat) if _ea_last_heartbeat else None,
            "gemini_last_call_sec_ago": round(now - _last_gemini_call_time) if _last_gemini_call_time else None,
            "last_signal_push_sec_ago": round(now - _last_ea_signal_time) if _last_ea_signal_time else None,
            "latest_log_age_sec": round(latest_log_age) if latest_log_age else None,
            "recent_error_count": error_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
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
    global _last_ea_signal_time, _last_gemini_approved, _last_gemini_direction, _ea_signal_dedup, _last_ai_decision
    global _last_gemini_close_call_time, _force_close_pending, _force_close_set_time
    if request.method == "OPTIONS":
        return jsonify({}), 200

    ea_data = request.get_json()
    if not ea_data:
        return jsonify({"error": "No data"}), 400

    crossover = ea_data.get('crossover', '')
    if crossover not in ('UP_CROSS', 'DOWN_CROSS'):
        return jsonify({"error": f"Invalid crossover: {crossover}"}), 400

    # ① 重複防止: 同一キャンドル内の同方向シグナルはスキップ
    # TIMEFRAME * 60 - 30秒 以内の同方向は「同キャンドルの再送」として扱う
    # 方向が変わった場合は時間に関係なく処理する
    now = time.time()
    last_recv = _ea_signal_dedup.get(crossover, 0)
    candle_dedup_seconds = max(TIMEFRAME_MINUTES * 60 - 30, 60)  # M15=870s, 最低60s
    if crossover == _ea_signal_dedup.get("last_direction") and now - last_recv < candle_dedup_seconds:
        _last_ea_signal_time = time.time()
        print(f"⏭️ /ea-signal 同キャンドル重複スキップ: {crossover} ({int(now - last_recv)}秒前に処理済み)")
        return jsonify({"status": "skipped", "reason": "same_candle_duplicate"}), 200
    _ea_signal_dedup[crossover] = now
    _ea_signal_dedup["last_direction"] = crossover

    print(f"📡 EA→サーバー シグナル受信: {crossover} "
          f"close={ea_data.get('latest_close')} "
          f"買い{ea_data.get('buy_score')}点 vs 売り{ea_data.get('sell_score')}点 "
          f"ADX={ea_data.get('adx')} mode={TRADING_MODE}")

    # シグナル受信を詳細ログに記録
    log_system("INFO", f"📡 シグナル受信: {crossover} @ {ea_data.get('latest_close')}円 (買={ea_data.get('buy_score')}, 売={ea_data.get('sell_score')}, ADX={ea_data.get('adx')})")
    _last_ea_signal_time = time.time()

    # 保有中のポジション含み益をログに記録（透明性向上）
    log_position_status()

    # ==================== v1.15 ローソク足確定時刻チェック ====================
    # シグナルが来たのがローソク足確定時か中盤かを判定
    # 確定時のみ AI 判定を実行、中盤はシグナル表示のみ（省エネ）
    now_utc = datetime.now(timezone.utc)
    current_minute = now_utc.minute
    seconds_in_minute = now_utc.second

    # ローソク足確定までの秒数を計算
    minutes_until_close = (TIMEFRAME_MINUTES - (current_minute % TIMEFRAME_MINUTES)) % TIMEFRAME_MINUTES
    seconds_until_close = (minutes_until_close * 60) - seconds_in_minute

    # 確定時刻判定: 秒数 < 5秒 または > (TIMEFRAME * 60 - 5) なら「確定時」
    IS_CANDLE_CONFIRMATION = seconds_until_close < 5 or seconds_until_close > (TIMEFRAME_MINUTES * 60 - 5)

    open_positions = []  # 後段のAI_CLOSE_MODEブロックでも参照できるよう初期化

    # 【v1.27改改改】ローソク足確定チェック：TRADING_MODE に応じて分岐
    # MANUAL/SEMI_AUTO: ローソク足確定時のみ AI判定（省エネ）
    # AI_CLOSE_MODE: 常に AI判定を実行（決済判定が毎回必要）
    if not IS_CANDLE_CONFIRMATION and TRADING_MODE in ["MANUAL", "SEMI_AUTO"]:
        # MANUAL/SEMI_AUTO モードのみ、ローソク足中盤でスキップ
        print(f"⏳ ローソク足中盤（確定まで {seconds_until_close}秒）→ シグナル表示のみ（AI判定スキップ）")
        ai = {"valid": None, "confidence": None, "reason": "ローソク足中盤（確定待機中）"}
        _last_gemini_approved = False
        _last_gemini_direction = crossover
        log_system("INFO", f"シグナル受信: {crossover} → ローソク足中盤（AI判定スキップ、確定待機中）")
    # ローソク足確定時のみ、以下の AI 判定処理を実行
    elif TRADING_MODE in ["MANUAL", "SEMI_AUTO"] and IS_CANDLE_CONFIRMATION:
        # 手動・半自動モード: Gemini 分析スキップ（ユーザー判断に委ねる）
        print(f"⏭️ {TRADING_MODE}モード: Gemini分析スキップ（ユーザー判断）")
        ai = {"valid": None, "confidence": None, "reason": f"{TRADING_MODE}モード"}
        _last_gemini_approved = False
        _last_gemini_direction = crossover
        log_system("INFO", f"シグナル受信: {crossover} → {TRADING_MODE}モード（ユーザー判断に委ねる）")
    else:
        # FULL_AUTO / AI_CLOSE_MODE: OPEN ポジション確認（省エネ対応）
        try:
            resp = req.get(
                f"{SUPABASE_URL}/rest/v1/trades",
                params={"status": "eq.OPEN"},
                headers=supabase_headers(),
                timeout=5
            )
            open_positions = resp.json() if resp.ok else []
        except Exception as e:
            print(f"⚠️ OPEN ポジション確認エラー: {e}")
            open_positions = []

        if open_positions:
            # ポジション保有中: 新シグナルの信頼度を判定（決済判定が必要）+ ポジション情報を反映
            print(f"✅ OPEN ポジション{len(open_positions)}件保有中 → 新シグナル信頼度を判定（含み損益を考慮）")
            ai = gemini_analyze_ea_signal(ea_data, open_positions=open_positions)
            _last_gemini_approved = bool(ai.get('valid'))
            # ✅ AI 判定情報を記録
            _last_ai_decision = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "decision_type": "CLOSE" if ai.get('confidence') is not None and ai.get('confidence') < CLOSE_CONFIDENCE_THRESHOLD else "HOLD",
                "crossover_direction": crossover,
                "confidence_score": ai.get('confidence'),
                "decision_reason": ai.get('reason', ''),
                "executed_action": f"決済待機中（信頼度判定）",
            }
            log_system("INFO", f"AI判定: ポジション保有中の新シグナル → 信頼度={ai.get('confidence')}% 有効={ai.get('valid')}")
        else:
            # ポジション保有なし: Gemini分析実行（エントリー判定）
            ai = gemini_analyze_ea_signal(ea_data)
            _last_gemini_approved = bool(ai.get('valid'))
            # ✅ AI 判定情報を記録（v1.27準備）
            _last_ai_decision = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "decision_type": "ENTRY",
                "crossover_direction": crossover,
                "confidence_score": ai.get('confidence'),
                "decision_reason": ai.get('reason', ''),
                "executed_action": f"{'SELL' if crossover == 'DOWN_CROSS' else 'BUY'} (待機中)" if ai.get('valid') else "スキップ",
            }
            log_system("INFO", f"AI判定: {crossover} → 有効={ai.get('valid')} 信頼度={ai.get('confidence')}% 理由={ai.get('reason')}")
        _last_gemini_direction = crossover

    # ==================== AI_CLOSE_MODE: クロスオーバー時のGemini決済判断 ====================
    # クロスが出るたびに実行（最低60秒クールダウン）- 15分ループと独立して判断
    if TRADING_MODE == "AI_CLOSE_MODE" and open_positions and \
            time.time() - _last_gemini_close_call_time >= 60:
        try:
            current_scores = {
                "buy_score":  ea_data.get("buy_score", 0),
                "sell_score": ea_data.get("sell_score", 0),
                "adx":        float(ea_data.get("adx", 0)),
                "rsi":        float(ea_data.get("rsi", 0)),
                "close":      float(ea_data.get("latest_close", 0)),
            }
            for pos in open_positions:
                close_result = gemini_position_close_decision(pos, current_scores)
                if close_result.get("should_close") and \
                        close_result.get("confidence", 0) >= CLOSE_CONFIDENCE_THRESHOLD:
                    _force_close_pending = True
                    _force_close_set_time = time.time()
                    log_system("INFO",
                        f"🔴 クロスオーバー決済判断: ポジション#{pos.get('id')} → Gemini決済指示 "
                        f"(信頼度{close_result.get('confidence')}% 理由:{close_result.get('reason','')})")
                    print(f"🔴 AI_CLOSE_MODE: クロス時決済指示 → force_close=True "
                          f"(conf={close_result.get('confidence')}%)")
                    break
            _last_gemini_close_call_time = time.time()
        except Exception as e:
            print(f"⚠️ AI_CLOSE_MODE クロスオーバー決済判断エラー: {e}")
            log_system("WARNING", f"AI_CLOSE_MODE クロスオーバー決済判断エラー: {e}")

    # 🚨 【v1.37改】新シグナルが低信頼度の場合、OPEN ポジションを即座に決済（リアルタイム）
    if crossover and ai.get('confidence') is not None:
        if ai.get('confidence') < CLOSE_CONFIDENCE_THRESHOLD:
            print(f"🚨 新シグナル危険検出: 信頼度={ai.get('confidence')}%（< {CLOSE_CONFIDENCE_THRESHOLD}%）")
            try:
                # OPEN ポジション確認
                close_pos_resp = req.get(
                    f"{SUPABASE_URL}/rest/v1/trades",
                    params={"status": "eq.OPEN"},
                    headers=supabase_headers(),
                    timeout=5
                )
                open_positions = close_pos_resp.json() if close_pos_resp.ok else []

                # 各 OPEN ポジションを即座に決済
                for pos in open_positions:
                    try:
                        req.patch(
                            f"{SUPABASE_URL}/rest/v1/trades",
                            params={"id": f"eq.{pos['id']}"},
                            json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                            headers={**supabase_headers(), "Prefer": "return=minimal"},
                            timeout=5
                        )
                        log_system("INFO", f"🚨 即座決済: 新シグナル危険（信頼度{ai.get('confidence')}%<{CLOSE_CONFIDENCE_THRESHOLD}%）でポジション#{pos['id']}を決済")
                        print(f"✅ 即座決済完了: ポジション#{pos['id']}")
                        _last_ai_decision["decision_type"] = "CLOSE"
                        _last_ai_decision["decision_reason"] = f"新シグナル危険（信頼度{ai.get('confidence')}%）で即座決済"
                        _last_ai_decision["executed_action"] = f"CLOSE_IMMEDIATE (ポジション#{pos['id']})"
                    except Exception as e:
                        print(f"❌ ポジション決済エラー: {e}")
                        log_system("ERROR", f"新シグナル即座決済エラー: {e}")
            except Exception as e:
                print(f"❌ OPEN ポジション取得エラー: {e}")
                log_system("ERROR", f"OPEN ポジション取得エラー（新シグナル判定）: {e}")

    # 【フェーズ2】シグナル消滅での決済
    if crossover is None:
        """前回シグナルがあったが、現在消滅した場合 → トレンド終了と判定して決済"""
        try:
            close_pos_resp = req.get(
                f"{SUPABASE_URL}/rest/v1/trades",
                params={"status": "eq.OPEN"},
                headers=supabase_headers(),
                timeout=5
            )
            open_positions = close_pos_resp.json() if close_pos_resp.ok else []

            for pos in open_positions:
                pos_id = pos.get('id')
                last_signal = _last_position_signal.get(pos_id)

                # 前回はシグナルがあったが、今回は消滅 → トレンド終了
                if last_signal is not None:
                    try:
                        entry_price = float(pos.get('entry_price', 0))
                        current_price = float(ea_data.get('latest_close', 0))
                        current_pnl = abs(current_price - entry_price) if current_price > 0 and entry_price > 0 else 0

                        req.patch(
                            f"{SUPABASE_URL}/rest/v1/trades",
                            params={"id": f"eq.{pos_id}"},
                            json={"status": "CLOSED", "close_time": datetime.now(timezone.utc).isoformat()},
                            headers={**supabase_headers(), "Prefer": "return=minimal"},
                            timeout=5
                        )

                        log_system("INFO", f"📉 決済: ポジション#{pos_id} シグナル消滅（前回:{last_signal}→現在:NONE、トレンド終了、損益={current_pnl:.2f}pips）")
                        print(f"📉 シグナル消滅決済: ポジション#{pos_id} (前回:{last_signal} → 今回:NONE = トレンド終了)")

                        _last_ai_decision = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "decision_type": "CLOSE",
                            "crossover_direction": None,
                            "confidence_score": 90,
                            "decision_reason": f"シグナル消滅。前回{last_signal}だったがNONEに。トレンド終了判定。",
                            "executed_action": f"CLOSE_SIGNAL_NONE (ポジション#{pos_id})",
                        }

                        del _last_position_signal[pos_id]

                    except Exception as e:
                        print(f"❌ シグナル消滅決済エラー: {e}")
                        log_system("ERROR", f"シグナル消滅決済エラー: {e}")
        except Exception as e:
            print(f"❌ OPEN ポジション取得エラー（シグナル消滅判定）: {e}")
            log_system("ERROR", f"OPEN ポジション取得エラー（シグナル消滅判定）: {e}")

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
        'timeframe':        TIMEFRAME_MINUTES,
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
            # 決済 → ticket でマッチする OPEN レコードを更新（なければ方向で検索）
            close_dir = direction.replace("_CLOSE", "")
            close_price = float(data.get("close_price") or data.get("price") or 0)
            mt5_profit = data.get("profit")        # MT5実際の損益（USD）
            close_reason = data.get("close_reason", "UNKNOWN")

            # ① ticket番号でノートを検索
            existing = []
            if ticket and str(ticket) != "?":
                try:
                    existing = req.get(
                        f"{SUPABASE_URL}/rest/v1/trades",
                        params={"status": "eq.OPEN", "notes": f"like.*ticket={ticket}*"},
                        headers=supabase_headers(), timeout=10
                    ).json()
                except Exception:
                    pass

            # ② ticket で見つからなければ方向+最新で検索
            if not existing:
                existing = req.get(
                    f"{SUPABASE_URL}/rest/v1/trades",
                    params={"status": "eq.OPEN", "direction": f"eq.{close_dir}",
                            "order": "entry_time.desc", "limit": "1"},
                    headers=supabase_headers(), timeout=10
                ).json()

            if existing:
                trade_id = existing[0]["id"]
                entry_price = float(existing[0].get("entry_price", 0))
                # MT5の実損益があればそれを優先、なければ価格差から計算
                if mt5_profit is not None:
                    profit_loss = round(float(mt5_profit), 2)
                elif close_price and entry_price:
                    profit_loss = round(
                        (close_price - entry_price) * (1 if close_dir == "BUY" else -1), 2
                    )
                else:
                    profit_loss = 0
                pips = round(profit_loss * 10, 1)
                status = "CLOSED_PROFIT" if profit_loss >= 0 else "CLOSED_LOSS"
                patch = {
                    "exit_price": close_price or float(data.get("price", 0)),
                    "exit_time": now_iso,
                    "profit_loss": profit_loss,
                    "pips": pips,
                    "status": status,
                    "updated_at": now_iso,
                    "notes": existing[0].get("notes", "") + f" | closed_by={close_reason}",
                }
                resp = req.patch(
                    f"{SUPABASE_URL}/rest/v1/trades",
                    params={"id": f"eq.{trade_id}"},
                    json=patch,
                    headers={**supabase_headers(), "Prefer": "return=minimal"},
                    timeout=10
                )
                print(f"✅ EA決済 Supabase更新完了 trade_id={trade_id} P/L={profit_loss} reason={close_reason} ({status})")
                log_system("INFO", f"EA決済: {close_dir} ticket={ticket} P/L={profit_loss} reason={close_reason}")
            else:
                print(f"⚠️  EA決済: 対応するOPENトレードが見つかりません ticket={ticket} dir={close_dir}")
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
            parts = text.split("```"); text = parts[1].replace("json", "").strip() if len(parts) >= 2 else text
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
# gunicorn でも直接起動でも必ず実行されるモジュールレベル初期化
load_fcm_tokens()
start_background_jobs()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
