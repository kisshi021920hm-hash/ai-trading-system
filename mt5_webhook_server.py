"""
MT5 Webhook サーバー（PC または VPS で起動）
Render（クラウド）から HTTP POST を受け取り、MT5 に自動注文を送信する。

起動方法:
    pip install flask MetaTrader5
    python mt5_webhook_server.py

環境変数（任意）:
    WEBHOOK_SECRET   - Render と同じシークレットキー（デフォルト: goldtrader_webhook_2026）
    MT5_LOGIN        - MT5 ログイン番号
    MT5_PASSWORD     - MT5 パスワード
    MT5_SERVER       - MT5 サーバー名
    PORT             - ポート番号（デフォルト: 5555）
"""

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️  MetaTrader5 ライブラリが見つかりません。pip install MetaTrader5")

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "goldtrader_webhook_2026")
MT5_LOGIN = int(os.environ.get("MT5_LOGIN", "0"))
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("MT5_SERVER", "")
SYMBOL = "XAUUSD"
DEVIATION = 20  # スリッページ（ポイント）

def init_mt5():
    """MT5 への接続を初期化する"""
    if not MT5_AVAILABLE:
        return False
    if not mt5.initialize():
        print(f"❌ MT5 初期化失敗: {mt5.last_error()}")
        return False
    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        if not mt5.login(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
            print(f"❌ MT5 ログイン失敗: {mt5.last_error()}")
            return False
    info = mt5.account_info()
    if info:
        print(f"✅ MT5 接続成功: アカウント {info.login} / 残高 {info.balance}")
    return True

def place_order(direction: str, volume: float, sl: float, tp: float, magic: int = 20260928, comment: str = "GoldAI"):
    """MT5 に成行注文を送信する"""
    if not MT5_AVAILABLE:
        return {"success": False, "error": "MT5 not available"}

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return {"success": False, "error": f"シンボル {SYMBOL} の価格取得失敗"}

    price = tick.ask if direction == "BUY" else tick.bid

    request_obj = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request_obj)
    if result is None:
        return {"success": False, "error": str(mt5.last_error())}

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"✅ MT5注文成功: {direction} {volume}lot @{price} ticket={result.order}")
        return {
            "success": True,
            "order_id": str(result.order),
            "entry_price": price,
            "sl": sl,
            "tp": tp,
            "direction": direction,
            "volume": volume,
        }
    else:
        print(f"❌ MT5注文失敗: retcode={result.retcode} comment={result.comment}")
        return {"success": False, "error": f"retcode={result.retcode}: {result.comment}"}

# ==================== エンドポイント ====================

@app.route("/health", methods=["GET"])
def health():
    mt5_ok = MT5_AVAILABLE and (mt5.account_info() is not None)
    return jsonify({
        "status": "ok",
        "mt5_connected": mt5_ok,
        "time": datetime.utcnow().isoformat()
    })

@app.route("/execute_order", methods=["POST"])
def execute_order():
    # 認証チェック
    secret = request.headers.get("X-Webhook-Secret", "")
    if secret != WEBHOOK_SECRET:
        print(f"⚠️  認証失敗: {request.remote_addr}")
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON body"}), 400

    direction = data.get("direction", "BUY")
    volume = float(data.get("volume", 0.1))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    magic = int(data.get("magic_number", 20260928))
    signal_id = data.get("signal_id", "")

    print(f"📨 注文受信: {direction} vol={volume} sl={sl} tp={tp} signal_id={signal_id}")

    # MT5 が未接続なら再初期化
    if MT5_AVAILABLE and mt5.account_info() is None:
        init_mt5()

    result = place_order(direction, volume, sl, tp, magic, f"GoldAI sig={signal_id}")
    status = 200 if result.get("success") else 502
    return jsonify(result), status

# ==================== 起動 ====================
if __name__ == "__main__":
    print("=" * 50)
    print("🚀 MT5 Webhook サーバー起動中...")
    print(f"   シンボル : {SYMBOL}")
    print(f"   ポート   : {os.environ.get('PORT', 5555)}")
    print("=" * 50)

    if MT5_AVAILABLE:
        init_mt5()
    else:
        print("⚠️  MT5 未接続（テストモードで起動）")

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5555)),
        debug=False
    )
