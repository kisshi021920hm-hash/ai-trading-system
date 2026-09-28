import MetaTrader5 as mt5
import pandas as pd
import time

# 接続
if not mt5.initialize(login=75611028, password="#1920Hmya1920", server="XMTrading-MT5 3"):
    print(f"❌ MT5 接続失敗: {mt5.last_error()}")
    exit()

time.sleep(2)

# シンボル選択
if not mt5.symbol_select("GOLD", True):
    print(f"❌ シンボル選択失敗: {mt5.last_error()}")
    mt5.shutdown()
    exit()

time.sleep(1)

# データ取得
rates = mt5.copy_rates_from_pos("GOLD", mt5.TIMEFRAME_M30, 0, 10)

# DataFrame に変換
df = pd.DataFrame(rates)

print("=" * 50)
print("データフレームのカラム名:")
print("=" * 50)
print(df.columns.tolist())
print("\n" + "=" * 50)
print("最初の 3 行:")
print("=" * 50)
print(df.head(3))

mt5.shutdown()