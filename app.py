"""
Render デプロイ用 Flask バックエンド
- MT5 シグナル計算（GOLD RSI クロスオーバー + Gemini ダマシ判定）
- WebSocket で Capacitor アプリにリアルタイム送信
- Supabase REST API にシグナル履歴保存（supabase パッケージ不使用）
"""

import os
import json
import time
import threading
from datetime import datetime, timezone

import requests as req
from flask import Flask, jsonify
from flask_socketio import SocketIO, emit

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

import numpy as np
import pandas as pd
from google import genai

# ==================== 環境変数 ====================
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_KEY"]
MT5_LOGIN        = int(os.environ.get("MT5_LOGIN", "75611028"))
MT5_PASSWORD     = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER       = os.environ.get("MT5_SERVER", "XMTrading-MT5 3")
SIGNAL_INTERVAL  = int(os.environ.get("SIGNAL_INTERVAL", "1800"))

# ==================== 初期化 ====================
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "goldtrader_secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

gemini_model = genai.Client(api_key=GEMINI_API_KEY)

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

# ==================== MT5 データ取得 ====================
def fetch_mt5_data():
    if not MT5_AVAILABLE:
        return None
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        print(f"MT5 接続失敗: {mt5.last_error()}")
        return None
    time.sleep(2)
    mt5.symbol_select("GOLD", True)
    time.sleep(1)
    rates = mt5.copy_rates_from_pos("GOLD", mt5.TIMEFRAME_M30, 0, 100)
    mt5.shutdown()
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(float)
    return df

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
        'time': df['time'].iloc[-1].isoformat()
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

# ==================== Supabase 保存（REST API）====================
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

# ==================== シグナルループ ====================
def signal_loop():
    print("🔄 シグナルループ開始")
    while True:
        try:
            df = fetch_mt5_data()
            signal = compute_signal(df)
            if signal is None:
                print("⚠️  シグナル計算失敗。スキップします。")
                time.sleep(SIGNAL_INTERVAL)
                continue
            if signal['crossover']:
                ai = gemini_validate(df, signal)
            else:
                ai = {'valid': None, 'confidence': None, 'reason': None}
            signal_data = {
                **signal,
                'ai_valid': ai['valid'],
                'ai_confidence': ai['confidence'],
                'ai_reason': ai['reason'],
                'generated_at': datetime.now(timezone.utc).isoformat()
            }
            socketio.emit('signal', signal_data)
            print(f"📡 シグナル送信: {signal_data}")
            save_signal_to_supabase(signal_data)
        except Exception as e:
            print(f"❌ シグナルループエラー: {e}")
        time.sleep(SIGNAL_INTERVAL)

# ==================== REST エンドポイント ====================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

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
    socketio.run(app, host="0.0.0.0", port=port)
