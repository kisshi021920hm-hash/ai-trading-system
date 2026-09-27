import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import time
import os
import json
import requests
from google import genai
from google.genai import types

# ==================== 設定 ====================
LOGIN = int(os.environ.get("MT5_LOGIN", "75611028"))
PASSWORD = os.environ.get("MT5_PASSWORD", "")
SERVER = os.environ.get("MT5_SERVER", "XMTrading-MT5 3")
SYMBOL = "GOLD"
TIMEFRAME = mt5.TIMEFRAME_M30

# Gemini API キー（環境変数から取得）
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Render サーバー設定
RENDER_URL   = "https://ai-trading-system-81jb.onrender.com"
PUSH_SECRET  = os.environ.get("PUSH_SECRET", "goldtrader_push_2026")
LOOP_INTERVAL = 1800  # 30分（秒）

# ==================== Gemini 初期化 ====================
def init_gemini():
    """Gemini API を初期化"""
    if not GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY が設定されていません")
        return None
    return genai.Client(api_key=GEMINI_API_KEY)

# ==================== 接続 ====================
def connect_mt5():
    """MT5 に接続"""
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        print(f"❌ MT5 接続失敗: {mt5.last_error()}")
        return False
    time.sleep(2)
    print("✓ MT5 接続成功")
    return True

# ==================== RSI 計算 ====================
def calculate_rsi(close_prices, period=14):
    """RSI を計算"""
    close = pd.Series(close_prices).astype(float)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ==================== データ取得 ====================
def get_bars(symbol, timeframe, count=100):
    """MT5 からチャートデータを取得"""
    if not mt5.symbol_select(symbol, True):
        print(f"❌ シンボル選択失敗: {symbol} - {mt5.last_error()}")
        return None
    time.sleep(1)

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None:
        print(f"❌ データ取得失敗: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df['close'] = df['close'].astype(float)
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    return df

# ==================== シグナル判定 ====================
def detect_rsi_crossover(df):
    """RSI のクロスオーバーを検出"""
    if df is None or len(df) < 15:
        return None

    rsi = calculate_rsi(df['close'].values, period=14)
    signal_line = pd.Series(rsi).rolling(window=9).mean()

    rsi_array = rsi.values
    signal_array = signal_line.values

    current_rsi = rsi_array[-1]
    current_signal = signal_array[-1]
    prev_rsi = rsi_array[-2]
    prev_signal = signal_array[-2]

    if np.isnan(current_rsi) or np.isnan(current_signal) or np.isnan(prev_rsi) or np.isnan(prev_signal):
        return None

    crossover = None
    if prev_rsi < prev_signal and current_rsi > current_signal:
        crossover = "UP_CROSS"
    elif prev_rsi > prev_signal and current_rsi < current_signal:
        crossover = "DOWN_CROSS"

    return {
        'rsi': current_rsi,
        'signal_line': current_signal,
        'crossover': crossover,
        'time': df['time'].iloc[-1]
    }

# ==================== Gemini ダマシ判定 ====================
def analyze_signal_with_gemini(model, df, signal):
    """
    Gemini API でシグナルのダマシ判定を行う。
    戻り値: {'valid': bool, 'confidence': int, 'reason': str}
    """
    if model is None or signal['crossover'] is None:
        return {'valid': False, 'confidence': 0, 'reason': 'Gemini 未設定またはシグナルなし'}

    # 直近20本の価格サマリーを作成
    recent = df.tail(20)[['time', 'open', 'high', 'low', 'close']].copy()
    recent['time'] = recent['time'].astype(str)
    price_summary = recent.to_dict(orient='records')

    direction = "買い（ロング）" if signal['crossover'] == "UP_CROSS" else "売り（ショート）"

    prompt = f"""
あなたはゴールド（XAUUSD）の専門トレーダーです。
以下のテクニカル情報を分析し、このシグナルがダマシかどうか判定してください。

【シグナル情報】
- 方向: {direction}
- RSI: {signal['rsi']:.2f}
- シグナルライン: {signal['signal_line']:.2f}
- 発生時刻: {signal['time']}

【直近20本の価格データ（M30）】
{json.dumps(price_summary, ensure_ascii=False, indent=2)}

【判定基準】
1. RSI がオーバーボート（>70）またはオーバーソールド（<30）領域でのクロスは有効
2. 価格トレンドとRSIの乖離（ダイバージェンス）に注意
3. 急激な価格変動後のクロスはダマシの可能性が高い
4. ボリンジャーバンド的な価格の位置を考慮

以下の JSON 形式のみで回答してください（説明文は不要）：
{{
  "valid": true または false,
  "confidence": 0〜100の整数（シグナルの信頼度）,
  "reason": "判定理由を50文字以内で"
}}
"""

    try:
        response = model.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        text = response.text.strip()
        # JSON 部分を抽出
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        result = json.loads(text)
        return {
            'valid': result.get('valid', False),
            'confidence': int(result.get('confidence', 0)),
            'reason': result.get('reason', '')
        }
    except Exception as e:
        print(f"⚠️  Gemini 解析エラー: {e}")
        return {'valid': False, 'confidence': 0, 'reason': f'解析エラー: {str(e)}'}

# ==================== Render へシグナル送信 ====================
def push_to_render(signal):
    """シグナルをRenderサーバーに送信してスマホに配信する"""
    try:
        payload = {
            'symbol': 'GOLD',
            'timeframe': 'M30',
            'crossover': signal.get('crossover'),
            'rsi': round(float(signal['rsi']), 2) if signal.get('rsi') else None,
            'signal_line': round(float(signal['signal_line']), 2) if signal.get('signal_line') else None,
            'latest_close': round(float(signal.get('latest_close', 0)), 2),
            'ai_valid': signal.get('ai_valid'),
            'ai_confidence': signal.get('ai_confidence'),
            'ai_reason': signal.get('ai_reason'),
            'generated_at': datetime.now(timezone.utc).isoformat()
        }
        resp = requests.post(
            f"{RENDER_URL}/push-signal",
            json=payload,
            headers={"X-Push-Secret": PUSH_SECRET},
            timeout=15
        )
        if resp.status_code == 200:
            print("✓ Renderへの送信完了 → スマホに配信されました")
        else:
            print(f"⚠️  Render送信エラー: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"⚠️  Render接続エラー: {e}")

# ==================== メイン処理 ====================
def main():
    print("=" * 50)
    print("GOLD RSI AI トレーディング システム v2")
    print("=" * 50)

    # Gemini 初期化
    gemini_model = init_gemini()
    if gemini_model:
        print("✓ Gemini API 初期化完了")
    else:
        print("⚠️  Gemini なしで動作します")

    # MT5 接続
    if not connect_mt5():
        return

    # データ取得
    print(f"\n📊 {SYMBOL} M30 のデータ取得中...")
    df = get_bars(SYMBOL, TIMEFRAME, count=100)
    if df is None:
        mt5.shutdown()
        return

    print(f"✓ {len(df)} 本のバーを取得")

    # RSI シグナル判定
    print("\n🔍 RSI クロスオーバー判定...")
    signal = detect_rsi_crossover(df)
    if signal is None:
        print("❌ RSI 計算エラー")
        mt5.shutdown()
        return

    print(f"\n📈 RSI 値: {signal['rsi']:.2f}")
    print(f"📉 シグナルライン: {signal['signal_line']:.2f}")
    print(f"⏰ 時刻: {signal['time']}")

    if signal['crossover']:
        print(f"\n🎯 クロスオーバー検出！ → {signal['crossover']}")

        # Gemini によるダマシ判定
        print("\n🤖 Gemini でダマシ判定中...")
        ai_result = analyze_signal_with_gemini(gemini_model, df, signal)
        signal['ai_valid'] = ai_result['valid']
        signal['ai_confidence'] = ai_result['confidence']
        signal['ai_reason'] = ai_result['reason']

        status = "✅ 有効シグナル" if ai_result['valid'] else "❌ ダマシ判定"
        print(f"\n{status}")
        print(f"📊 信頼度: {ai_result['confidence']}%")
        print(f"💬 理由: {ai_result['reason']}")
    else:
        print(f"\n⏸️  クロスオーバーなし")
        signal['ai_valid'] = None
        signal['ai_confidence'] = None
        signal['ai_reason'] = None

    # Render サーバーへ送信（スマホに配信）
    print("\n📡 Renderへシグナル送信中...")
    push_to_render(signal)

    # CSV に保存
    df.to_csv('gold_data_log.csv', index=False)
    print("\n✓ ログを gold_data_log.csv に保存")

    mt5.shutdown()
    print("✓ MT5 を閉じました")

    return signal

if __name__ == "__main__":
    print("=" * 50)
    print("GOLD RSI AI トレーディング システム - 自動ループ")
    print(f"シグナル間隔: {LOOP_INTERVAL // 60} 分")
    print("停止するには Ctrl+C を押してください")
    print("=" * 50)
    while True:
        result = main()
        print(f"\n⏰ 次回実行まで {LOOP_INTERVAL // 60} 分待機中...")
        time.sleep(LOOP_INTERVAL)
