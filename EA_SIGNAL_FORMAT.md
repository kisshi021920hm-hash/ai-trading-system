# EA シグナル送信フォーマット

**エンドポイント:** `POST https://ai-trading-system-81jb.onrender.com/ea-signal`

## 必須フィールド

```json
{
  "crossover": "UP_CROSS" | "DOWN_CROSS",
  "latest_close": 2345.67,
  "buy_score": 0-100,
  "sell_score": 0-100,
  "buy_reasons": "EMA交差、RSI<30",
  "sell_reasons": "EMA交差、RSI>70",
  
  // ==================== テクニカル指標（v2で追加） ====================
  
  // 現在足の詳細情報（オプション - あると判定精度UP）
  "current_open": 2344.50,
  "current_high": 2346.20,
  "current_low": 2343.80,
  
  // 過去ローソク足データ（オプション - トレンド判定用）
  "candle_history": [
    {"open": 2340.00, "high": 2342.00, "low": 2339.00, "close": 2341.50},
    {"open": 2341.50, "high": 2343.50, "low": 2340.50, "close": 2343.00},
    {"open": 2343.00, "high": 2345.00, "low": 2342.50, "close": 2344.50},
    // ... 過去5-20本のローソク足を追加
  ],
  
  // EMA（既に実装済み）
  "ema20": 2343.45,
  "ema50": 2340.23,
  "ema_long": 2335.67,
  
  // RSI（既に実装済み）
  "rsi": 65.4,
  
  // MACD（既に実装済み）
  "macd": 0.0234,
  "macd_signal": 0.0198,
  
  // ボリンジャーバンド（既に実装済み）
  "bb_upper": 2348.90,
  "bb_lower": 2339.45,
  
  // ストキャスティクス（既に実装済み）
  "stoch_k": 72.5,
  "stoch_d": 68.3,
  
  // ADX（既に実装済み）
  "adx": 28.5,
  "di_plus": 25.3,
  "di_minus": 12.1,
  
  // ATR（既に実装済み）
  "atr": 3.45
}
```

## Gemini 分析で追加利用される項目（v2新機能）

**テクニカル指標の詳細分析が自動で行われます：**

1. **RSI 分析**
   - `rsi > 70` → ⚠️ オーバーバイ域（反転警戒）
   - `rsi < 30` → ⚠️ オーバーソールド域（反発期待）

2. **ボリンジャーバンド乖離度**
   - 現在価格がバンドの上限/下限に近いか判定
   - ボラティリティ（幅）を確認

3. **MACD トレンド強度**
   - ヒストグラム（MACD - Signal）で勢いの衰退を検知
   - マイナスに向かっていたら反転警戒

4. **ADX トレンド強度**
   - `ADX < 20` → 弱トレンド（信頼度低下）
   - `ADX > 25` → 強トレンド（信頼度維持）

5. **ローソク足ボラティリティ**
   - 高値 - 安値 で現在の値動き幅を判定
   - 極度に高い場合は危険性を考慮

6. **過去ローソク足の方向性**
   - 過去5足でトレンド方向を確認
   - 新シグナル方向と一致しているか判定

## 例：UP_CROSS シグナル（充実版）

```json
{
  "crossover": "UP_CROSS",
  "latest_close": 2345.67,
  "buy_score": 78,
  "sell_score": 22,
  "buy_reasons": "EMA20が下からEMA50を突破、RSI<30から上昇",
  "sell_reasons": "ADX弱い",
  
  "current_open": 2344.50,
  "current_high": 2346.20,
  "current_low": 2343.80,
  
  "candle_history": [
    {"open": 2340.00, "high": 2342.00, "low": 2339.00, "close": 2341.50},
    {"open": 2341.50, "high": 2343.50, "low": 2340.50, "close": 2343.00},
    {"open": 2343.00, "high": 2345.00, "low": 2342.50, "close": 2344.50},
    {"open": 2344.50, "high": 2346.20, "low": 2343.80, "close": 2345.67}
  ],
  
  "ema20": 2344.45,
  "ema50": 2341.23,
  "ema_long": 2336.67,
  "rsi": 35.2,
  "macd": 0.0234,
  "macd_signal": 0.0198,
  "bb_upper": 2348.90,
  "bb_lower": 2339.45,
  "stoch_k": 28.5,
  "stoch_d": 32.3,
  "adx": 22.5,
  "di_plus": 18.3,
  "di_minus": 14.1,
  "atr": 3.45
}
```

## Gemini 分析例

このシグナルは以下のように分析されます：

✅ **ポジティブ:**
- UP_CROSS で買いシグナル
- RSI 35.2（オーバーソールド域から上昇中）→ 反発期待
- 過去4足で上昇トレンド継続
- 買いスコア 78 点

⚠️ **注意点:**
- ADX 22.5（弱いトレンド）→ 信頼度やや低
- MACD はまだ弱い

**推定判定:** 有効性あり、信頼度 65-75%

---

## 対応状況

- ✅ テクニカル指標送信フォーマット定義
- ✅ Gemini 分析ロジック実装
- ⏳ EA 側で `current_high/low/open` と `candle_history` を追加送信

## 次のステップ

**EA MQL5 側で以下を追加してください：**

```mql5
// 現在足の詳細情報を取得
double current_open = iOpen(Symbol(), PERIOD_CURRENT, 0);
double current_high = iHigh(Symbol(), PERIOD_CURRENT, 0);
double current_low = iLow(Symbol(), PERIOD_CURRENT, 0);

// 過去20本のローソク足データを収集
ArraySetAsSeries(rates, true);
CopyRates(Symbol(), Period(), 0, 20, rates);

// JSONで送信
// ... 既存の JSON に以下を追加:
// "current_open": current_open,
// "current_high": current_high,
// "current_low": current_low,
// "candle_history": [{"open": ..., "high": ..., "low": ..., "close": ...}, ...]
```
