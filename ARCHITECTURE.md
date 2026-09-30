# GOLD AI Trader - システムアーキテクチャ

**最終更新**: 2026-09-30  
**バージョン**: v1.24（計画中）

---

## システム概要

MT5 EA（XAUUSD M15）+ Render Flask サーバー + Gemini AI + FCM + Android アプリからなる、**自動売買システム**。

3層構成：
1. **MT5 EA**（エージェント層）: 指標計算・シグナル生成
2. **Flask Server**（判断層）: Gemini AI 分析・ルールベース判定・ダッシュボード
3. **Mobile App**（実行層）: 通知・ポジション管理・監視

---

## 各コンポーネント

### 🤖 MT5 EA (`GOLD_AI_Trader.mq5` v1.23→1.24)

**責務**:
- M15 時間足で7つのテクニカル指標を計算（RSI, MACD, EMA, ボリンジャーバンド, ストキャスティクス, ADX, ATR）
- スコアリングロジック（買いスコア/売りスコア）で UP_CROSS / DOWN_CROSS を判定
- `/ea-signal` エンドポイントで Flask サーバーにプッシュ
- サーバーからの最新シグナルを定期ポーリング（15秒間隔）

**重要な設計判断**:
- **M15 単一時間足**: シンプル・レスポンシブ重視。H1/H4 判定は後の phase で試験的追加予定
- **スコアリング式**: 7指標の買い/売りスコアを単純加算。ADX（トレンド強度）と組み合わせることで、レンジ相場での誤シグナル削減
- **SIGNAL_PUSH_INTERVAL = 900秒**: 同方向シグナルの15分再送信。EA の状態維持とサーバー同期の確保
- **SL/TP パラメータ**:
  - v1.23: DEFAULT_SL=20, DEFAULT_TP=40 → XAUUSD の値動き（20ドル SL は狭すぎて逆行が多い）
  - v1.24: DEFAULT_SL=50, DEFAULT_TP=80 → リスク/リワード 1:1.6 維持しつつ、XAUUSDの日中変動に対応

**v1.24 追加機能**:
- **トレーリングストップ**: 含み益 $30 以上で自動開始、$20 利益を保護しながら SL を自動引き上げ
  - 実装理由: 利益確定と損失拡大防止のバランス
  - 検証: ダッシュボード勝率比較で効果測定予定

**ECB（EA Control Block）**:
- g_last_signal_push_time / g_last_pushed_crossover: デデュープ（同じ方向を重複プッシュしない）
- _ea_latest_scores: 最新スコア保存（ハートビート経由でサーバーに送信）

---

### 🖥️ Flask Server (`app.py`)

**責務**:
- EA からのシグナル受け取り (`/ea-signal` エンドポイント)
- **Gemini AI 分析**: 受け取ったシグナル→AI 判定（有効/無効・信頼度・SL/TP 提案）
- **ルールベースフォールバック**: Gemini クォータ中でもルール（スコア≥4 & ADX≥20）でエントリー
- **FCM プッシュ通知**: モバイルアプリへの配信
- **WebSocket 配信**: リアルタイムシグナル更新
- **Supabase 永続化**: 全データ保存

**重要な設計判断**:

1. **Gemini 統合**:
   - 無料プランで /ea-signal ごとに呼び出し
   - クォータ 429 エラー時は `valid=None, confidence=None` を返す（「判定できない」と明示）
   - 返却: `sl_suggestion`, `tp_suggestion`, `reason`, `key_level`

2. **ルールベースフォールバック** (`DEMO_RULE_BASED=true`):
   - Gemini クォータ中も取引を止めない（デモ運用継続）
   - 条件: `(買いスコア≥4 AND 売りスコア<買い) OR (売りスコア≥4 AND 買い<売り) AND ADX≥20`
   - 信頼度: 固定 50%（EA の MIN_CONFIDENCE と合致）
   - 実装理由: デモ環境での継続トレード + データ収集

3. **信号ソースの多重化**:
   - EA_PUSH (MT5 → `/ea-signal`): リアルタイム、信頼度高
   - Yahoo Finance signal_loop: 現在は YAHOO_FINANCE_ENABLED=false で無効（ノイズ源だったため）
   - どちらも無効時は待機

4. **デデュープ戦略**:
   - **EA側**: g_last_pushed_crossover で同じ方向の再送信防止
   - **サーバー側**: `_ea_signal_dedup` キャッシュで 60秒内の重複を削除（EA 二重実行対策）

5. **クールダウン機構** (廃止検討中):
   - 過去のレンジ相場クールダウン（現在は意味を失った）
   - v1.25 で削除予定

**重要な グローバル変数**:
```python
_ea_last_heartbeat          # EA の最後のハートビート時刻
_last_gemini_call_time      # 最後の Gemini 呼び出し時刻
_ea_signal_dedup            # 60秒デデュープキャッシュ {crossover: last_time}
_ea_latest_scores           # 最新スコア（ハートビートで更新）
DEMO_RULE_BASED             # Gemini クォータ中のフォールバック有効化
```

---

### 📱 Mobile App (`App.tsx` v1.13)

**責務**:
- WebSocket でサーバーからシグナルをリアルタイム受信
- シグナル表示（クロスオーバー・RSI・MACD・ADX・AI 判定・信頼度・SL/TP）
- 取引履歴表示・P&L 追跡
- 手動取引記録
- FCM プッシュ通知受信・表示

**UI/UX 設計**:

1. **AI 判定表示**:
   - `ai_valid=true`: ✅ 有効（緑）
   - `ai_valid=false`: ❌ ダマシ（赤）
   - `ai_valid=null`:
     - `ai_reason.includes("クォータ")` → ⚠️ 判定不能（オレンジ、AI 一時不可）
     - else → ⏳ 待機中（グレー、次のシグナル待ち）

2. **SL/TP 表示** (v1.13):
   - `ai_sl_suggestion != null` → 推奨 SL 表示（赤ハイライト）
   - `ai_tp_suggestion != null` → 推奨 TP 表示（緑ハイライト）
   - クォータ中は Gemini が値を返さないため表示されない → v1.24 実装後 H1 フィルター検証時に確認可能

3. **ラベル問題** (未修正):
   - `RSIシグナルSMA: 3.78` は実は MACD シグナル値
   - 原因: Supabase `signals` テーブルの `crossover_mode` カラムが NULL
   - 修正予定: App.tsx で `signal_line` → `MACDシグナル` にラベル変更

---

### 📊 ダッシュボード（計画中、v1.24 後）

**責務** (予定):
- `/status` エンドポイントから 5分ごとに状態を記録（`status_logs` テーブル）
- リアルタイム KPI 表示（EA 稼働率、スコア分布、P&L、ハートビート信頼性）
- チャート表示（スコアトレンド、P&L トレンド、ADX 推移）
- ログテーブル（過去データの検索・分析）

**設計思想**:
- **データドリブン改善**: 実測値を記録→分析→改善のサイクル
- **マルチフレーム検証**: 事後的に M15 データから H1/H4 相当の分析が可能
- **試験的機能検証**: H1 フィルター効果を数値化して判定

---

## データフロー

```
【エントリー】
MT5 EA (M15)
  ↓ /ea-signal (UP_CROSS/DOWN_CROSS + 指標)
Flask Server
  ├→ Gemini AI 分析 → sl_suggestion, tp_suggestion, confidence
  ├→ ルールベースチェック（クォータ中）
  └→ Supabase signals テーブル保存
    ↓
  FCM プッシュ
  WebSocket emit
    ↓
  Mobile App 通知・表示

【決済】
EA が逆クロス検出 OR SL/TP 到達
  ↓
  Supabase trades テーブル update (status: CLOSED_PROFIT/LOSS)
    ↓
  App 履歴更新

【監視】
定期的（5分）に Flask `/status` を記録
  ↓
  Supabase status_logs テーブル保存
    ↓
  Mobile App ダッシュボード表示
```

---

## 外部依存

| 依存 | 用途 | 代替案 |
|------|------|--------|
| **Gemini API** | AI シグナル判定 | ルールベース（フォールバック実装済み） |
| **Supabase** | DB・REST API | 他のノーコード DB（Firebase など） |
| **FCM** | プッシュ通知 | OneSignal など |
| **Render** | Flask ホスティング | Heroku、AWS など |

---

## 制約・トレードオフ

| 項目 | 制約 | 理由 |
|------|------|------|
| **M15 単一時間足** | 小さな時間足のみ | 大きな時間足（H1/H4）での環境判定がない → v1.25 で試験的追加予定 |
| **全ポジション一括決済** | トレーリングストップまで保有 | 部分利確なし → 利益の天井を逃す可能性 |
| **デモ運用** | 実金なし | 実際のスリップ・スプレッド・市場影響は不明 |
| **Gemini 無料枠** | 月間クォータあり | クォータ中はルールベースのみ → クォータ設定で回避可能 |
| **リアルタイムデータ記録** | ログサイズ増加 | 5分ごと × 30日 = 8,640 レコード（許容範囲） |

---

## セキュリティ

- **WebRequest 許可 URL**: `https://ai-trading-system-81jb.onrender.com` を MT5 オプションで許可
- **Supabase API キー**: `.env` に保存、GitHub に push しない
- **FCM 認証キー**: firebase-service-account.json（秘密）

---

## 次のステップ（ロードマップ参照）

- v1.24: SL/TP 調整 + トレーリングストップ
- v1.25: ダッシュボード + H1 フィルター試験
- v1.26: データ検証 + H1 フィルター最適化
- v1.3+: マルチシンボル、複数時間足統合戦略

