//+------------------------------------------------------------------+
//|  GOLD AI Trader EA  v1.22                                        |
//|  Render API + Gemini AI シグナルによる自動売買                      |
//|  対象: XAUUSD (GOLD) M15                                         |
//|  決済: 逆クロスでドテン（SL/TPでも決済）                             |
//|  v1.22: MT5リアルタイム指標をサーバーにプッシュ（Yahoo Finance廃止）  |
//+------------------------------------------------------------------+
#property copyright "GOLD AI Trader"
#property version   "1.23"

//--- 入力パラメータ
input string   API_BASE         = "https://ai-trading-system-81jb.onrender.com";
input string   API_URL          = "https://ai-trading-system-81jb.onrender.com/latest-signal";
input int      POLL_SECONDS     = 15;    // APIポーリング間隔（秒）
input int      MIN_CONFIDENCE   = 50;    // 最低AI信頼度 (%) ※デモ用に緩め
input bool     REQUIRE_AI_VALID = false; // AI承認必須 ※デモ用にOFF
input double   RISK_PERCENT     = 2.0;   // 1トレードあたりのリスク率 (%)
input double   DEFAULT_SL_USD   = 20.0;  // デフォルトSL（価格幅ドル）
input double   DEFAULT_TP_USD   = 40.0;  // デフォルトTP（価格幅ドル）
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

//--- 最新スコア（ハートビートでサーバーに送るためグローバル保存）
int    g_latest_buy_score  = 0;
int    g_latest_sell_score = 0;
double g_latest_rsi        = 0.0;
double g_latest_adx        = 0.0;
double g_latest_close      = 0.0;
string g_latest_crossover  = "NONE";  // 最新のクロスオーバー方向（NONE/UP_CROSS/DOWN_CROSS）

//+------------------------------------------------------------------+
int OnInit()
{
    Print("=== GOLD AI Trader EA v1.23 起動 ===");
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
}
void OnTick()
{
    if (TimeCurrent() - g_last_poll_time >= POLL_SECONDS)
    {
        ComputeAndPushSignal();
        PollAndTrade();
    }
}

//+------------------------------------------------------------------+
//  MT5リアルタイム指標計算 → /ea-signal プッシュ（v1.22）
//+------------------------------------------------------------------+
void PushSignalToServer(string crossover, double close_price,
                        double rsi, double macd, double macd_sig,
                        double ema20, double ema50, double ema200,
                        double bb_upper, double bb_lower,
                        double stoch_k, double stoch_d,
                        double adx, double di_plus, double di_minus,
                        double atr, int buy_score, int sell_score,
                        string buy_reasons, string sell_reasons)
{
    // buy_reasons/sell_reasons は日本語のためJSON送信から除外（スコアと数値指標で代替）
    string json = "{"
        + "\"crossover\":\""    + crossover                          + "\""
        + ",\"latest_close\":"  + DoubleToString(close_price, 2)
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
        + ",\"buy_score\":"     + IntegerToString(buy_score)
        + ",\"sell_score\":"    + IntegerToString(sell_score)
        + "}";

    string headers = "Content-Type: application/json\r\n";
    char   post[], result[];
    string res_headers;
    StringToCharArray(json, post, 0, StringLen(json));

    int status = WebRequest("POST", API_BASE + "/ea-signal", headers, 8000, post, result, res_headers);
    if (status == 200)
        Print("✅ /ea-signal 送信成功: ", crossover,
              " close=", close_price, " 買い", buy_score, "点 売り", sell_score, "点");
    else
        Print("⚠️  /ea-signal 送信失敗: HTTP ", status, " (EAが稼働中でなければ正常)");
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
    if (CopyBuffer(g_h_bb,     0, 0, 2, bb_upper_buf) < 2) return;
    if (CopyBuffer(g_h_bb,     1, 0, 2, bb_lower_buf) < 2) return;
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

    if (crossover == "") return;  // シグナルなし

    // 同方向かつプッシュ間隔未満 → スキップ
    if (crossover == g_last_pushed_crossover &&
        TimeCurrent() - g_last_signal_push_time < SIGNAL_PUSH_INTERVAL) return;

    // サーバーにプッシュ
    PushSignalToServer(crossover, cur_close,
                       cur_rsi, cur_macd, cur_macd_sig,
                       cur_ema20, cur_ema50, cur_ema200,
                       cur_bb_upper, cur_bb_lower,
                       cur_stoch_k, cur_stoch_d,
                       cur_adx, cur_di_plus, cur_di_minus, cur_atr,
                       buy_score, sell_score, buy_reasons, sell_reasons);

    g_last_pushed_crossover = crossover;
    g_last_signal_push_time = TimeCurrent();
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
    WebRequest("POST", API_BASE + "/ea-heartbeat", hb_headers, 3000, hb_post, hb_result, hb_resp_headers);
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
    WebRequest("POST", API_BASE + "/ea-trade", rep_headers, 3000, rep_post, rep_result, rep_resp_headers);
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
    if (confidence >= 0 && confidence < MIN_CONFIDENCE)
    {
        Print("⛔ 信頼度不足: ", confidence, "% < ", MIN_CONFIDENCE, "%");
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
            double close_price = req.price;
            ReportTrade("CLOSE", close_dir, close_price, 0, 0,
                        PositionGetDouble(POSITION_VOLUME), ticket);
        }
        else
            Print("❌ 決済失敗: retcode=", res.retcode);
    }
}

//+------------------------------------------------------------------+
void ExecuteOrder(ENUM_ORDER_TYPE order_type, double sl_price, double tp_price)
{
    //--- 最小取引間隔チェック（チョッピー相場のノイズシグナル排除）
    if (g_last_trade_time > 0 && TimeCurrent() - g_last_trade_time < MIN_TRADE_INTERVAL)
    {
        int remaining = (int)(MIN_TRADE_INTERVAL - (TimeCurrent() - g_last_trade_time));
        Print("⏳ 最小取引間隔中: あと", remaining, "秒 スキップ");
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
    Print("💰 残高:", balance, currency,
          " リスク:", NormalizeDouble(risk_amount, 2), currency,
          " SL幅:", NormalizeDouble(sl_distance, 2),
          " tick_value:", NormalizeDouble(tick_value, 4),
          " → ロット:", lot_size);

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
