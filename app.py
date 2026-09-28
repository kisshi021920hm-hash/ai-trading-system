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
from google import genai
import firebase_admin
from firebase_admin import credentials, messaging

# ==================== 環境変数 ====================
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
PUSH_SECRET     = os.environ.get("PUSH_SECRET", "goldtrader_push_2026")

# 動的設定（APIで変更可能）
TIMEFRAME_MINUTES = int(os.environ.get("TIMEFRAME_MINUTES", "30"))
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# ==================== 初期化 ====================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "goldtrader_secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

gemini_model = genai.Client(api_key=GEMINI_API_KEY)

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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

# FCMトークン一覧（メモリ保持）
fcm_tokens: set[str] = set()

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
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

# ==================== シグナル計算 ====================
def compute_signal(df):
    if df is None or len(df) < 15:
        return None
    rsi = calculate_rsi(df['close'].values)
    signal_line = pd.Series(rsi).rolling(window=9).mean()
    rsi_arr = rsi.values
    sig_arr = signal_line.values
    cur_rsi, cur_sig = rsi_arr[-1], sig_arr[-1]
    prv_rsi, prv_sig = rsi_arr[-2], sig_arr[-2]
    if any(np.isnan(v) for v in [cur_rsi, cur_sig, prv_rsi, prv_sig]):
        return None
    crossover = None
    if prv_rsi < prv_sig and cur_rsi > cur_sig:
        crossover = "UP_CROSS"
    elif prv_rsi > prv_sig and cur_rsi < cur_sig:
        crossover = "DOWN_CROSS"
    return {
        'rsi': round(float(cur_rsi), 2),
        'signal_line': round(float(cur_sig), 2),
        'crossover': crossover,
        'latest_close': round(float(df['close'].iloc[-1]), 2),
        'time': str(df['time'].iloc[-1])
    }

# ==================== Gemini ダマシ判定 ====================
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
        response = gemini_model.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        text = response.text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            'valid': bool(result.get('valid', False)),
            'confidence': int(result.get('confidence', 0)),
            'reason': str(result.get('reason', ''))
        }
    except Exception as e:
        print(f"Gemini エラー: {e}")
        return {'valid': False, 'confidence': 0, 'reason': str(e)[:50]}

# ==================== Supabase 保存 ====================
def save_signal_to_supabase(signal_data):
    try:
        resp = req.post(
            f"{SUPABASE_URL}/rest/v1/signals",
            json={
                "symbol": "GOLD",
                "timeframe": "M30",
                "crossover": signal_data.get('crossover'),
                "rsi": signal_data.get('rsi'),
                "signal_line": signal_data.get('signal_line'),
                "latest_close": signal_data.get('latest_close'),
                "ai_valid": signal_data.get('ai_valid'),
                "ai_confidence": signal_data.get('ai_confidence'),
                "ai_reason": signal_data.get('ai_reason'),
                "created_at": datetime.now(timezone.utc).isoformat()
            },
            headers=supabase_headers(),
            timeout=10
        )
        resp.raise_for_status()
        print("✓ Supabase に保存完了")
    except Exception as e:
        print(f"⚠️  Supabase 保存エラー: {e}")

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
                time.sleep(tf * 60)
                continue

            if signal['crossover']:
                print(f"🎯 クロスオーバー検出: {signal['crossover']}")
                if TEST_MODE:
                    ai = {'valid': True, 'confidence': 50, 'reason': 'テストモード（AI省略）'}
                    print(f"🧪 TEST: RSI={signal['rsi']}, TF={tf}m")
                else:
                    ai = gemini_validate(df, signal)
            else:
                ai = {'valid': None, 'confidence': None, 'reason': None}
                print(f"⏸️  クロスオーバーなし RSI={signal['rsi']} TF={tf}m")

            signal_data = {
                **signal,
                'ai_valid': ai['valid'],
                'ai_confidence': ai['confidence'],
                'ai_reason': ai['reason'],
                'timeframe': tf,
                'test_mode': TEST_MODE,
                'generated_at': datetime.now(timezone.utc).isoformat()
            }

            socketio.emit('signal', signal_data)
            print(f"📡 シグナル配信完了: close={signal_data['latest_close']}")

            if signal_data.get('crossover') and signal_data.get('ai_valid'):
                send_fcm_push(signal_data)

            save_signal_to_supabase(signal_data)

        except Exception as e:
            print(f"❌ シグナルループエラー: {e}")

        time.sleep(TIMEFRAME_MINUTES * 60)

# ==================== FCM プッシュ送信 ====================
def send_fcm_push(signal_data):
    if not FCM_ENABLED or not fcm_tokens:
        return
    direction = "📈 買いシグナル" if signal_data.get('crossover') == "UP_CROSS" else "📉 売りシグナル"
    confidence = signal_data.get('ai_confidence', 0)
    reason = signal_data.get('ai_reason', '')
    invalid_tokens = set()
    for token in list(fcm_tokens):
        try:
            msg = messaging.Message(
                notification=messaging.Notification(
                    title=f"GOLD {direction}",
                    body=f"信頼度: {confidence}%  {reason}",
                ),
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id="gold-signal",
                        notification_count=1,
                    ),
                ),
                token=token,
            )
            messaging.send(msg)
            print(f"✓ FCMプッシュ送信完了: {token[:20]}...")
        except Exception as e:
            print(f"⚠️  FCM送信エラー ({token[:20]}...): {e}")
            if "registration-token-not-registered" in str(e) or "invalid-argument" in str(e):
                invalid_tokens.add(token)
    fcm_tokens.difference_update(invalid_tokens)

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

@app.route("/register-token", methods=["POST", "OPTIONS"])
def register_token():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    token = data.get("token") if data else None
    if not token:
        return jsonify({"error": "No token"}), 400
    fcm_tokens.add(token)
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
    print(f"📊 時間足変更: {tf}分足")
    return jsonify({"status": "ok", "timeframe": tf})

@app.route("/api/settings/mode", methods=["POST", "OPTIONS"])
def set_mode():
    global TEST_MODE
    if request.method == "OPTIONS":
        return jsonify({}), 200
    data = request.get_json()
    mode = data.get("mode", "PRODUCTION")
    TEST_MODE = (mode == "TEST")
    print(f"{'🧪 TEST_MODE ON' if TEST_MODE else '🚀 PRODUCTION ON'}")
    return jsonify({"status": "ok", "mode": mode, "test_mode": TEST_MODE})

@app.route("/api/settings/current", methods=["GET"])
def get_current_settings():
    return jsonify({
        "timeframe": TIMEFRAME_MINUTES,
        "mode": "TEST" if TEST_MODE else "PRODUCTION",
        "test_mode": TEST_MODE
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

# ==================== WebSocket イベント ====================
@socketio.on("connect")
def on_connect():
    print(f"📱 クライアント接続: {threading.current_thread().name}")

@socketio.on("disconnect")
def on_disconnect():
    print("📴 クライアント切断")

# ==================== 起動 ====================
if __name__ == "__main__":
    t = threading.Thread(target=signal_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
