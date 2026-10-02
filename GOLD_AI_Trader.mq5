//+------------------------------------------------------------------+
//|  GOLD AI Trader EA  v1.25                                        |
//|  Render API + Gemini AI シグナルによる自動売買                      |
//|  対象: XAUUSD (GOLD) M15                                         |
//|  決済: 逆クロスでドテン（SL/TPでも決済）                             |
//|  v1.25: ハイブリッドSL + 含み損自動決済 + トレーリング実装             |
//+------------------------------------------------------------------+
#property copyright "GOLD AI Trader"
#property version   "1.30"

//--- 入力パラメータ
input string   API_BASE         = "https://ai-trading-system-81jb.onrender.com";
input string   API_URL          = "https://ai-trading-system-81jb.onrender.com/latest-signal";
input int      POLL_SECONDS     = 15;    // APIポーリング間隔（秒）
input int      MIN_CONFIDENCE   = 50;    // 最低AI信頼度 (%) ※デモ用に緩め
input bool     REQUIRE_AI_VALID = false; // AI承認必須 ※デモ用にOFF
input double   RISK_PERCENT     = 2.0;   // 1トレードあたりのリスク率 (%)
input double   DEFAULT_SL_USD   = 50.0;  // デフォルトSL（価格幅ドル）
input double   DEFAULT_TP_USD   = 80.0;  // デフォルトTP（価格幅ドル）
input bool     USE_AI_SL_TP     = true;  // GeminiのSL/TP提案を使用する
input bool     FLIP_ON_REVERSE  = true;  // 逆クロスでドテン
input int      MIN_TRADE_INTERVAL = 900; // 最短取引間隔（秒）= 15分
input int      MAGIC_NUMBER     = 20260929;
input int      SIGNAL_PUSH_INTERVAL = 900; // 同方向シグナルの最小プッシュ間隔（秒）

//--- グローバル変数
int      g_last_signal_id    = -1;
datetime g_last_poll_time    = 0;
datetime g_last_heartbeat    = 0;   // 最後にハートビートを送った時刻
datetime g_last_trade_time   = 0;   // 最後に注文した時刻

//--- 指標ハンドル（v1.22: MT5リアルタイムデータ対応）
int      g_h_rsi    = INVALID_HANDLE;
int      g_h_macd   = INVALID_HANDLE;
int      g_h_ema20  = INVALID_HANDLE;
int      g_h_ema50  = INVALID_HANDLE;
int      g_h_ema200 = INVALID_HANDLE;
int      g_h_bb     = INVALID_HANDLE;
int      g_h_stoch  = INVALID_HANDLE;
int      g_h_adx    = INVALID_HANDLE;
int      g_h_atr    = INVALID_HANDLE;

//--- シグナルプッシュ管理
string   g_last_pushed_crossover = "";  // 最後にサーバーに送ったクロス方向
datetime g_last_signal_push_time = 0;   // 最後に/ea-signalにPOSTした時刻

//--- アプリスライダー連動: サーバーから取得した動的信頼度閾値
int      g_dynamic_min_confidence = -1; // -1=未取得（未取得時はEA入力のMIN_CONFIDENCEを使用）

//--- 最新スコア（ハートビートでサーバーに送るためグローバル保存）
int    g_latest_buy_score  = 0;
int    g_latest_sell_score = 0;
double g_latest_rsi        = 0.0;
double g_latest_adx        = 0.0;
double g_latest_close      = 0.0;
string g_latest_crossover  = "NONE";  // 最新のクロスオーバー方向（NONE/UP_CROSS/DOWN_CROSS）

//--- ブレイクアウト→リテスト追跡（v1.28新機能）
double   g_broken_resistance      = 0.0;  // ブレイクアウトしたレジスタンス水準（0=未ブレイク）
datetime g_broken_resistance_time = 0;    // ブレイクアウト検出時刻（有効期限管理）

//--- ハイブリッドSL設定キャッシュ（固定4変数・上書き更新のみで増えない）
double   g_cached_initial_sl_usd       = 200.0;
double   g_cached_trailing_trigger_usd = 100.0;
double   g_cached_trailing_sl_usd      = 40.0;
datetime g_last_hybrid_sl_fetch        = 0;    // 最後にFlaskから取得した時刻

//+------------------------------------------------------------------+
int OnInit()
{
    Print("=== GOLD AI Trader EA v1.29 起動 ===");
    Print("API: ", API_URL);
    Print("ポーリング: ", POLL_SECONDS, "秒  最低信頼度: ", MIN_CONFIDENCE,
          "%  AI承認必須: ", REQUIRE_AI_VALID);
    Print("逆クロスドテン: ", FLIP_ON_REVERSE, "  リスク率: ", RISK_PERCENT, "%");
    Print("口座残高: $", AccountInfoDouble(ACCOUNT_BALANCE),
          "  通貨: ", AccountInfoString(ACCOUNT_CURRENCY));
    Print("⚠️  ツール→オプション→EA→WebRequest許可URLに追加:");
    Print("   https://ai-trading-system-81jb.onrender.com");

    // 指標ハンドル初期化（M15 リアルタイム）
    g_h_rsi    = iRSI        (_Symbol, PERIOD_M15, 14, PRICE_CLOSE);
    g_h_macd   = iMACD       (_Symbol, PERIOD_M15, 12, 26, 9, PRICE_CLOSE);
    g_h_ema20  = iMA         (_Symbol, PERIOD_M15, 20,  0, MODE_EMA, PRICE_CLOSE);
    g_h_ema50  = iMA         (_Symbol, PERIOD_M15, 50,  0, MODE_EMA, PRICE_CLOSE);
    g_h_ema200 = iMA         (_Symbol, PERIOD_M15, 200, 0, MODE_EMA, PRICE_CLOSE);
    g_h_bb     = iBands      (_Symbol, PERIOD_M15, 20,  0, 2.0, PRICE_CLOSE);
    g_h_stoch  = iStochastic (_Symbol, PERIOD_M15, 14, 3, 3, MODE_SMA, STO_LOWHIGH);
    g_h_adx    = iADX        (_Symbol, PERIOD_M15, 14);
    g_h_atr    = iATR        (_Symbol, PERIOD_M15, 14);

    if (g_h_rsi == INVALID_HANDLE || g_h_macd == INVALID_HANDLE ||
        g_h_adx == INVALID_HANDLE)
        Print("⚠️  指標ハンドル初期化失敗 - /ea-signalプッシュが無効になります");
    else
        Print("✓ 指標ハンドル初期化完了（MT5リアルタイムモード有効）");

    EventSetTimer(POLL_SECONDS);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    EventKillTimer();
    IndicatorRelease(g_h_rsi);
    IndicatorRelease(g_h_macd);
    IndicatorRelease(g_h_ema20);
    IndicatorRelease(g_h_ema50);
    IndicatorRelease(g_h_ema200);
    IndicatorRelease(g_h_bb);
    IndicatorRelease(g_h_stoch);
    IndicatorRelease(g_h_adx);
    IndicatorRelease(g_h_atr);
    Print("GOLD AI Trader EA 停止");
}

void OnTimer()
{
    ComputeAndPushSignal();  // MT5指標計算 → /ea-signalにプッシュ
    PollAndTrade();
    TrailingStopUpdate();    // トレーリングストップ更新
}
// SL/TP/手動決済をサーバーに報告（Supabase整合性維持）
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest&     request,
                        const MqlTradeResult&      result)
{
    // ポジション決済のみ対象（DEAL_ENTRY_OUT = クローズ）
    if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
    if (trans.deal_type != DEAL_TYPE_BUY && trans.deal_type != DEAL_TYPE_SELL) return;

    ulong deal = trans.deal;
    if (!HistoryDealSelect(deal)) return;

    ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(deal, DEAL_ENTRY);
    if (entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_INOUT) return;

    // 決済情報を取得
    double close_price = HistoryDealGetDouble(deal,   DEAL_PRICE);
    double profit      = HistoryDealGetDouble(deal,   DEAL_PROFIT);
    ulong  ticket      = HistoryDealGetInteger(deal,  DEAL_POSITION_ID);
    string symbol      = HistoryDealGetString(deal,   DEAL_SYMBOL);
    // 決済ディールの方向は保有ポジションと逆なので反転する
    // BUYポジション決済 → DEAL_TYPE_SELL / SELLポジション決済 → DEAL_TYPE_BUY
    string dir         = (trans.deal_type == DEAL_TYPE_SELL) ? "BUY" : "SELL";

    // クローズ理由を判定
    ENUM_DEAL_REASON reason = (ENUM_DEAL_REASON)HistoryDealGetInteger(deal, DEAL_REASON);
    string close_reason = "MANUAL";
    if (reason == DEAL_REASON_SL)   close_reason = "SL";
    else if (reason == DEAL_REASON_TP) close_reason = "TP";

    Print("📤 決済報告: ticket=", ticket, " dir=", dir, " price=", close_price,
          " profit=", profit, " reason=", close_reason);

    // /ea-trade に CLOSE を報告
    ReportTradeClose(ticket, dir, close_price, profit, close_reason);
}

void OnTick()
{
    // 含み損監視は最低1秒間隔（毎tick実行で過負荷にならないよう制限）
    static datetime s_last_trailing_tick = 0;
    if (TimeCurrent() > s_last_trailing_tick)
    {
        s_last_trailing_tick = TimeCurrent();
        TrailingStopUpdate();
    }
    if (TimeCurrent() - g_last_poll_time >= POLL_SECONDS)
    {
        ComputeAndPushSignal();
        PollAndTrade();
    }
}

//+------------------------------------------------------------------+
//  MT5リアルタイム指標計算 → /ea-signal プッシュ（v1.24: テクニカル強化版）
//+------------------------------------------------------------------+
void PushSignalToServer(string crossover, double close_price,
                        double open_price, double high_price, double low_price,
                        double rsi, double macd, double macd_sig,
                        double ema20, double ema50, double ema200,
                        double bb_upper, double bb_lower,
                        double stoch_k, double stoch_d,
                        double adx, double di_plus, double di_minus,
                        double atr, int buy_score, int sell_score,
                        string buy_reasons, string sell_reasons,
                        bool support_bounce = false, double support_level_price = 0.0,
                        bool breakout_retest = false, double broken_resistance_price = 0.0)
{
    // 過去20本のローソク足データを取得（テクニカル強化用）
    MqlRates rates[20];
    if (CopyRates(_Symbol, PERIOD_M15, 0, 20, rates) < 20)
    {
        Print("⚠️ CopyRates失敗 - シグナルプッシュをスキップ");
        return;
    }

    string candle_history = "[";
    for (int i = 19; i >= 0; i--)  // 古い足から新しい足へ
    {
        if (i < 19) candle_history += ",";
        candle_history += "{"
            + "\"open\":"  + DoubleToString(rates[i].open,  2)
            + ",\"high\":" + DoubleToString(rates[i].high,  2)
            + ",\"low\":"  + DoubleToString(rates[i].low,   2)
            + ",\"close\":" + DoubleToString(rates[i].close, 2)
            + "}";
    }
    candle_history += "]";

    // buy_reasons/sell_reasons は日本語のためJSON送信から除外（スコアと数値指標で代替）
    string json = "{"
        + "\"crossover\":\""    + crossover                          + "\""
        + ",\"latest_close\":"  + DoubleToString(close_price, 2)
        + ",\"current_open\":"  + DoubleToString(open_price,   2)
        + ",\"current_high\":"  + DoubleToString(high_price,   2)
        + ",\"current_low\":"   + DoubleToString(low_price,    2)
        + ",\"candle_history\":" + candle_history
        + ",\"rsi\":"           + DoubleToString(rsi,          2)
        + ",\"macd\":"          + DoubleToString(macd,         4)
        + ",\"macd_signal\":"   + DoubleToString(macd_sig,     4)
        + ",\"ema20\":"         + DoubleToString(ema20,        2)
        + ",\"ema50\":"         + DoubleToString(ema50,        2)
        + ",\"ema_long\":"      + DoubleToString(ema200,       2)
        + ",\"bb_upper\":"      + DoubleToString(bb_upper,     2)
        + ",\"bb_lower\":"      + DoubleToString(bb_lower,     2)
        + ",\"stoch_k\":"       + DoubleToString(stoch_k,      2)
        + ",\"stoch_d\":"       + DoubleToString(stoch_d,      2)
        + ",\"adx\":"           + DoubleToString(adx,          2)
        + ",\"di_plus\":"       + DoubleToString(di_plus,      2)
        + ",\"di_minus\":"      + DoubleToString(di_minus,     2)
        + ",\"atr\":"           + DoubleToString(atr,          4)
        + ",\"buy_score\":"       + IntegerToString(buy_score)
        + ",\"sell_score\":"      + IntegerToString(sell_score)
        + ",\"support_bounce\":"      + (support_bounce ? "true" : "false")
        + ",\"support_level\":"       + DoubleToString(support_level_price, 2)
        + ",\"breakout_retest\":"     + (breakout_retest ? "true" : "false")
        + ",\"broken_resistance\":"   + DoubleToString(broken_resistance_price, 2)
        + "}";

    string headers = "Content-Type: application/json\r\n";
    char   post[], result[];
    string res_headers;
    StringToCharArray(json, post, 0, StringLen(json));

    int status = WebRequest("POST", API_BASE + "/ea-signal", headers, 30000, post, result, res_headers);
    if (status == 200)
        Print("✅ /ea-signal 送信成功: ", crossover,
              " close=", close_price, " 買い", buy_score, "点 売り", sell_score, "点");
    else
        Print("⚠️  /ea-signal 送信失敗: HTTP ", status, " (EAが稼働中でなければ正常)");
}

// サポートバウンス検出（ポジションなし・クロスなし時の再エントリー）
bool DetectSupportBounce(double cur_price, double cur_rsi, double prev_rsi,
                         double cur_atr, double &support_level)
{
    int bars     = 50;
    int swing_len = 3;
    double proximity_threshold = cur_atr * 0.5;  // ATRの半分以内をサポート近接と判定

    for (int i = swing_len + 1; i < bars - swing_len; i++)
    {
        double low_i = iLow(_Symbol, PERIOD_M15, i);
        if (low_i >= cur_price) continue;  // サポートは現在価格より下のみ

        bool is_swing_low = true;
        for (int j = i - swing_len; j <= i + swing_len; j++)
        {
            if (j == i) continue;
            if (iLow(_Symbol, PERIOD_M15, j) < low_i)
            {
                is_swing_low = false;
                break;
            }
        }
        if (!is_swing_low) continue;

        // サポートへの近接チェック（ATR * 0.5以内）
        if ((cur_price - low_i) <= proximity_threshold)
        {
            // RSI反転確認: 過売られ域から上昇中（底確認）
            if (cur_rsi < 50.0 && cur_rsi > prev_rsi)
            {
                support_level = low_i;
                return true;
            }
        }
    }
    return false;
}

// レジスタンスブレイクアウト検出（前足がレジスタンス以下 → 現足が上抜け）
bool DetectResistanceBreakout(double cur_close, double prev_close, double &broken_level)
{
    int bars = 50, swing_len = 3;
    for (int i = swing_len + 1; i < bars - swing_len; i++)
    {
        double high_i = iHigh(_Symbol, PERIOD_M15, i);
        if (high_i <= prev_close) continue;  // 前足がすでにこのレジスタンスを超えていたらスキップ
        bool is_swing_high = true;
        for (int j = i - swing_len; j <= i + swing_len; j++)
        {
            if (j == i) continue;
            if (iHigh(_Symbol, PERIOD_M15, j) > high_i) { is_swing_high = false; break; }
        }
        if (!is_swing_high) continue;
        // 前足がレジスタンス以下 → 現足がレジスタンスを上抜け
        if (prev_close < high_i && cur_close > high_i)
        {
            broken_level = high_i;
            return true;
        }
    }
    return false;
}

// ブレイクアウト→リテスト検出（旧レジスタンスが新サポートとして機能し反発）
bool DetectBreakoutRetest(double cur_price, double cur_rsi, double prev_rsi, double cur_atr)
{
    if (g_broken_resistance <= 0) return false;
    // ブレイクアウトから48時間以内のみ有効（M15×192本=48h）
    if ((int)(TimeCurrent() - g_broken_resistance_time) > 192 * 900) return false;
    // 旧レジスタンス（新サポート）への接近チェック: ATR×0.5以内かつ価格が上
    double dist = cur_price - g_broken_resistance;
    if (dist < 0 || dist > cur_atr * 0.5) return false;
    // RSI反転確認（反発の底確認）
    return (cur_rsi < 60.0 && cur_rsi > prev_rsi);
}

void ComputeAndPushSignal()
{
    if (g_h_rsi == INVALID_HANDLE || g_h_adx == INVALID_HANDLE) return;

    double rsi_buf[3], macd_main[3], macd_sig[3];
    double ema20_buf[2], ema50_buf[2], ema200_buf[2];
    double bb_upper_buf[2], bb_lower_buf[2];
    double stoch_k_buf[3], stoch_d_buf[3];
    double adx_buf[2], di_plus_buf[2], di_minus_buf[2];
    double atr_buf[2];

    // 指標バッファ取得（失敗したらスキップ）
    if (CopyBuffer(g_h_rsi,    0, 0, 3, rsi_buf)      < 3) return;
    if (CopyBuffer(g_h_macd,   0, 0, 3, macd_main)    < 3) return;
    if (CopyBuffer(g_h_macd,   1, 0, 3, macd_sig)     < 3) return;
    if (CopyBuffer(g_h_ema20,  0, 0, 2, ema20_buf)    < 2) return;
    if (CopyBuffer(g_h_ema50,  0, 0, 2, ema50_buf)    < 2) return;
    if (CopyBuffer(g_h_ema200, 0, 0, 2, ema200_buf)   < 2) return;
    if (CopyBuffer(g_h_bb,     1, 0, 2, bb_upper_buf) < 2) return;  // buffer1=UPPER_BAND
    if (CopyBuffer(g_h_bb,     2, 0, 2, bb_lower_buf) < 2) return;  // buffer2=LOWER_BAND
    if (CopyBuffer(g_h_stoch,  0, 0, 3, stoch_k_buf)  < 3) return;
    if (CopyBuffer(g_h_stoch,  1, 0, 3, stoch_d_buf)  < 3) return;
    if (CopyBuffer(g_h_adx,    0, 0, 2, adx_buf)      < 2) return;
    if (CopyBuffer(g_h_adx,    1, 0, 2, di_plus_buf)  < 2) return;
    if (CopyBuffer(g_h_adx,    2, 0, 2, di_minus_buf) < 2) return;
    if (CopyBuffer(g_h_atr,    0, 0, 2, atr_buf)      < 2) return;

    // 現在値（index 0 = 最新足）
    double cur_close    = iClose(_Symbol, PERIOD_M15, 0);
    double cur_rsi      = rsi_buf[0];
    double cur_macd     = macd_main[0],  cur_macd_sig = macd_sig[0];
    double prv_macd     = macd_main[1],  prv_macd_sig = macd_sig[1];
    double cur_ema20    = ema20_buf[0],  cur_ema50    = ema50_buf[0];
    double cur_ema200   = ema200_buf[0];
    double cur_bb_upper = bb_upper_buf[0], cur_bb_lower = bb_lower_buf[0];
    double cur_stoch_k  = stoch_k_buf[0], cur_stoch_d  = stoch_d_buf[0];
    double prv_stoch_k  = stoch_k_buf[1], prv_stoch_d  = stoch_d_buf[1];
    double cur_adx      = adx_buf[0];
    double cur_di_plus  = di_plus_buf[0], cur_di_minus = di_minus_buf[0];
    double cur_atr      = atr_buf[0];

    // --- 7指標スコアリング（サーバーの compute_signal_composite と同一ロジック）---
    int    buy_score = 0, sell_score = 0;
    string buy_reasons = "", sell_reasons = "";

    // 1. EMAトレンド
    if (cur_ema20 > cur_ema50)    { buy_score++;  buy_reasons  += "短期EMA↑,"; }
    else                          { sell_score++; sell_reasons += "短期EMA↓,"; }
    if (cur_close > cur_ema200)   { buy_score++;  buy_reasons  += "長期EMA上方,"; }
    else                          { sell_score++; sell_reasons += "長期EMA下方,"; }

    // 2. MACD
    if      (prv_macd < prv_macd_sig && cur_macd > cur_macd_sig)
        { buy_score  += 2; buy_reasons  += "MACDゴールデンクロス,"; }
    else if (prv_macd > prv_macd_sig && cur_macd < cur_macd_sig)
        { sell_score += 2; sell_reasons += "MACDデッドクロス,"; }
    else if (cur_macd > cur_macd_sig)
        { buy_score++;  buy_reasons  += "MACD買い優勢,"; }
    else
        { sell_score++; sell_reasons += "MACD売り優勢,"; }

    // 3. RSI
    if      (cur_rsi < 30)
        { buy_score  += 2; buy_reasons  += StringFormat("RSI売られすぎ(%.0f),", cur_rsi); }
    else if (cur_rsi >= 40 && cur_rsi <= 65)
        { buy_score++;  buy_reasons  += StringFormat("RSI買い圏(%.0f),", cur_rsi); }
    if      (cur_rsi > 70)
        { sell_score += 2; sell_reasons += StringFormat("RSI買われすぎ(%.0f),", cur_rsi); }
    else if (cur_rsi >= 35 && cur_rsi < 60)
        { sell_score++; sell_reasons += StringFormat("RSI売り圏(%.0f),", cur_rsi); }

    // 4. Stochastic
    if      (prv_stoch_k < prv_stoch_d && cur_stoch_k > cur_stoch_d)
        { buy_score  += 2; buy_reasons  += StringFormat("ストキャスGC(%.0f),", cur_stoch_k); }
    else if (prv_stoch_k > prv_stoch_d && cur_stoch_k < cur_stoch_d)
        { sell_score += 2; sell_reasons += StringFormat("ストキャスDC(%.0f),", cur_stoch_k); }
    else if (cur_stoch_k > cur_stoch_d)
        { buy_score++;  buy_reasons  += "ストキャス買い優勢,"; }
    else
        { sell_score++; sell_reasons += "ストキャス売り優勢,"; }

    // 5. ボリンジャーバンド
    double bb_range = cur_bb_upper - cur_bb_lower;
    if (bb_range > 0)
    {
        double bb_pos = (cur_close - cur_bb_lower) / bb_range;
        if      (bb_pos < 0.25) { buy_score++;  buy_reasons  += "BB下限付近,"; }
        else if (bb_pos > 0.75) { sell_score++; sell_reasons += "BB上限付近,"; }
    }

    // 6. ADX方向
    if      (cur_di_plus > cur_di_minus && cur_adx > 20)
        { buy_score++;  buy_reasons  += StringFormat("DI+優勢(ADX%.0f),", cur_adx); }
    else if (cur_di_minus > cur_di_plus && cur_adx > 20)
        { sell_score++; sell_reasons += StringFormat("DI-優勢(ADX%.0f),", cur_adx); }

    // --- クロスオーバー判定 ---
    int    THRESHOLD = 3;
    string crossover = "";
    if      (buy_score  >= THRESHOLD && buy_score  > sell_score + 1) crossover = "UP_CROSS";
    else if (sell_score >= THRESHOLD && sell_score > buy_score  + 1) crossover = "DOWN_CROSS";

    // 最新スコアを常に保存（ハートビート経由でサーバーに送るため）
    g_latest_buy_score  = buy_score;
    g_latest_sell_score = sell_score;
    g_latest_rsi        = cur_rsi;
    g_latest_adx        = cur_adx;
    g_latest_close      = cur_close;
    g_latest_crossover  = (crossover != "") ? crossover : "NONE";

    // MT5 エキスパートログに1分ごとに表示
    static datetime s_last_log_time = 0;
    if (TimeCurrent() - s_last_log_time >= 60)
    {
        s_last_log_time = TimeCurrent();
        Print("📊 スコア: 買い", buy_score, "点 vs 売り", sell_score, "点",
              " | RSI=", DoubleToString(cur_rsi, 1),
              " ADX=", DoubleToString(cur_adx, 1),
              " close=", DoubleToString(cur_close, 2),
              " | ", (crossover != "" ? crossover : "クロスなし"));
    }

    // 現在足の OHLC を取得
    double cur_open  = iOpen (_Symbol, PERIOD_M15, 0);
    double cur_high  = iHigh (_Symbol, PERIOD_M15, 0);
    double cur_low   = iLow  (_Symbol, PERIOD_M15, 0);

    if (crossover == "")
    {
        // 通常クロスなし → ポジションなし時のみ再エントリー検出
        int total_open = CountPositions(POSITION_TYPE_BUY) + CountPositions(POSITION_TYPE_SELL);
        if (total_open == 0)
        {
            double prev_rsi_val = rsi_buf[1];

            // ① ブレイクアウト→リテスト検出（優先: 最も強いシグナル）
            if (g_broken_resistance > 0 && DetectBreakoutRetest(cur_close, cur_rsi, prev_rsi_val, cur_atr))
            {
                static datetime s_last_retest_time = 0;
                if (TimeCurrent() - s_last_retest_time >= SIGNAL_PUSH_INTERVAL)
                {
                    s_last_retest_time    = TimeCurrent();
                    double saved_res      = g_broken_resistance;
                    g_broken_resistance   = 0;  // 1回のみトリガー
                    Print("⭐ ブレイクアウト→リテスト検出! 旧レジスタンス=", DoubleToString(saved_res, 2),
                          " 価格=", DoubleToString(cur_close, 2));
                    PushSignalToServer("UP_CROSS", cur_close, cur_open, cur_high, cur_low,
                                       cur_rsi, cur_macd, cur_macd_sig,
                                       cur_ema20, cur_ema50, cur_ema200,
                                       cur_bb_upper, cur_bb_lower,
                                       cur_stoch_k, cur_stoch_d,
                                       cur_adx, cur_di_plus, cur_di_minus, cur_atr,
                                       buy_score, sell_score, buy_reasons, sell_reasons,
                                       false, 0.0, true, saved_res);
                    g_last_pushed_crossover = "BREAKOUT_RETEST";
                    g_last_signal_push_time = TimeCurrent();
                }
                return;
            }

            // ② サポートバウンス検出（通常クロスなしの底反発）
            double support_lvl = 0;
            if (DetectSupportBounce(cur_close, cur_rsi, prev_rsi_val, cur_atr, support_lvl))
            {
                static double   s_last_bounce_support = 0;
                static datetime s_last_bounce_time    = 0;
                bool same_level = (MathAbs(support_lvl - s_last_bounce_support) < 1.0);
                bool too_soon   = (TimeCurrent() - s_last_bounce_time < SIGNAL_PUSH_INTERVAL);
                if (!same_level || !too_soon)
                {
                    s_last_bounce_support = support_lvl;
                    s_last_bounce_time    = TimeCurrent();
                    Print("🌀 サポートバウンス検出: 価格=", DoubleToString(cur_close, 2),
                          " サポート=", DoubleToString(support_lvl, 2),
                          " RSI=", DoubleToString(cur_rsi, 1));
                    PushSignalToServer("UP_CROSS", cur_close, cur_open, cur_high, cur_low,
                                       cur_rsi, cur_macd, cur_macd_sig,
                                       cur_ema20, cur_ema50, cur_ema200,
                                       cur_bb_upper, cur_bb_lower,
                                       cur_stoch_k, cur_stoch_d,
                                       cur_adx, cur_di_plus, cur_di_minus, cur_atr,
                                       buy_score, sell_score, buy_reasons, sell_reasons,
                                       true, support_lvl);
                    g_last_pushed_crossover = "SUPPORT_BOUNCE";
                    g_last_signal_push_time = TimeCurrent();
                }
            }
        }
        return;
    }

    // 同方向かつプッシュ間隔未満 → スキップ
    if (crossover == g_last_pushed_crossover &&
        TimeCurrent() - g_last_signal_push_time < SIGNAL_PUSH_INTERVAL) return;

    // サーバーにプッシュ
    PushSignalToServer(crossover, cur_close, cur_open, cur_high, cur_low,
                       cur_rsi, cur_macd, cur_macd_sig,
                       cur_ema20, cur_ema50, cur_ema200,
                       cur_bb_upper, cur_bb_lower,
                       cur_stoch_k, cur_stoch_d,
                       cur_adx, cur_di_plus, cur_di_minus, cur_atr,
                       buy_score, sell_score, buy_reasons, sell_reasons);

    g_last_pushed_crossover = crossover;
    g_last_signal_push_time = TimeCurrent();

    // UP_CROSSでレジスタンスブレイクアウトを記録（次のリテストを待機）
    if (crossover == "UP_CROSS")
    {
        double prev_close_val = iClose(_Symbol, PERIOD_M15, 1);
        double broken_lvl     = 0;
        if (DetectResistanceBreakout(cur_close, prev_close_val, broken_lvl))
        {
            g_broken_resistance      = broken_lvl;
            g_broken_resistance_time = TimeCurrent();
            Print("🔴 レジスタンスブレイクアウト記録: 水準=", DoubleToString(broken_lvl, 2),
                  " → リテスト待機中");
        }
    }
}

//+------------------------------------------------------------------+
void SendHeartbeat()
{
    if (TimeCurrent() - g_last_heartbeat < 60) return;
    g_last_heartbeat = TimeCurrent();

    // 最新スコアをハートビートに乗せてサーバーへ送る（v1.23）
    string hb_json = "{"
        + "\"buy_score\":"    + IntegerToString(g_latest_buy_score)
        + ",\"sell_score\":"  + IntegerToString(g_latest_sell_score)
        + ",\"rsi\":"         + DoubleToString(g_latest_rsi,   1)
        + ",\"adx\":"         + DoubleToString(g_latest_adx,   1)
        + ",\"close\":"       + DoubleToString(g_latest_close, 2)
        + ",\"crossover\":\"" + g_latest_crossover + "\""
        + "}";

    string hb_headers = "Content-Type: application/json\r\n";
    char   hb_post[], hb_result[];
    string hb_resp_headers;
    StringToCharArray(hb_json, hb_post, 0, StringLen(hb_json));
    int hb_status = WebRequest("POST", API_BASE + "/ea-heartbeat", hb_headers, 3000, hb_post, hb_result, hb_resp_headers);
    if (hb_status != 200)
        Print("⚠️ ハートビート送信失敗: HTTP", hb_status);
}

void ReportTrade(string action, string direction, double price,
                 double sl, double tp, double lot, ulong ticket)
{
    string json = "{\"action\":\"" + action + "\""
                + ",\"direction\":\"" + direction + "\""
                + ",\"price\":"    + DoubleToString(price, 2)
                + ",\"sl\":"       + DoubleToString(sl, 2)
                + ",\"tp\":"       + DoubleToString(tp, 2)
                + ",\"lot\":"      + DoubleToString(lot, 2)
                + ",\"ticket\":"   + IntegerToString((long)ticket)
                + ",\"balance\":"  + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
                + "}";
    string rep_headers = "Content-Type: application/json\r\n";
    char   rep_post[], rep_result[];
    string rep_resp_headers;
    StringToCharArray(json, rep_post, 0, StringLen(json));
    int rep_status = WebRequest("POST", API_BASE + "/ea-trade", rep_headers, 3000, rep_post, rep_result, rep_resp_headers);
    if (rep_status != 200)
        Print("⚠️ 取引レポート送信失敗: HTTP", rep_status, " action=", action, " direction=", direction);
}

// SL/TP/手動決済をサーバーに報告
void ReportTradeClose(ulong ticket, string direction, double close_price,
                      double profit, string close_reason)
{
    string json = "{\"action\":\"CLOSE\""
                + ",\"ticket\":"       + IntegerToString((long)ticket)
                + ",\"direction\":\"" + direction + "\""
                + ",\"close_price\":" + DoubleToString(close_price, 2)
                + ",\"profit\":"      + DoubleToString(profit, 2)
                + ",\"close_reason\":\"" + close_reason + "\""
                + ",\"balance\":"     + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
                + "}";
    string headers = "Content-Type: application/json\r\n";
    char   post[], res[];
    string resp_headers;
    StringToCharArray(json, post, 0, StringLen(json));
    int status = WebRequest("POST", API_BASE + "/ea-trade", headers, 3000, post, res, resp_headers);
    if (status != 200)
        Print("⚠️ 決済レポート送信失敗: HTTP", status, " ticket=", ticket, " reason=", close_reason);
    else
        Print("✅ 決済報告完了: ticket=", ticket, " reason=", close_reason, " profit=", profit);
}

//+------------------------------------------------------------------+
void PollAndTrade()
{
    g_last_poll_time = TimeCurrent();
    SendHeartbeat();

    string headers = "Content-Type: application/json\r\n";
    char   post[], result[];
    string res_headers;

    int status = WebRequest("GET", API_URL, headers, 5000, post, result, res_headers);
    if (status != 200)
    {
        if (status == -1)
            Print("❌ WebRequest失敗: WebRequest許可URLリストにAPIを追加してください");
        else
            Print("⚠️  HTTP ", status);
        return;
    }

    string json = CharArrayToString(result);

    int    sig_id     = JsonInt(json,    "\"id\":");
    string crossover  = JsonString(json, "\"crossover\":");
    bool   ai_valid   = JsonBool(json,   "\"ai_valid\":");
    int    confidence = JsonInt(json,    "\"ai_confidence\":");
    double sl_price   = JsonDouble(json, "\"ai_sl_suggestion\":");
    double tp_price   = JsonDouble(json, "\"ai_tp_suggestion\":");
    bool   force_close = JsonBool(json,  "\"force_close\":");

    // アプリスライダーの信頼度閾値を取得（サーバーから配信）
    int server_min_conf = JsonInt(json, "\"min_confidence\":");
    if (server_min_conf > 0)
    {
        if (g_dynamic_min_confidence != server_min_conf)
            Print("🎯 信頼度閾値をアプリ設定に同期: ", g_dynamic_min_confidence, "% → ", server_min_conf, "%");
        g_dynamic_min_confidence = server_min_conf;
    }
    int effective_min_confidence = (g_dynamic_min_confidence > 0) ? g_dynamic_min_confidence : MIN_CONFIDENCE;

    //--- AI_CLOSE_MODE: Geminiからの決済指示（force_close）チェック（signal_idに依存しない）
    if (force_close)
    {
        int open_buy  = CountPositions(POSITION_TYPE_BUY);
        int open_sell = CountPositions(POSITION_TYPE_SELL);
        if (open_buy > 0 || open_sell > 0)
        {
            Print("🔴 Gemini決済指示受信 → 全ポジション決済");
            if (open_buy  > 0) ClosePositions(POSITION_TYPE_BUY);
            if (open_sell > 0) ClosePositions(POSITION_TYPE_SELL);
        }
        return;  // force_close時はエントリーロジックをスキップ
    }

    if (sig_id <= 0 || sig_id == g_last_signal_id) return;
    g_last_signal_id = sig_id;

    if (crossover == "" || crossover == "null") return;

    Print("📡 #", sig_id, " ", crossover,
          "  AI=", (ai_valid ? "✅" : "❌"),
          "  信頼度=", confidence, "%");

    //--- AIフィルター（デモ用に緩め）
    if (REQUIRE_AI_VALID && !ai_valid)
    {
        Print("⛔ AI非承認スキップ");
        return;
    }
    // Geminiが実際に動いた場合(confidence>=0)のみ信頼度チェック
    // クールダウン中(confidence=-1)はREQUIRE_AI_VALIDに従う
    // effective_min_confidence = アプリスライダー値（未取得時はEA入力パラメータ）
    if (confidence >= 0 && confidence < effective_min_confidence)
    {
        Print("⛔ 信頼度不足: ", confidence, "% < ", effective_min_confidence, "%（アプリ設定値）");
        return;
    }

    //--- 既存ポジション確認
    int open_buy  = CountPositions(POSITION_TYPE_BUY);
    int open_sell = CountPositions(POSITION_TYPE_SELL);

    if (crossover == "UP_CROSS")
    {
        if (open_sell > 0 && FLIP_ON_REVERSE)
        {
            Print("🔄 SELLをドテン → BUYへ");
            ClosePositions(POSITION_TYPE_SELL);
        }
        if (open_buy == 0)
            ExecuteOrder(ORDER_TYPE_BUY, sl_price, tp_price);
        else
            Print("ℹ️  BUYポジション既存のためスキップ");
    }
    else if (crossover == "DOWN_CROSS")
    {
        if (open_buy > 0 && FLIP_ON_REVERSE)
        {
            Print("🔄 BUYをドテン → SELLへ");
            ClosePositions(POSITION_TYPE_BUY);
        }
        if (open_sell == 0)
            ExecuteOrder(ORDER_TYPE_SELL, sl_price, tp_price);
        else
            Print("ℹ️  SELLポジション既存のためスキップ");
    }
}

//+------------------------------------------------------------------+
int CountPositions(ENUM_POSITION_TYPE pos_type)
{
    int count = 0;
    for (int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if (ticket == 0) continue;
        if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != MAGIC_NUMBER) continue;
        if ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == pos_type)
            count++;
    }
    return count;
}

void ClosePositions(ENUM_POSITION_TYPE pos_type)
{
    for (int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if (ticket == 0) continue;
        if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != MAGIC_NUMBER) continue;
        if ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) != pos_type) continue;

        MqlTradeRequest req = {};
        MqlTradeResult  res = {};
        req.action       = TRADE_ACTION_DEAL;
        req.symbol       = _Symbol;
        req.volume       = PositionGetDouble(POSITION_VOLUME);
        req.type         = (pos_type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
        req.price        = (pos_type == POSITION_TYPE_BUY)
                           ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                           : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        req.position     = ticket;
        req.magic        = MAGIC_NUMBER;
        req.comment      = "GOLD_AI_CLOSE";
        req.type_filling = ORDER_FILLING_IOC;

        if (OrderSend(req, res))
        {
            Print("✅ 決済成功: ticket=", ticket);
            string close_dir = (pos_type == POSITION_TYPE_BUY) ? "BUY_CLOSE" : "SELL_CLOSE";
            ReportTrade("CLOSE", close_dir, req.price, 0, 0,
                        req.volume, ticket);  // 決済前に取得済みのreq.volumeを使用
        }
        else
            Print("❌ 決済失敗: retcode=", res.retcode, " comment=", res.comment);
    }
}

//+------------------------------------------------------------------+
//  ハイブリッドSL設定をFlaskから取得（60秒キャッシュ・固定4変数で増えない）
//+------------------------------------------------------------------+
void FetchHybridSlConfig()
{
    if (TimeCurrent() - g_last_hybrid_sl_fetch < 60) return;  // 60秒キャッシュ有効中はスキップ

    string headers = "Content-Type: application/json\r\n";
    char   post[], result[];
    string res_headers;

    int status = WebRequest("GET", API_BASE + "/api/settings/hybrid-sl",
                            headers, 3000, post, result, res_headers);
    if (status != 200)
    {
        // 取得失敗時はキャッシュ済みの値をそのまま使い続ける（デフォルト値で安全動作）
        Print("⚠️  ハイブリッドSL設定取得失敗(HTTP", status, ") → キャッシュ値で継続");
        g_last_hybrid_sl_fetch = TimeCurrent();  // 再試行は60秒後（連続失敗を防ぐ）
        return;
    }

    string json = CharArrayToString(result);
    double new_initial_sl       = JsonDouble(json, "\"initial_sl_usd\":");
    double new_trailing_trigger = JsonDouble(json, "\"trailing_trigger_usd\":");
    double new_trailing_sl      = JsonDouble(json, "\"trailing_sl_usd\":");

    // 値が有効な場合のみ上書き（0や負の値は無視してキャッシュを保持）
    if (new_initial_sl > 0)       g_cached_initial_sl_usd       = new_initial_sl;
    if (new_trailing_trigger > 0) g_cached_trailing_trigger_usd = new_trailing_trigger;
    if (new_trailing_sl > 0)      g_cached_trailing_sl_usd      = new_trailing_sl;

    g_last_hybrid_sl_fetch = TimeCurrent();  // 取得時刻を更新（次回は60秒後）

    Print("🔧 ハイブリッドSL設定更新: 初期SL=$", g_cached_initial_sl_usd,
          " トレーリング開始=$", g_cached_trailing_trigger_usd,
          " トレーリングSL=$", g_cached_trailing_sl_usd);
}

//+------------------------------------------------------------------+
//  ハイブリッドSL + トレーリングストップ（v1.25: 含み損自動決済 + トレーリング実装）
//+------------------------------------------------------------------+
void TrailingStopUpdate()
{
    FetchHybridSlConfig();  // 60秒ごとにFlaskから設定を取得（キャッシュ制御済み）

    double initial_sl_usd       = g_cached_initial_sl_usd;
    double trailing_trigger_usd = g_cached_trailing_trigger_usd;
    double trailing_sl_usd      = g_cached_trailing_sl_usd;

    for (int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if (ticket == 0) continue;
        if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != MAGIC_NUMBER) continue;

        ENUM_POSITION_TYPE pos_type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        double entry_price = PositionGetDouble(POSITION_PRICE_OPEN);
        double current_sl   = PositionGetDouble(POSITION_SL);
        double current_tp   = PositionGetDouble(POSITION_TP);
        double bid          = SymbolInfoDouble(_Symbol, SYMBOL_BID);
        double ask          = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        double current_price = (pos_type == POSITION_TYPE_BUY) ? bid : ask;
        double volume       = PositionGetDouble(POSITION_VOLUME);

        // 含み益/含み損計算（USD）
        double unrealized_pips = 0;
        if (pos_type == POSITION_TYPE_BUY)
            unrealized_pips = (current_price - entry_price);
        else
            unrealized_pips = (entry_price - current_price);

        double contract_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
        double usd_per_pip    = volume * contract_size;  // ブローカー依存のコントラクトサイズ
        double unrealized_usd = unrealized_pips * usd_per_pip;

        Print("📊 ポジション監視: ", _Symbol, " ", (pos_type == POSITION_TYPE_BUY ? "BUY" : "SELL"),
              " volume=", volume, " entry=", entry_price, " current=", current_price,
              " P&L=", DoubleToString(unrealized_usd, 2), "$ (", DoubleToString(unrealized_pips, 2), " pips)");

        // ======== 1. 含み損が初期SL超過 → 強制決済 ========
        if (unrealized_usd < -initial_sl_usd)
        {
            Print("🚨 含み損限界超過: ", DoubleToString(unrealized_usd, 2), "$ < -", initial_sl_usd,
                  "$ → 強制決済実行");
            ClosePosition(ticket, pos_type);
            continue;
        }

        // ======== 2. トレーリング条件チェック: 含み益 >= $2 ========
        if (unrealized_usd >= trailing_trigger_usd)
        {
            // トレーリング中: SLを引き上げ（ドル→価格幅に正しく変換）
            double trailing_sl_points = (usd_per_pip > 0) ? (trailing_sl_usd / usd_per_pip) : trailing_sl_usd;
            // ブローカーの最低ストップ距離を確保
            double min_stop_points = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL)
                                     * SymbolInfoDouble(_Symbol, SYMBOL_POINT);
            trailing_sl_points = MathMax(trailing_sl_points, min_stop_points + SymbolInfoDouble(_Symbol, SYMBOL_POINT));

            double new_sl = 0;
            if (pos_type == POSITION_TYPE_BUY)
            {
                new_sl = current_price - trailing_sl_points;
            }
            else
            {
                new_sl = current_price + trailing_sl_points;
            }

            int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
            new_sl = NormalizeDouble(new_sl, digits);

            // 新しいSLが現在のSLより有利な場合のみ更新
            bool should_update = false;
            if (pos_type == POSITION_TYPE_BUY  && new_sl > current_sl) should_update = true;
            if (pos_type == POSITION_TYPE_SELL && new_sl < current_sl) should_update = true;

            if (should_update)
            {
                MqlTradeRequest req = {};
                MqlTradeResult  res = {};
                req.action   = TRADE_ACTION_SLTP;
                req.symbol   = _Symbol;
                req.position = ticket;
                req.sl       = new_sl;
                req.tp       = current_tp;
                req.magic    = MAGIC_NUMBER;

                if (OrderSend(req, res))
                {
                    Print("✅ トレーリングストップ更新: ticket=", ticket,
                          " 含み益=$", DoubleToString(unrealized_usd, 2),
                          " 旧SL=", DoubleToString(current_sl, 2),
                          " 新SL=", DoubleToString(new_sl, 2),
                          " (スプレッド$", DoubleToString(trailing_sl_usd, 2), "保護)");
                    current_sl = new_sl;  // ライン描画に最新SLを反映
                }
                else
                {
                    Print("⚠️  トレーリングストップ更新失敗: retcode=", res.retcode);
                }
            }
        }

        // チャートにSL/トレーリングラインを描画
        DrawPositionLines(pos_type, entry_price, current_sl,
                          current_price, unrealized_usd,
                          trailing_trigger_usd, trailing_sl_usd, usd_per_pip);
    }

    // ポジションがない場合はラインを削除
    if (PositionsTotal() == 0)
        DeletePositionLines();
}

//+------------------------------------------------------------------+
//  チャートライン描画ユーティリティ（v1.29: SL/トレーリング可視化）
//+------------------------------------------------------------------+
void DrawHLine(string name, double price, color clr, ENUM_LINE_STYLE style, int width, string label)
{
    if (ObjectFind(0, name) < 0)
        ObjectCreate(0, name, OBJ_HLINE, 0, 0, price);
    ObjectSetDouble(0, name, OBJPROP_PRICE, price);
    ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
    ObjectSetInteger(0, name, OBJPROP_STYLE, style);
    ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
    ObjectSetString(0, name, OBJPROP_TOOLTIP, label);
    ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
    ObjectSetInteger(0, name, OBJPROP_BACK, true);
    ChartRedraw(0);
}

void DeletePositionLines()
{
    string names[] = {"EA_Entry", "EA_SL", "EA_TrailStart", "EA_TrailSL"};
    for (int i = 0; i < ArraySize(names); i++)
        ObjectDelete(0, names[i]);
    ChartRedraw(0);
}

void DrawPositionLines(ENUM_POSITION_TYPE pos_type,
                       double entry_price, double current_sl,
                       double current_price, double unrealized_usd,
                       double trailing_trigger_usd, double trailing_sl_usd, double usd_per_pip)
{
    int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

    // エントリーライン（水色 実線）
    DrawHLine("EA_Entry", entry_price, clrDodgerBlue, STYLE_SOLID, 1,
              "エントリー: " + DoubleToString(entry_price, digits));

    // 現在のSLライン（赤 太実線）
    if (current_sl > 0)
        DrawHLine("EA_SL", current_sl, clrRed, STYLE_SOLID, 2,
                  "SL: " + DoubleToString(current_sl, digits));

    // トレーリング開始レベル（オレンジ 点線）- 含み益がこの水準に達するとトレーリング開始
    if (usd_per_pip > 0)
    {
        double trigger_points = trailing_trigger_usd / usd_per_pip;
        double trail_start_price = (pos_type == POSITION_TYPE_BUY)
                                   ? entry_price + trigger_points
                                   : entry_price - trigger_points;
        trail_start_price = NormalizeDouble(trail_start_price, digits);
        DrawHLine("EA_TrailStart", trail_start_price, clrOrange, STYLE_DOT, 1,
                  "トレーリング開始 (+" + DoubleToString(trailing_trigger_usd, 0) + "$利益で発動)");
    }

    // 現在のトレーリングSLライン（オレンジレッド 破線）- トレーリング中のみ表示
    if (unrealized_usd >= trailing_trigger_usd && usd_per_pip > 0)
    {
        double trail_sl_points = trailing_sl_usd / usd_per_pip;
        double trail_sl_price  = (pos_type == POSITION_TYPE_BUY)
                                 ? current_price - trail_sl_points
                                 : current_price + trail_sl_points;
        trail_sl_price = NormalizeDouble(trail_sl_price, digits);
        DrawHLine("EA_TrailSL", trail_sl_price, clrOrangeRed, STYLE_DASH, 2,
                  "トレーリングSL (" + DoubleToString(trailing_sl_usd, 0) + "$幅 | 現在: " + DoubleToString(trail_sl_price, digits) + ")");
    }
    else
        ObjectDelete(0, "EA_TrailSL");  // トレーリング未発動中は非表示
}

// ポジション強制決済（含み損時の損切り）
void ClosePosition(ulong ticket, ENUM_POSITION_TYPE pos_type)
{
    if (!PositionSelectByTicket(ticket))
    {
        Print("❌ 含み損決済: ticket=", ticket, " のポジション選択失敗");
        return;
    }
    double volume = PositionGetDouble(POSITION_VOLUME);

    double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double close_price = (pos_type == POSITION_TYPE_BUY) ? bid : ask;

    MqlTradeRequest req = {};
    MqlTradeResult  res = {};
    req.action       = TRADE_ACTION_DEAL;
    req.symbol       = _Symbol;
    req.volume       = volume;  // ★修正: 決済ロット数を設定
    req.price        = close_price;
    req.sl           = 0;
    req.tp           = 0;
    req.type         = (pos_type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
    req.position     = ticket;
    req.comment      = "GOLD_AI_LOSS_CUT";
    req.type_filling = ORDER_FILLING_IOC;
    req.magic        = MAGIC_NUMBER;

    if (OrderSend(req, res))
    {
        Print("✅ 含み損自動決済成功: ticket=", ticket,
              " volume=", volume, " price=", close_price);
        string close_dir = (pos_type == POSITION_TYPE_BUY) ? "BUY_CLOSE" : "SELL_CLOSE";
        ReportTrade("LOSS_CUT", close_dir, close_price, 0, 0, volume, ticket);
    }
    else
    {
        Print("❌ 含み損自動決済失敗: retcode=", res.retcode, " comment=", res.comment,
              " ticket=", ticket, " volume=", volume);
    }
}

void ExecuteOrder(ENUM_ORDER_TYPE order_type, double sl_price, double tp_price)
{
    //--- 最小取引間隔チェック（ポジションなし時はスキップ）
    // ポジションがない状態では即エントリー可能（SLヒット後も素早く再エントリー）
    int total_open = CountPositions(POSITION_TYPE_BUY) + CountPositions(POSITION_TYPE_SELL);
    if (total_open == 0)
    {
        // ポジションなし: 間隔制限なしで即エントリー
    }
    else if (g_last_trade_time > 0 && TimeCurrent() - g_last_trade_time < MIN_TRADE_INTERVAL)
    {
        int remaining = (int)(MIN_TRADE_INTERVAL - (TimeCurrent() - g_last_trade_time));
        Print("⏳ 最小取引間隔中: あと", remaining, "秒 スキップ（ポジション保有中）");
        return;
    }

    double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double price  = (order_type == ORDER_TYPE_BUY) ? ask : bid;
    int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

    double sl, tp;
    if (USE_AI_SL_TP && sl_price > 0.0 && tp_price > 0.0)
    {
        sl = NormalizeDouble(sl_price, digits);
        tp = NormalizeDouble(tp_price, digits);

        // Gemini SL/TPの方向整合性チェック（逆方向SLは注文失敗の原因になる）
        bool sl_valid = (order_type == ORDER_TYPE_BUY)  ? (sl < price) : (sl > price);
        bool tp_valid = (order_type == ORDER_TYPE_BUY)  ? (tp > price) : (tp < price);
        if (!sl_valid || !tp_valid)
        {
            Print("⚠️ Gemini SL/TP方向不正: SL=", sl, " TP=", tp, " price=", price,
                  " → デフォルト値を使用");
            sl = NormalizeDouble((order_type == ORDER_TYPE_BUY) ? price - DEFAULT_SL_USD : price + DEFAULT_SL_USD, digits);
            tp = NormalizeDouble((order_type == ORDER_TYPE_BUY) ? price + DEFAULT_TP_USD : price - DEFAULT_TP_USD, digits);
        }
        else
            Print("💡 Gemini SL/TP: SL=", sl, " TP=", tp);
    }
    else
    {
        if (order_type == ORDER_TYPE_BUY)
        {
            sl = NormalizeDouble(price - DEFAULT_SL_USD, digits);
            tp = NormalizeDouble(price + DEFAULT_TP_USD, digits);
        }
        else
        {
            sl = NormalizeDouble(price + DEFAULT_SL_USD, digits);
            tp = NormalizeDouble(price - DEFAULT_TP_USD, digits);
        }
        Print("💡 デフォルト SL=", sl, " TP=", tp);
    }

    //--- リスク率からロット自動計算（口座通貨を自動考慮）
    double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
    double risk_amount = balance * (RISK_PERCENT / 100.0);
    double sl_distance = MathAbs(price - sl);
    double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tick_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    string currency   = AccountInfoString(ACCOUNT_CURRENCY);
    // XM JPY口座ではSYMBOL_TRADE_TICK_VALUEがUSD建て(≈1.0)で返るためUSJPYで換算
    if (currency == "JPY" && tick_value < 50.0)
    {
        double usdjpy = SymbolInfoDouble("USDJPY", SYMBOL_BID);
        if (usdjpy > 100.0) tick_value *= usdjpy;
    }
    double lot_size   = 0.01;
    if (sl_distance <= 0.0 || tick_value <= 0.0 || tick_size <= 0.0)
        Print("⚠️ ロット計算失敗（フォールバック0.01使用）: sl_distance=", sl_distance,
              " tick_value=", tick_value, " tick_size=", tick_size);
    if (sl_distance > 0.0 && tick_value > 0.0 && tick_size > 0.0)
    {
        double ticks_in_sl = sl_distance / tick_size;
        lot_size = risk_amount / (ticks_in_sl * tick_value);
    }
    double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    lot_size = MathMax(min_lot, MathMin(max_lot,
                   MathFloor(lot_size / lot_step) * lot_step));
    lot_size = NormalizeDouble(lot_size, 2);

    // ======== 初期SLをアプリ設定値(-$X)に相当する価格に上書き ========
    // ロット確定後に計算することで price_distance = initial_sl_usd / (lot × contract) が正確になる
    double cs = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
    if (lot_size > 0 && cs > 0)
    {
        FetchHybridSlConfig();  // 最新設定を確認（キャッシュ済みなら即return）
        double sl_price_dist  = g_cached_initial_sl_usd / (lot_size * cs);
        double initial_sl_price = NormalizeDouble(
            (order_type == ORDER_TYPE_BUY) ? price - sl_price_dist
                                           : price + sl_price_dist, digits);

        // より保護的な（エントリー価格に近い）SLを採用
        bool initial_is_tighter = (order_type == ORDER_TYPE_BUY) ? (initial_sl_price > sl)
                                                                  : (initial_sl_price < sl);
        if (initial_is_tighter)
        {
            Print("🛡️ 初期SL上書き: -$", g_cached_initial_sl_usd,
                  " → SL価格=", initial_sl_price,
                  " (価格幅=", NormalizeDouble(sl_price_dist, 2), ")");
            sl = initial_sl_price;
        }
        else
        {
            Print("🛡️ 初期SL: -$", g_cached_initial_sl_usd, " 相当=", initial_sl_price,
                  " / Gemini/デフォルトSL=", sl, " → 既存SLの方が保護的");
        }
    }

    Print("💰 残高:", balance, currency,
          " リスク:", NormalizeDouble(risk_amount, 2), currency,
          " SL幅:", NormalizeDouble(sl_distance, 2),
          " tick_value:", NormalizeDouble(tick_value, 4),
          " → ロット:", lot_size, " 最終SL:", sl);

    MqlTradeRequest req = {};
    MqlTradeResult  res = {};
    req.action       = TRADE_ACTION_DEAL;
    req.symbol       = _Symbol;
    req.volume       = lot_size;
    req.type         = order_type;
    req.price        = price;
    req.sl           = sl;
    req.tp           = tp;
    req.magic        = MAGIC_NUMBER;
    req.comment      = "GOLD_AI_EA";
    req.type_filling = ORDER_FILLING_IOC;

    if (OrderSend(req, res))
    {
        Print("✅ 注文成功: ", EnumToString(order_type),
              " price=", price, " SL=", sl, " TP=", tp,
              " ticket=", res.order);
        g_last_trade_time = TimeCurrent();
        string dir = (order_type == ORDER_TYPE_BUY) ? "BUY" : "SELL";
        ReportTrade("ORDER", dir, price, sl, tp, lot_size, res.order);
    }
    else
        Print("❌ 注文失敗: retcode=", res.retcode, " ", res.comment);
}

//+------------------------------------------------------------------+
//  JSON パーサー
//+------------------------------------------------------------------+
int JsonInt(const string &j, string key)
{
    int pos = StringFind(j, key);
    if (pos < 0) return -1;
    pos += StringLen(key);
    while (pos < StringLen(j) && StringGetCharacter(j, pos) == ' ') pos++;
    if (StringGetCharacter(j, pos) == 'n') return -1;
    string s = "";
    for (; pos < StringLen(j); pos++)
    {
        ushort c = StringGetCharacter(j, pos);
        if ((c >= '0' && c <= '9') || c == '-') s += ShortToString(c);
        else break;
    }
    return (int)StringToInteger(s);
}

double JsonDouble(const string &j, string key)
{
    int pos = StringFind(j, key);
    if (pos < 0) return 0.0;
    pos += StringLen(key);
    while (pos < StringLen(j) && StringGetCharacter(j, pos) == ' ') pos++;
    if (StringGetCharacter(j, pos) == 'n') return 0.0;
    string s = "";
    for (; pos < StringLen(j); pos++)
    {
        ushort c = StringGetCharacter(j, pos);
        if ((c >= '0' && c <= '9') || c == '-' || c == '.') s += ShortToString(c);
        else break;
    }
    return StringToDouble(s);
}

bool JsonBool(const string &j, string key)
{
    int pos = StringFind(j, key);
    if (pos < 0) return false;
    pos += StringLen(key);
    while (pos < StringLen(j) && StringGetCharacter(j, pos) == ' ') pos++;
    return StringSubstr(j, pos, 4) == "true";
}

string JsonString(const string &j, string key)
{
    int pos = StringFind(j, key);
    if (pos < 0) return "";
    pos += StringLen(key);
    while (pos < StringLen(j) && StringGetCharacter(j, pos) == ' ') pos++;
    if (StringGetCharacter(j, pos) == 'n') return "null";
    if (StringGetCharacter(j, pos) != '"') return "";
    pos++;
    string v = "";
    for (; pos < StringLen(j); pos++)
    {
        ushort c = StringGetCharacter(j, pos);
        if (c == '"') break;
        v += ShortToString(c);
    }
    return v;
}
//+------------------------------------------------------------------+
