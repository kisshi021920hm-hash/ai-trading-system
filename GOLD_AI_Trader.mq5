//+------------------------------------------------------------------+
//|  GOLD AI Trader EA  v1.1                                         |
//|  Render API + Gemini AI シグナルによる自動売買                      |
//|  対象: XAUUSD (GOLD) M15                                         |
//|  決済: 逆クロスでドテン（SL/TPでも決済）                             |
//+------------------------------------------------------------------+
#property copyright "GOLD AI Trader"
#property version   "1.10"

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
input int      MAGIC_NUMBER     = 20260929;

//--- グローバル変数
int      g_last_signal_id    = -1;
datetime g_last_poll_time    = 0;
datetime g_last_heartbeat    = 0;   // 最後にハートビートを送った時刻

//+------------------------------------------------------------------+
int OnInit()
{
    Print("=== GOLD AI Trader EA v1.1 起動 ===");
    Print("API: ", API_URL);
    Print("ポーリング: ", POLL_SECONDS, "秒  最低信頼度: ", MIN_CONFIDENCE,
          "%  AI承認必須: ", REQUIRE_AI_VALID);
    Print("逆クロスドテン: ", FLIP_ON_REVERSE, "  リスク率: ", RISK_PERCENT, "%");
    Print("口座残高: $", AccountInfoDouble(ACCOUNT_BALANCE),
          "  通貨: ", AccountInfoString(ACCOUNT_CURRENCY));
    Print("⚠️  ツール→オプション→EA→WebRequest許可URLに追加:");
    Print("   https://ai-trading-system-81jb.onrender.com");
    EventSetTimer(POLL_SECONDS);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    EventKillTimer();
    Print("GOLD AI Trader EA 停止");
}

void OnTimer() { PollAndTrade(); }
void OnTick()  { if (TimeCurrent() - g_last_poll_time >= POLL_SECONDS) PollAndTrade(); }

//+------------------------------------------------------------------+
void SendHeartbeat()
{
    if (TimeCurrent() - g_last_heartbeat < 60) return;
    g_last_heartbeat = TimeCurrent();
    string hb_headers = "Content-Type: application/json\r\n";
    char   hb_post[], hb_result[];
    string hb_resp_headers;
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

    //--- リスク率からロット自動計算
    double balance       = AccountInfoDouble(ACCOUNT_BALANCE);
    double risk_amount   = balance * (RISK_PERCENT / 100.0);
    double sl_distance   = MathAbs(price - sl);
    double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
    double lot_size      = 0.01;
    if (sl_distance > 0.0 && contract_size > 0.0)
        lot_size = risk_amount / (sl_distance * contract_size);
    double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    lot_size = MathMax(min_lot, MathMin(max_lot,
                   MathFloor(lot_size / lot_step) * lot_step));
    lot_size = NormalizeDouble(lot_size, 2);
    Print("💰 残高:$", balance, " リスク:$", NormalizeDouble(risk_amount, 2),
          " SL幅:$", NormalizeDouble(sl_distance, 2),
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
