# CHANGELOG - GOLD AI Trader

**フォーマット**: [Semantic Versioning](https://semver.org/) に準拠  
**規約**: keep-a-changelog に準拠

---

## [Unreleased]

### Planned (v1.24改良版)
- [ ] レンジ回避ロジック: ADX≥25 & |DI+- DI-| > 15 の条件追加
- [ ] ルールベース判定の強化（Gemini なしでもレンジ判定）

### Planned (v1.25)
- [ ] Supabase `status_logs` テーブル作成
- [ ] Flask バックグラウンドジョブ: `status_logging_loop()`
- [ ] React StatusDashboard コンポーネント
- [ ] H1 フィルター試験実装（弱制約）

### Planned (v1.26)
- [ ] ダッシュボード データ分析 & H1 フィルター最適化
- [ ] H1 フィルター本実装 or 削除判定

---

## [1.24] - 2026-09-30

### Added
- **トレーリングストップ機能**: 含み益 $30 以上で自動開始、$20 利益保護
  - BUY ポジション: SL 上方に移動
  - SELL ポジション: SL 下方に移動
  - 現在の SL より有利な場合のみ更新（逆行防止）

### Changed
- **DEFAULT_SL_USD**: 20.0 → 50.0 ドル（XAUUSD の値動きに対応）
- **DEFAULT_TP_USD**: 40.0 → 80.0 ドル（リスク/リワード 1:1.6 維持）

### Reasoning
- 従来の SL=20 は XAUUSD の日中変動（30-50ドル）に対して狭すぎた
- 逆行による早期損切が多発
- トレーリングストップで利益を保護しながら拡大可能に

### Testing
- ダッシュボード稼働後、勝率向上を検証予定（v1.25）

---

## [1.23] - 2026-09-29

### Added
- **mt5_gold_trader.py でのリアルタイムデータプッシュ**: v1.23 では Python でのデータ取得は廃止
- **EA ハートビート機能**: 60秒ごとに `/ea-heartbeat` でスコア・RSI・ADX をサーバーに送信
- **サーバー側デデュープキャッシュ**: `/ea-signal` で 60秒内の同方向シグナルを削除

### Changed
- **信号ソース切り替え**: Yahoo Finance → MT5 EA に統一（リアルタイム・信頼度向上）

### Fixed
- **Gemini WebRequest タイムアウト**: 8000ms → 30000ms に延長（API 応答時間改善対応）
- **重複シグナル問題**: サーバー側デデュープで 60% 削減

### Reasoning
- Yahoo Finance の遅延・ノイズが取引に悪影響
- MT5 EA からのリアルタイムデータが信頼度高い
- サーバー側フィルター（デデュープ）が最後の砦として必須

### Tests
- EA 二重実行 & Yahoo Finance・EA 同時実行での重複シグナル → デデュープで回避確認

---

## [1.22] - 2026-09-28

### Added
- **MT5 リアルタイム指標計算**: EA 側で RSI・MACD・EMA・ボリンジャーバンド・ストキャスティクス・ADX・ATR を計算
- **スコアリングロジック**: 7指標の買いスコア/売りスコアを数値化
- **UI 改善**: App.tsx で複合指標表示

### Changed
- **signal_loop 開始タイミング**: Render デプロイ後、120秒待機 → EA 接続猶予

### Fixed
- **Yahoo Finance・EA 同時実行の競合**: signal_loop が EA 接続前に動く → 古いデータで上書き
- 修正: signal_loop 開始前に 120秒待機、かつ EA 稼働判定（300秒閾値）を追加

---

## [1.13] - 2026-09-30 (Mobile App)

### Added
- **デモ用ルールベースエントリー**: DEMO_RULE_BASED 環境変数でスコア≥4 & ADX≥20 なら信頼度 50%でエントリー
- **クォータエラー専用表示**: AI 判定 = "⚠️ 判定不能"（オレンジ）で区別表示

### Changed
- **AI 判定ロジック**:
  - Gemini クォータ時: `valid=None, confidence=None` を返す（従来の `False/0` から変更）
  - App で条件分岐: `ai_reason.includes("クォータ")` → 専用表示

### Fixed
- **Gemini クォータ中の信頼度表示**: "ダマシ" ではなく "判定不能" として正確に表示

### UI Improvements
- AI 判定表示フロー:
  ```
  ai_reason に "クールダウン" → "⏸️ 保留中"
  ai_reason に "クォータ" → "⚠️ 判定不能"
  ai_valid === null → "⏳ 待機中"
  ai_valid === true → "✅ 有効"
  ai_valid === false → "❌ ダマシ"
  ```

### Version
- `versionCode`: 13 → 14
- `versionName`: "1.12" → "1.13"

---

## [1.12] - 2026-09-27 (Mobile App)

### Added
- **WebSocket リアルタイム配信**: Flask からシグナル受け取り
- **FCM プッシュ通知**: 新シグナル到着時に通知
- **シグナル履歴表示**: 直近 50 件のシグナル一覧
- **取引記録機能**: 手動でエントリー・決済を記録
- **P&L 追跡**: 取引履歴から利益・損失を集計

### UI Components
- Signal Card: クロスオーバー・指標・AI 判定・信頼度表示
- Trade History: 取引履歴テーブル（エントリー価格・決済価格・P&L）
- Analytics Tab: 本日成績（取引数・勝率・総 Pips）

---

## [1.0] - 2026-09-01 (Initial Release)

### Added
- **MT5 EA**: XAUUSD M15 でのクロスオーバー検出
- **Flask サーバー**: Gemini AI シグナル判定
- **Supabase DB**: 全データ永続化
- **Android アプリ**: WebSocket でのシグナル受信・表示

### Initial Features
- UP_CROSS / DOWN_CROSS 自動検出
- Gemini AI による信頼度判定
- FCM プッシュ通知
- ポジション管理（OPEN/CLOSED）

---

## Notes on Versioning

- **Major (X.0)**: 大規模機能追加（例: マルチシンボル対応）
- **Minor (.Y0)**: 機能追加・改善（例: トレーリングストップ）
- **Patch (.YZ)**: バグ修正・小改善

**例:**
- 1.24: v1.2 のパッチ 4（SL/TP 調整 + トレーリングストップ）
- 1.25: v1.2 のパッチ 5（ダッシュボール + H1 フィルター試験）
- 2.0: 将来の大版（マルチシンボル等）

---

## Git Commit References

| Version | Commit | Date | Changes |
|---------|--------|------|---------|
| 1.24 | 88398a9 | 2026-09-30 | SL/TP + トレーリングストップ |
| 1.23 | 2defed4 | 2026-09-30 | デデュープ + クォータ対応 + デモエントリー |
| 1.23 | 3c80ffa | 2026-09-30 | Yahoo Finance 無効化環境変数 |
| 1.23 | 369d251 | 2026-09-29 | signal_loop 120秒待機 + 300秒判定 |
| 1.23 | 2042a8e | 2026-09-29 | EA 稼働中の signal_loop スキップ |
| 1.23 | d76f664 | 2026-09-29 | WebRequest タイムアウト 8→30秒 |

---

## Release Checklist

デプロイ前のチェックリスト:

- [ ] CHANGELOG.md に新機能・修正を記載
- [ ] DECISION_LOG.md に重要な判断を記録
- [ ] ARCHITECTURE.md を最新の状態に更新
- [ ] git commit メッセージに「なぜ」を含める
- [ ] MetaEditor で EA をリコンパイル（バージョン番号確認）
- [ ] app.py でバージョン番号を確認
- [ ] build.gradle で versionCode・versionName を確認
- [ ] npm run build でエラーなし
- [ ] APK ビルド成功
- [ ] GitHub Release に APK をアップロード
- [ ] Render に自動デプロイ（git push）
- [ ] /status エンドポイントで稼働確認

