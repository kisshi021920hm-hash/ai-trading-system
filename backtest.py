"""
GOLD AI Trader バックテスト
app.py と同じ COMPOSITE シグナルロジックで過去データを検証
"""

import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ==================== 設定 ====================
SYMBOL = "GC=F"          # GOLD先物
TIMEFRAME = "15m"
PERIOD = "60d"           # 過去60日（yfinance 15分足の上限）
SL_PIPS = 5.0            # SL $/oz
TP_PIPS = 15.0           # TP $/oz
TRAIL_TRIGGER = 4.0      # トレーリング開始距離 $/oz
TRAIL_WIDTH = 3.0        # トレーリングSL幅 $/oz
MIN_SCORE_GAP = 2        # 買い/売りスコア差の最低値
SCORE_THRESHOLD = 3      # 最低スコア（composite）

# フィルター設定
RSI_FILTER_THRESHOLD = 35.0    # ①RSI<この値のシグナルはスキップ
SELL_MIN_GAP = 4               # ②SELLシグナルに必要な最低スコア差（BUYより厳しく）
ADX_FILTER_THRESHOLD = 20.0    # ③ADX<この値（レンジ相場）のシグナルはスキップ

# R/Sブレイク検出設定
SR_LOOKBACK = 100              # S/R検索対象の過去ローソク足数
SR_SWING_BARS = 3              # スイングポイント判定に使うバー数（左右各N本）


# ==================== 指標計算（app.py と同じ） ====================
def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(close, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig

def calculate_adx(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    dm_p = (high.diff()).where((high.diff() > low.diff().abs()) & (high.diff() > 0), 0)
    dm_m = (-low.diff()).where((low.diff().abs() > high.diff()) & (low.diff() < 0), 0)
    di_p = 100 * dm_p.ewm(span=period, adjust=False).mean() / atr
    di_m = 100 * dm_m.ewm(span=period, adjust=False).mean() / atr
    dx = 100 * (di_p - di_m).abs() / (di_p + di_m)
    adx = dx.ewm(span=period, adjust=False).mean()
    return adx, di_p, di_m

def calculate_bb(close, period=20, std_dev=2):
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    return sma + std_dev * std, sma - std_dev * std

def calculate_stoch(high, low, close, k=14, d=3):
    lo = low.rolling(k).min()
    hi = high.rolling(k).max()
    stoch_k = 100 * (close - lo) / (hi - lo)
    stoch_d = stoch_k.rolling(d).mean()
    return stoch_k, stoch_d

def find_key_levels(highs, lows, current_price):
    """R/Sレベルを検出（EA側の GOLD_AI_Trader.mq5 と同じロジック）
    スイングポイント：左右各 SR_SWING_BARS 本より高い/低い点
    過去 SR_LOOKBACK 本から最大 3 つのレジスタンス・サポートを検出
    """
    if len(highs) < SR_LOOKBACK:
        return [], [], []

    lookback_highs = highs[-SR_LOOKBACK:]
    lookback_lows = lows[-SR_LOOKBACK:]

    resistances = []
    supports = []

    # スイングハイ（レジスタンス候補）を探す
    for i in range(SR_SWING_BARS, len(lookback_highs) - SR_SWING_BARS):
        is_swing_high = True
        for j in range(1, SR_SWING_BARS + 1):
            if lookback_highs[i] <= lookback_highs[i - j] or lookback_highs[i] <= lookback_highs[i + j]:
                is_swing_high = False
                break
        if is_swing_high:
            resistances.append(lookback_highs[i])

    # スイングロー（サポート候補）を探す
    for i in range(SR_SWING_BARS, len(lookback_lows) - SR_SWING_BARS):
        is_swing_low = True
        for j in range(1, SR_SWING_BARS + 1):
            if lookback_lows[i] >= lookback_lows[i - j] or lookback_lows[i] >= lookback_lows[i + j]:
                is_swing_low = False
                break
        if is_swing_low:
            supports.append(lookback_lows[i])

    # 現在価格より上のレジスタンスのみを降順でソート
    resistances = sorted([r for r in resistances if r > current_price], reverse=True)[:3]
    # 現在価格より下のサポートのみを昇順でソート（最初の3個=最も近い）
    supports = sorted([s for s in supports if s < current_price])[:3]

    return resistances, supports, []


def detect_breakout(highs, lows, current_close, prev_close):
    """レジスタンスブレイクアウト検出"""
    res_levels, sup_levels, _ = find_key_levels(highs, lows, prev_close)

    if len(res_levels) > 0:
        # 最も近いレジスタンス
        res = res_levels[0]
        if prev_close <= res and current_close > res:
            return True, res
    return False, 0.0


def detect_breakdown(highs, lows, current_close, prev_close):
    """サポートブレイクダウン検出"""
    res_levels, sup_levels, _ = find_key_levels(highs, lows, prev_close)

    if len(sup_levels) > 0:
        # 最も近いサポート
        sup = sup_levels[0]
        if prev_close >= sup and current_close < sup:
            return True, sup
    return False, 0.0


def calculate_trendline_tp(highs, lows, current_price, is_buy):
    """トレンドラインから対面側までの距離を計算してTPを算出

    BUY（上昇トレンド）の場合：最も近いレジスタンスまで
    SELL（下降トレンド）の場合：最も近いサポートまで
    """
    res_levels, sup_levels, _ = find_key_levels(highs, lows, current_price)

    if is_buy and len(res_levels) > 0:
        tp_price = res_levels[0]
        tp_distance = tp_price - current_price
        return tp_distance if tp_distance > 0 else None
    elif not is_buy and len(sup_levels) > 0:
        tp_price = sup_levels[0]
        tp_distance = current_price - tp_price
        return tp_distance if tp_distance > 0 else None

    return None


def calc_lr_channel(close, idx, period, std_mult=1.5):
    """idx時点から過去period本の線形回帰チャネル上辺・下辺を返す"""
    start = idx - period + 1
    if start < 0:
        return None, None
    y = close[start:idx + 1].astype(float)
    x = np.arange(len(y), dtype=float)
    coeffs = np.polyfit(x, y, 1)
    y_pred = np.polyval(coeffs, x)
    std = np.std(y - y_pred)
    upper = float(y_pred[-1]) + std_mult * std
    lower = float(y_pred[-1]) - std_mult * std
    return upper, lower


def simulate_trades_channel(df_price, df_signals, channel_mode="replace_tp",
                             channel_period=30, channel_std=1.5,
                             sl=SL_PIPS, tp=TP_PIPS,
                             trail_trigger=TRAIL_TRIGGER, trail_width=TRAIL_WIDTH):
    """チャネルブレイク決済バリアントのトレードシミュレーション

    channel_mode:
      "replace_tp"      : SL残し、TPをチャネルブレイクに変更
      "with_trailing"   : トレーリング+チャネルブレイク（早い方）
      "only"            : チャネルブレイクのみ（SL/TPなし）
    """
    trades = []
    close = df_price['Close'].values
    high  = df_price['High'].values
    low   = df_price['Low'].values
    times = df_price.index

    sig_rows = df_signals[df_signals['crossover'].notna()].copy()
    sig_rows = sig_rows[sig_rows['crossover'] != sig_rows['crossover'].shift(1)]

    for _, row in sig_rows.iterrows():
        i = row['idx']
        direction  = row['crossover']
        entry_price = row['close']
        entry_time  = row['time']
        is_buy = direction == "UP_CROSS"

        sl_price = (entry_price - sl) if is_buy else (entry_price + sl)
        tp_price = (entry_price + tp) if is_buy else (entry_price - tp)

        exit_price  = None
        exit_time   = None
        exit_reason = None
        trail_peak  = entry_price
        trail_sl    = None
        breakeven_activated = False  # ブレイクイーブン移動済みフラグ

        for j in range(i + 1, min(i + 200, len(close))):
            h, l, c = high[j], low[j], close[j]

            # ── チャネル計算 ──
            ch_upper, ch_lower = calc_lr_channel(close, j, channel_period, channel_std)
            channel_ok = ch_upper is not None

            # ── トレーリング更新（with_trailingモード用） ──
            if channel_mode == "with_trailing":
                if is_buy:
                    if h > trail_peak: trail_peak = h
                    if trail_peak - entry_price >= trail_trigger:
                        new_ts = trail_peak - trail_width
                        if trail_sl is None or new_ts > trail_sl:
                            trail_sl = new_ts
                else:
                    if l < trail_peak: trail_peak = l
                    if entry_price - trail_peak >= trail_trigger:
                        new_ts = trail_peak + trail_width
                        if trail_sl is None or new_ts < trail_sl:
                            trail_sl = new_ts

            # ── ブレイクイーブン移動（breakeven_channelモード用） ──
            if channel_mode == "breakeven_channel" and not breakeven_activated:
                if is_buy and h - entry_price >= trail_trigger:
                    sl_price = entry_price  # SLをエントリー価格に移動
                    breakeven_activated = True
                elif not is_buy and entry_price - l >= trail_trigger:
                    sl_price = entry_price
                    breakeven_activated = True

            # ── 決済判定 ──
            if is_buy:
                # SL（replace_tp / with_trailing / breakeven_channelモード）
                if channel_mode != "only":
                    if l <= sl_price:
                        exit_price = sl_price
                        exit_reason = "BE" if (channel_mode == "breakeven_channel" and breakeven_activated) else "SL"
                        exit_time = times[j]; break
                # トレーリングSL
                if channel_mode == "with_trailing" and trail_sl and l <= trail_sl:
                    exit_price, exit_reason = trail_sl, "TRAIL"
                    exit_time = times[j]; break
                # チャネル下辺ブレイク → 上昇トレンド終了
                if channel_ok and c < ch_lower:
                    exit_price, exit_reason = c, "CH_BREAK"
                    exit_time = times[j]; break
            else:
                # SL
                if channel_mode != "only":
                    if h >= sl_price:
                        exit_price = sl_price
                        exit_reason = "BE" if (channel_mode == "breakeven_channel" and breakeven_activated) else "SL"
                        exit_time = times[j]; break
                # トレーリングSL
                if channel_mode == "with_trailing" and trail_sl and h >= trail_sl:
                    exit_price, exit_reason = trail_sl, "TRAIL"
                    exit_time = times[j]; break
                # チャネル上辺ブレイク → 下降トレンド終了
                if channel_ok and c > ch_upper:
                    exit_price, exit_reason = c, "CH_BREAK"
                    exit_time = times[j]; break

        if exit_price is None:
            last_j = min(i + 199, len(close) - 1)
            exit_price = close[last_j]
            exit_time  = times[last_j]
            exit_reason = "TIMEOUT"


        pnl = (exit_price - entry_price) if is_buy else (entry_price - exit_price)
        trades.append({
            'direction':   direction,
            'entry_time':  entry_time,
            'exit_time':   exit_time,
            'entry_price': entry_price,
            'exit_price':  exit_price,
            'pnl':         round(pnl, 2),
            'exit_reason': exit_reason,
            'rsi':  row['rsi'],
            'adx':  row['adx'],
            'buy_score':  row['buy_score'],
            'sell_score': row['sell_score'],
        })

    return pd.DataFrame(trades)


def simulate_trades_dynamic_sl(df_price, df_signals, channel_period=30, channel_std=1.5,
                                sl_buffer=5.0, use_fixed_tp=False, tp=TP_PIPS):
    """動的SL: チャネル下辺/上辺 ± バッファ でSLを毎足更新（上昇のみ）

    BUY: SL = max(current_sl, ch_lower - sl_buffer)  ← 毎足上方更新
    SELL: SL = min(current_sl, ch_upper + sl_buffer) ← 毎足下方更新
    """
    trades = []
    close = df_price['Close'].values
    high  = df_price['High'].values
    low   = df_price['Low'].values
    times = df_price.index

    sig_rows = df_signals[df_signals['crossover'].notna()].copy()
    sig_rows = sig_rows[sig_rows['crossover'] != sig_rows['crossover'].shift(1)]

    for _, row in sig_rows.iterrows():
        i = row['idx']
        direction   = row['crossover']
        entry_price = row['close']
        entry_time  = row['time']
        is_buy = direction == "UP_CROSS"

        # 初期SL: エントリー時のチャネル下辺/上辺 ± バッファ
        ch_upper0, ch_lower0 = calc_lr_channel(close, i, channel_period, channel_std)
        if ch_lower0 is not None:
            sl_price = (ch_lower0 - sl_buffer) if is_buy else (ch_upper0 + sl_buffer)
        else:
            sl_price = (entry_price - sl_buffer * 2) if is_buy else (entry_price + sl_buffer * 2)

        tp_price = (entry_price + tp) if is_buy else (entry_price - tp)

        exit_price  = None
        exit_time   = None
        exit_reason = None

        for j in range(i + 1, min(i + 200, len(close))):
            h, l, c = high[j], low[j], close[j]

            # チャネル計算して動的SL更新
            ch_upper, ch_lower = calc_lr_channel(close, j, channel_period, channel_std)
            if ch_lower is not None:
                if is_buy:
                    new_sl = ch_lower - sl_buffer
                    if new_sl > sl_price:   # SLは上にしか動かない
                        sl_price = new_sl
                else:
                    new_sl = ch_upper + sl_buffer
                    if new_sl < sl_price:   # SLは下にしか動かない
                        sl_price = new_sl

            # 決済判定
            if is_buy:
                if l <= sl_price:
                    exit_price = sl_price
                    exit_reason = "DYN_SL"
                    exit_time = times[j]; break
                if use_fixed_tp and h >= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exit_time = times[j]; break
            else:
                if h >= sl_price:
                    exit_price = sl_price
                    exit_reason = "DYN_SL"
                    exit_time = times[j]; break
                if use_fixed_tp and l <= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exit_time = times[j]; break

        if exit_price is None:
            last_j = min(i + 199, len(close) - 1)
            exit_price = close[last_j]
            exit_time  = times[last_j]
            exit_reason = "TIMEOUT"

        pnl = (exit_price - entry_price) if is_buy else (entry_price - exit_price)
        trades.append({
            'direction':   direction,
            'entry_time':  entry_time,
            'exit_time':   exit_time,
            'entry_price': entry_price,
            'exit_price':  exit_price,
            'pnl':         round(pnl, 2),
            'exit_reason': exit_reason,
            'rsi':  row['rsi'],
            'adx':  row['adx'],
            'buy_score':  row['buy_score'],
            'sell_score': row['sell_score'],
        })

    return pd.DataFrame(trades)


def simulate_trades_hybrid_flip(df_price, df_signals,
                                sl=SL_PIPS, be_trigger=None,
                                adx_min=None, min_score_gap=None):
    """クロス転換決済 ハイブリッド戦略

    be_trigger  : この利益($/oz)に達したらSLをBEに移動し、以後クロス転換まで保有
                  None = BEなし（純粋クロス転換）
    adx_min     : この値以上のシグナルのみ適用（Noneで無効）
    min_score_gap: スコア差がこの値以上のシグナルのみ（Noneで無効）
    """
    close = df_price['Close'].values
    high  = df_price['High'].values
    low   = df_price['Low'].values
    times = df_price.index

    # バーごとにアクティブクロス方向を記録
    active_cross = [None] * len(close)
    sig_rows_all = df_signals[df_signals['crossover'].notna()].sort_values('idx')
    current_cross = None
    sig_iter = iter(sig_rows_all.iterrows())
    next_sig = next(sig_iter, None)
    for i in range(len(close)):
        while next_sig is not None and next_sig[1]['idx'] <= i:
            current_cross = next_sig[1]['crossover']
            next_sig = next(sig_iter, None)
        active_cross[i] = current_cross

    # シグナルを方向変化のみに絞る
    sig_rows = sig_rows_all.copy()
    sig_rows = sig_rows[sig_rows['crossover'] != sig_rows['crossover'].shift(1)]

    trades = []
    for _, row in sig_rows.iterrows():
        i0       = int(row['idx'])
        direction = row['crossover']
        is_buy   = direction == 'UP_CROSS'
        ep       = row['close']
        adx_val  = row['adx']
        score_gap = abs(row['buy_score'] - row['sell_score'])

        # フィルター
        if adx_min is not None and adx_val < adx_min:
            continue
        if min_score_gap is not None and score_gap < min_score_gap:
            continue

        sl_price    = ep - sl if is_buy else ep + sl
        be_reached  = False
        exit_price  = None
        exit_reason = None
        exit_idx    = None

        for j in range(i0 + 1, min(i0 + 800, len(close))):
            h, l, c = high[j], low[j], close[j]

            # 反対クロス → 決済
            if active_cross[j] is not None and active_cross[j] != direction:
                exit_price  = c
                exit_reason = 'CROSS_FLIP'
                exit_idx    = j; break

            # BEトリガー到達でSLをBEへ移動
            if be_trigger is not None and not be_reached:
                if is_buy  and h >= ep + be_trigger:
                    sl_price   = ep        # SLをエントリー価格（BE）へ
                    be_reached = True
                if not is_buy and l <= ep - be_trigger:
                    sl_price   = ep
                    be_reached = True

            # SL判定
            if is_buy  and l <= sl_price:
                exit_price  = sl_price
                exit_reason = 'BE' if be_reached else 'SL'
                exit_idx    = j; break
            if not is_buy and h >= sl_price:
                exit_price  = sl_price
                exit_reason = 'BE' if be_reached else 'SL'
                exit_idx    = j; break

        if exit_price is None:
            last_j      = min(i0 + 799, len(close) - 1)
            exit_price  = close[last_j]
            exit_reason = 'TIMEOUT'
            exit_idx    = last_j

        pnl = (exit_price - ep) if is_buy else (ep - exit_price)
        trades.append({
            'direction':   direction,
            'entry_time':  times[i0],
            'exit_time':   times[exit_idx],
            'entry_price': ep,
            'exit_price':  exit_price,
            'pnl':         round(pnl, 2),
            'exit_reason': exit_reason,
            'adx':         adx_val,
            'score_gap':   score_gap,
            'be_reached':  be_reached,
        })

    return pd.DataFrame(trades) if trades else pd.DataFrame()


def simulate_trades_active_reentry(df_price, df_signals,
                                   sl=SL_PIPS, tp=TP_PIPS,
                                   trail_trigger=TRAIL_TRIGGER, trail_width=TRAIL_WIDTH,
                                   use_trailing=True, max_reentry=10,
                                   trail_only=False, max_bars=None):
    """クロス継続中の再エントリー戦略

    クロスシグナルが有効な間（反対クロスが出るまで）、
    決済後も同方向に再エントリーし続ける。
    通常のSL/TP/トレーリングパラメータをそのまま使用。
    """
    close = df_price['Close'].values
    high  = df_price['High'].values
    low   = df_price['Low'].values
    times = df_price.index

    # バーごとにアクティブクロス方向を記録（最後のクロスを継承）
    active_cross = [None] * len(close)
    sig_rows = df_signals[df_signals['crossover'].notna()].copy()
    sig_rows = sig_rows.sort_values('idx')
    current_cross = None
    sig_iter = sig_rows.iterrows()
    next_sig = next(sig_iter, None)
    for i in range(len(close)):
        while next_sig is not None and next_sig[1]['idx'] <= i:
            current_cross = next_sig[1]['crossover']
            next_sig = next(sig_iter, None)
        active_cross[i] = current_cross

    all_trades = []
    processed_signals = set()

    for _, row in sig_rows.iterrows():
        i0 = int(row['idx'])
        direction = row['crossover']
        is_buy = direction == "UP_CROSS"

        # 既にこのシグナル起点でエントリー済みならスキップ
        if i0 in processed_signals:
            continue
        processed_signals.add(i0)

        entry_idx   = i0
        entry_price = row['close']
        rentry_count = 0

        while rentry_count <= max_reentry and entry_idx < len(close) - 2:
            ep = entry_price
            trail_peak = ep
            trail_sl   = None
            sub_exit_price  = None
            sub_exit_reason = None
            sub_exit_idx    = None

            for j in range(entry_idx + 1, min(entry_idx + 400, len(close))):
                h, l, c = high[j], low[j], close[j]

                # アクティブクロスが反転 → 即決済
                if active_cross[j] != direction and active_cross[j] is not None:
                    sub_exit_price  = c
                    sub_exit_reason = "CROSS_FLIP"
                    sub_exit_idx    = j; break

                # TP
                if is_buy and h >= ep + tp:
                    sub_exit_price  = ep + tp
                    sub_exit_reason = "TP"
                    sub_exit_idx    = j; break
                if not is_buy and l <= ep - tp:
                    sub_exit_price  = ep - tp
                    sub_exit_reason = "TP"
                    sub_exit_idx    = j; break

                # SL
                if is_buy and l <= ep - sl:
                    sub_exit_price  = ep - sl
                    sub_exit_reason = "SL"
                    sub_exit_idx    = j; break
                if not is_buy and h >= ep + sl:
                    sub_exit_price  = ep + sl
                    sub_exit_reason = "SL"
                    sub_exit_idx    = j; break

                # トレーリング
                if use_trailing:
                    if is_buy:
                        if h > trail_peak: trail_peak = h
                        if trail_peak - ep >= trail_trigger:
                            new_ts = trail_peak - trail_width
                            if trail_sl is None or new_ts > trail_sl:
                                trail_sl = new_ts
                        if trail_sl and l <= trail_sl:
                            sub_exit_price  = trail_sl
                            sub_exit_reason = "TRAIL"
                            sub_exit_idx    = j; break
                    else:
                        if l < trail_peak: trail_peak = l
                        if ep - trail_peak >= trail_trigger:
                            new_ts = trail_peak + trail_width
                            if trail_sl is None or new_ts < trail_sl:
                                trail_sl = new_ts
                        if trail_sl and h >= trail_sl:
                            sub_exit_price  = trail_sl
                            sub_exit_reason = "TRAIL"
                            sub_exit_idx    = j; break

            if sub_exit_price is None:
                last_j = min(entry_idx + 399, len(close) - 1)
                sub_exit_price  = close[last_j]
                sub_exit_reason = "TIMEOUT"
                sub_exit_idx    = last_j

            pnl = (sub_exit_price - ep) if is_buy else (ep - sub_exit_price)
            all_trades.append({
                'direction':   direction,
                'entry_time':  times[entry_idx],
                'exit_time':   times[sub_exit_idx],
                'entry_price': ep,
                'exit_price':  sub_exit_price,
                'pnl':         round(pnl, 2),
                'exit_reason': sub_exit_reason,
                'reentry_no':  rentry_count,
                'rsi':  row['rsi'],
                'adx':  row['adx'],
            })

            # クロス反転 or TP or タイムアウト → セッション終了
            if sub_exit_reason in ("CROSS_FLIP", "TP", "TIMEOUT"):
                break

            # SL or TRAIL → 再エントリー判定
            if trail_only and sub_exit_reason == "SL":
                break  # TRAIL後のみの場合、SLで終了

            next_idx = sub_exit_idx + 1
            if next_idx >= len(close):
                break
            if active_cross[next_idx] != direction:
                break  # クロスが変わっていたら再エントリーしない
            if max_bars is not None and (next_idx - i0) > max_bars:
                break  # シグナルからN本以上経過したら終了

            entry_idx   = next_idx
            entry_price = close[next_idx]
            rentry_count += 1

    df_trades = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()
    return df_trades


def simulate_trades_scalping(df_price, df_signals,
                             trail_trigger=2.0, trail_width=1.5,
                             adx_min=25.0, channel_period=30, channel_std=1.5,
                             max_reentry=10):
    """スキャルピング再エントリー戦略

    トレーリング決済後、チャネル継続＋ADX>=adx_minなら即再エントリー。
    チャネルブレイク or ADX低下で停止。
    """
    all_trades = []   # 全サブトレード記録
    sessions  = []    # セッション単位（シグナル→トレンド終了）

    close = df_price['Close'].values
    high  = df_price['High'].values
    low   = df_price['Low'].values
    adx_arr = df_price.get('adx_pre', None)  # 事前計算ADX（なければ都度計算）
    times = df_price.index

    sig_rows = df_signals[df_signals['crossover'].notna()].copy()
    sig_rows = sig_rows[sig_rows['crossover'] != sig_rows['crossover'].shift(1)]

    for _, row in sig_rows.iterrows():
        i0 = row['idx']
        direction = row['crossover']
        is_buy = direction == "UP_CROSS"
        if row['adx'] < adx_min:
            continue  # ADX不足でスキップ

        session_pnl = 0.0
        session_trades = 0
        entry_idx = i0
        entry_price = row['close']
        reentry_count = 0

        while reentry_count <= max_reentry and entry_idx < len(close) - 2:
            # ── 1サブトレード ──
            ep = entry_price
            trail_peak = ep
            trail_sl = None
            sub_exit_price = None
            sub_exit_reason = None
            sub_exit_idx = None

            for j in range(entry_idx + 1, min(entry_idx + 200, len(close))):
                h, l, c = high[j], low[j], close[j]

                # チャネルチェック
                ch_upper, ch_lower = calc_lr_channel(close, j, channel_period, channel_std)
                channel_ok = ch_upper is not None

                # ADXチェック（df_signalsのADXを参照できないのでチャネルで代替）
                # チャネルブレイク = トレンド終了
                if channel_ok:
                    if is_buy and c < ch_lower:
                        sub_exit_price  = c
                        sub_exit_reason = "CH_BREAK"
                        sub_exit_idx    = j; break
                    if not is_buy and c > ch_upper:
                        sub_exit_price  = c
                        sub_exit_reason = "CH_BREAK"
                        sub_exit_idx    = j; break

                # トレーリング更新
                if is_buy:
                    if h > trail_peak: trail_peak = h
                    if trail_peak - ep >= trail_trigger:
                        new_ts = trail_peak - trail_width
                        if trail_sl is None or new_ts > trail_sl:
                            trail_sl = new_ts
                    if trail_sl and l <= trail_sl:
                        sub_exit_price  = trail_sl
                        sub_exit_reason = "TRAIL"
                        sub_exit_idx    = j; break
                else:
                    if l < trail_peak: trail_peak = l
                    if ep - trail_peak >= trail_trigger:
                        new_ts = trail_peak + trail_width
                        if trail_sl is None or new_ts < trail_sl:
                            trail_sl = new_ts
                    if trail_sl and h >= trail_sl:
                        sub_exit_price  = trail_sl
                        sub_exit_reason = "TRAIL"
                        sub_exit_idx    = j; break

            if sub_exit_price is None:
                last_j = min(entry_idx + 199, len(close) - 1)
                sub_exit_price  = close[last_j]
                sub_exit_reason = "TIMEOUT"
                sub_exit_idx    = last_j

            pnl = (sub_exit_price - ep) if is_buy else (ep - sub_exit_price)
            session_pnl += pnl
            session_trades += 1
            all_trades.append({
                'direction':    direction,
                'entry_time':   times[entry_idx],
                'exit_time':    times[sub_exit_idx],
                'entry_price':  ep,
                'exit_price':   sub_exit_price,
                'pnl':          round(pnl, 2),
                'exit_reason':  sub_exit_reason,
                'reentry_no':   reentry_count,
                'rsi':  row['rsi'],
                'adx':  row['adx'],
            })

            # チャネルブレイク or タイムアウト → セッション終了
            if sub_exit_reason in ("CH_BREAK", "TIMEOUT"):
                break

            # TRAIL決済 → 即再エントリー判定
            entry_idx   = sub_exit_idx
            entry_price = sub_exit_price
            reentry_count += 1

        sessions.append({
            'direction':      direction,
            'session_pnl':    round(session_pnl, 2),
            'trade_count':    session_trades,
            'rsi':  row['rsi'],
            'adx':  row['adx'],
        })

    df_trades   = pd.DataFrame(all_trades)   if all_trades else pd.DataFrame()
    df_sessions = pd.DataFrame(sessions)     if sessions   else pd.DataFrame()
    return df_trades, df_sessions


def compute_composite(df):
    """app.py の compute_signal_composite と同じスコアリング"""
    close = df['Close']
    high = df['High']
    low = df['Low']

    rsi = calculate_rsi(close)
    macd, macd_sig = calculate_macd(close)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=min(200, len(close)-1), adjust=False).mean()
    sma25  = close.rolling(25).mean()
    sma75  = close.rolling(75).mean()
    sma200 = close.rolling(200).mean()
    bb_up, bb_lo = calculate_bb(close)
    stoch_k, stoch_d = calculate_stoch(high, low, close)
    adx, di_p, di_m = calculate_adx(high, low, close)

    signals = []
    highs_list = high.tolist()
    lows_list = low.tolist()
    close_list = close.tolist()

    for i in range(60, len(df)):
        buy, sell = 0, 0

        # 1. EMA
        if ema20.iloc[i] > ema50.iloc[i]: buy += 1
        else: sell += 1
        if close.iloc[i] > ema200.iloc[i]: buy += 1
        else: sell += 1

        # 2. MACD クロス
        if macd.iloc[i] > macd_sig.iloc[i] and macd.iloc[i-1] <= macd_sig.iloc[i-1]: buy += 1
        elif macd.iloc[i] < macd_sig.iloc[i] and macd.iloc[i-1] >= macd_sig.iloc[i-1]: sell += 1

        # 3. RSI
        if rsi.iloc[i] > 55: buy += 1
        elif rsi.iloc[i] < 45: sell += 1

        # 4. BB
        if close.iloc[i] > bb_up.iloc[i]: buy += 1
        elif close.iloc[i] < bb_lo.iloc[i]: sell += 1

        # 5. Stochastic
        if stoch_k.iloc[i] > stoch_d.iloc[i] and stoch_k.iloc[i] < 80: buy += 1
        elif stoch_k.iloc[i] < stoch_d.iloc[i] and stoch_k.iloc[i] > 20: sell += 1

        # 6. ADX/DI
        if di_p.iloc[i] > di_m.iloc[i] and adx.iloc[i] > 20: buy += 1
        elif di_m.iloc[i] > di_p.iloc[i] and adx.iloc[i] > 20: sell += 1

        crossover = None
        if buy >= SCORE_THRESHOLD and buy > sell + 1:
            crossover = "UP_CROSS"
        elif sell >= SCORE_THRESHOLD and sell > buy + 1:
            crossover = "DOWN_CROSS"

        # R/Sブレイク検出
        is_breakout, breakout_lvl = detect_breakout(highs_list[:i+1], lows_list[:i+1], close_list[i], close_list[i-1])
        is_breakdown, breakdown_lvl = detect_breakdown(highs_list[:i+1], lows_list[:i+1], close_list[i], close_list[i-1])

        signals.append({
            'idx': i,
            'time': df.index[i],
            'close': close.iloc[i],
            'crossover': crossover,
            'buy_score': buy,
            'sell_score': sell,
            'rsi': rsi.iloc[i],
            'adx':   adx.iloc[i],
            'di_p':  di_p.iloc[i],
            'di_m':  di_m.iloc[i],
            'sma25':  sma25.iloc[i],
            'sma75':  sma75.iloc[i],
            'sma200': sma200.iloc[i],
            'macd': macd.iloc[i],
            'macd_sig': macd_sig.iloc[i],
            'is_breakout': is_breakout,
            'breakout_lvl': breakout_lvl,
            'is_breakdown': is_breakdown,
            'breakdown_lvl': breakdown_lvl,
        })

    return pd.DataFrame(signals)


# ==================== トレードシミュレーション ====================
def get_utc_hour(t):
    try:
        import pandas as pd
        ts = pd.Timestamp(t)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        return ts.hour
    except:
        return -1

def apply_adx_adaptive_flip(df_sig, adx_threshold=25.0):
    """ADX値に基づいてドテン無効化（トレンド中は逆クロスを無視）

    トレンド中（ADX > threshold）：逆クロスシグナルを削除（ドテンなし）
    レンジ中（ADX < threshold）：逆クロスシグナルを保持（ドテン有り）
    """
    df = df_sig.copy()

    # トレンド中（ADX > threshold）の逆方向シグナルを削除
    # 例：UP_CROSS状態でADX高時にDOWN_CROSSが出ても無視
    current_position = None  # UP_CROSS or DOWN_CROSS or None

    for i in range(len(df)):
        if df.iloc[i]['crossover'] is None:
            continue

        # トレンド判定
        is_trend = df.iloc[i]['adx'] > adx_threshold

        if current_position is None:
            # ポジションなし：シグナルを受け入れる
            current_position = df.iloc[i]['crossover']
        else:
            # ポジションあり
            new_signal = df.iloc[i]['crossover']
            if new_signal != current_position:
                # 逆方向シグナル
                if is_trend:
                    # トレンド中：逆クロスを無視（シグナル削除）
                    df.at[i, 'crossover'] = None
                else:
                    # レンジ中：逆クロスを受け入れ（ドテン）
                    current_position = new_signal
            else:
                # 同方向シグナル：無視
                df.at[i, 'crossover'] = None

    return df


def apply_filters(df_sig, rsi_filter=False, sell_gap_filter=False, adx_filter=False,
                  adx_threshold=ADX_FILTER_THRESHOLD, skip_sessions=None, breakout_filter=False, breakout_mode="single"):
    """①RSIフィルター ②SELL方向スコア差フィルター ③ADXフィルター ④時間帯フィルター ⑤R/Sブレイクフィルターを適用

    breakout_filter: R/Sブレイク検出を使用するか
    breakout_mode:
        "single": ブレイク単独でエントリー（7指標不要）
        "and": ブレイク AND 7指標スコア3以上
        "or": ブレイク OR 7指標スコア3以上

    skip_sessions: スキップする時間帯リスト (例: ["東京", "深夜"])
    """
    SESSION_RANGES = {
        "東京":   (0, 8),
        "ロンドン": (8, 13),
        "NY":    (13, 22),
        "深夜":   (22, 24),
    }
    df = df_sig.copy()
    df['gap'] = (df['buy_score'] - df['sell_score']).abs()

    if rsi_filter:
        df.loc[df['rsi'] < RSI_FILTER_THRESHOLD, 'crossover'] = None
    if sell_gap_filter:
        mask = (df['crossover'] == 'DOWN_CROSS') & (df['gap'] < SELL_MIN_GAP)
        df.loc[mask, 'crossover'] = None
    if adx_filter:
        df.loc[df['adx'] < adx_threshold, 'crossover'] = None
    if skip_sessions:
        hours = df['time'].apply(get_utc_hour)
        for sess in skip_sessions:
            if sess in SESSION_RANGES:
                h_start, h_end = SESSION_RANGES[sess]
                df.loc[(hours >= h_start) & (hours < h_end), 'crossover'] = None

    # ⑤ R/Sブレイク検出
    if breakout_filter:
        if breakout_mode == "single":
            # ブレイク単独: 7指標なし、ブレイク検出のみ
            df['crossover'] = None
            df.loc[df['is_breakout'], 'crossover'] = "UP_CROSS"
            df.loc[df['is_breakdown'], 'crossover'] = "DOWN_CROSS"
        elif breakout_mode == "and":
            # ブレイク AND 7指標: 両方満たす場合のみ
            has_signal = df['crossover'].notna()
            has_breakout = (df['is_breakout'] & (df['crossover'] == 'UP_CROSS')) | (df['is_breakdown'] & (df['crossover'] == 'DOWN_CROSS'))
            df.loc[~(has_signal & has_breakout), 'crossover'] = None
        elif breakout_mode == "or":
            # ブレイク OR 7指標: どちらかが満たす場合
            has_signal = df['crossover'].notna()
            has_breakout = (df['is_breakout'] & (df['crossover'] != 'DOWN_CROSS')) | (df['is_breakdown'] & (df['crossover'] != 'UP_CROSS'))
            # シグナルがない場合、ブレイクだけでエントリー
            df.loc[~has_signal & df['is_breakout'], 'crossover'] = "UP_CROSS"
            df.loc[~has_signal & df['is_breakdown'], 'crossover'] = "DOWN_CROSS"

    return df


def simulate_trades(df_price, df_signals, sl=SL_PIPS, tp=TP_PIPS,
                    trail_trigger=TRAIL_TRIGGER, trail_width=TRAIL_WIDTH,
                    use_trailing=True, use_adx_adaptive_tp=False, adx_trend_threshold=25.0,
                    use_adx_adaptive_flip=False, use_trendline_exit=False):
    """
    use_adx_adaptive_tp: ADX値に基づいてTPを動的に設定
    use_adx_adaptive_flip: ADX値に基づいてドテン無効化
      - トレンド（ADX > threshold）：ドテンなし（逆クロス無視）
      - レンジ（ADX < threshold）：ドテン有り（逆クロスで決済）
    use_trendline_exit: トレンドライン逸脱での自動決済
    """
    trades = []
    close = df_price['Close'].values
    high = df_price['High'].values
    low = df_price['Low'].values
    times = df_price.index

    # クロスオーバーシグナルのみ
    sig_rows = df_signals[df_signals['crossover'].notna()].copy()

    # ADX適応的ドテン無効化の場合、トレンド時の連続同方向処理をスキップ
    if not use_adx_adaptive_flip:
        # 連続同方向シグナルは最初だけ（方向が変わるまで再エントリーしない）
        sig_rows = sig_rows[sig_rows['crossover'] != sig_rows['crossover'].shift(1)]

    for _, row in sig_rows.iterrows():
        i = row['idx']
        direction = row['crossover']
        entry_price = row['close']
        entry_time = row['time']

        is_buy = direction == "UP_CROSS"

        # TPの計算（ADX適応的 or 固定）
        if use_adx_adaptive_tp and row['adx'] > adx_trend_threshold:
            # トレンド相場：対面側ラインまで
            trendline_tp = calculate_trendline_tp(high[:i+1], low[:i+1], entry_price, is_buy)
            if trendline_tp is not None:
                tp_distance = trendline_tp
            else:
                tp_distance = tp
        else:
            # レンジ相場 or 固定TP：固定値を使用
            tp_distance = tp

        if is_buy:
            sl_price = entry_price - sl
            tp_price = entry_price + tp_distance
        else:
            sl_price = entry_price + sl
            tp_price = entry_price - tp_distance

        # 最大100本後まで追跡
        exit_price = None
        exit_time = None
        exit_reason = None
        trail_peak = entry_price
        trail_sl = None

        for j in range(i + 1, min(i + 100, len(close))):
            h, l = high[j], low[j]

            # トレーリング更新
            if use_trailing:
                if is_buy:
                    if h > trail_peak:
                        trail_peak = h
                    profit_dist = trail_peak - entry_price
                    if profit_dist >= trail_trigger:
                        new_trail_sl = trail_peak - trail_width
                        if trail_sl is None or new_trail_sl > trail_sl:
                            trail_sl = new_trail_sl
                else:
                    if l < trail_peak:
                        trail_peak = l
                    profit_dist = entry_price - trail_peak
                    if profit_dist >= trail_trigger:
                        new_trail_sl = trail_peak + trail_width
                        if trail_sl is None or new_trail_sl < trail_sl:
                            trail_sl = new_trail_sl

            # SL/TP/Trail チェック
            if is_buy:
                if trail_sl and l <= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "TRAIL"
                    exit_time = times[j]
                    break
                if l <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exit_time = times[j]
                    break
                if h >= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exit_time = times[j]
                    break
            else:
                if trail_sl and h >= trail_sl:
                    exit_price = trail_sl
                    exit_reason = "TRAIL"
                    exit_time = times[j]
                    break
                if h >= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exit_time = times[j]
                    break
                if l <= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exit_time = times[j]
                    break

        if exit_price is None:
            # 100本後も未決済 → 時間切れで現在値決済
            last_j = min(i + 99, len(close) - 1)
            exit_price = close[last_j]
            exit_time = times[last_j]
            exit_reason = "TIMEOUT"

        pnl = (exit_price - entry_price) if is_buy else (entry_price - exit_price)
        trades.append({
            'direction': direction,
            'entry_time': entry_time,
            'exit_time': exit_time,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl': round(pnl, 2),
            'exit_reason': exit_reason,
            'rsi': row['rsi'],
            'adx': row['adx'],
            'buy_score': row['buy_score'],
            'sell_score': row['sell_score'],
        })

    return pd.DataFrame(trades)


# ==================== 統計表示 ====================
def print_stats(label, trades):
    if len(trades) == 0:
        print(f"  {label}: データなし")
        return
    wins = trades[trades['pnl'] > 0]
    losses = trades[trades['pnl'] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
    total_pnl = trades['pnl'].sum()
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    bar_w = int(win_rate / 5)
    bar_l = 20 - bar_w
    bar = "█" * bar_w + "░" * bar_l

    print(f"  {label:<22} [{bar}] {win_rate:5.1f}%  "
          f"取引:{len(trades):3d}  合計:{total_pnl:+7.1f}$/oz  "
          f"平均勝:{avg_win:+.1f} 平均負:{avg_loss:+.1f}  RR:{rr:.2f}")


def get_session(t):
    """UTCで時間帯判定"""
    try:
        h = t.hour if hasattr(t, 'hour') else pd.Timestamp(t).hour
    except:
        return "不明"
    if 0 <= h < 8:   return "東京(0-8UTC)"
    if 8 <= h < 13:  return "ロンドン(8-13UTC)"
    if 13 <= h < 22: return "NY(13-22UTC)"
    return "深夜(22-24UTC)"


def get_adx_zone(adx):
    if adx < 20:   return "レンジ(<20)"
    if adx < 25:   return "移行(20-25)"
    return "トレンド(>25)"


def calc_max_drawdown(trades):
    cumulative = trades['pnl'].cumsum()
    peak = cumulative.cummax()
    dd = cumulative - peak
    return dd.min()


# ==================== メイン ====================
def main():
    print("=" * 72)
    print("  GOLD AI Trader バックテスト")
    print(f"  期間: 過去{PERIOD} / 足種: {TIMEFRAME} / SL:{SL_PIPS}$/oz TP:{TP_PIPS}$/oz")
    print(f"  トレーリング: 開始{TRAIL_TRIGGER}$/oz 幅{TRAIL_WIDTH}$/oz")
    print("=" * 72)

    # データ取得
    print("\n📥 データ取得中...")
    df = yf.download(SYMBOL, period=PERIOD, interval=TIMEFRAME, progress=False)
    if df.empty:
        print("❌ データ取得失敗")
        return

    # MultiIndex対応
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    print(f"  取得: {len(df)}本  {df.index[0]} 〜 {df.index[-1]}")

    # シグナル計算
    print("\n📊 シグナル計算中...")
    df_sig = compute_composite(df)
    cross_count = df_sig['crossover'].notna().sum()
    print(f"  総シグナル数: {cross_count}件")

    # トレードシミュレーション（トレーリングあり）
    print("\n⚙️  トレードシミュレーション中...")
    trades = simulate_trades(df, df_sig, use_trailing=True)

    if len(trades) == 0:
        print("❌ トレードデータなし")
        return

    # セッション・ADXゾーン追加
    trades['session'] = trades['entry_time'].apply(get_session)
    trades['adx_zone'] = trades['adx'].apply(get_adx_zone)

    wins = trades[trades['pnl'] > 0]
    losses = trades[trades['pnl'] <= 0]
    total_pnl = trades['pnl'].sum()
    win_rate = len(wins) / len(trades) * 100
    max_dd = calc_max_drawdown(trades)

    print(f"\n{'=' * 72}")
    print(f"  【総合成績】")
    print(f"  総取引数: {len(trades)}  勝率: {win_rate:.1f}%  合計損益: {total_pnl:+.1f}$/oz")
    print(f"  最大ドローダウン: {max_dd:.1f}$/oz")
    avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    print(f"  平均利益: {avg_win:+.2f}$/oz  平均損失: {avg_loss:+.2f}$/oz  RR比: {rr:.2f}")

    # 決済理由内訳
    reasons = trades['exit_reason'].value_counts()
    print(f"  決済内訳: " + "  ".join([f"{k}:{v}" for k, v in reasons.items()]))

    # ──── 方向別 ────
    print(f"\n{'─' * 72}")
    print("  【方向別】")
    for d in ["UP_CROSS", "DOWN_CROSS"]:
        label = "🔴 BUY  (UP_CROSS)" if d == "UP_CROSS" else "🔵 SELL (DOWN_CROSS)"
        print_stats(label, trades[trades['direction'] == d])

    # ──── 時間帯別 ────
    print(f"\n{'─' * 72}")
    print("  【時間帯別】（UTC）")
    for sess in ["東京(0-8UTC)", "ロンドン(8-13UTC)", "NY(13-22UTC)", "深夜(22-24UTC)"]:
        print_stats(sess, trades[trades['session'] == sess])

    # ──── ADX別 ────
    print(f"\n{'─' * 72}")
    print("  【市場タイプ別（ADX）】")
    for zone in ["レンジ(<20)", "移行(20-25)", "トレンド(>25)"]:
        print_stats(zone, trades[trades['adx_zone'] == zone])

    # ──── RSI水準別 ────
    print(f"\n{'─' * 72}")
    print("  【RSI水準別】（シグナル時点）")
    trades['rsi_zone'] = pd.cut(trades['rsi'],
        bins=[0, 35, 45, 55, 65, 100],
        labels=["過売り(<35)", "弱め(35-45)", "中立(45-55)", "強め(55-65)", "過買い(>65)"])
    for zone in ["過売り(<35)", "弱め(35-45)", "中立(45-55)", "強め(55-65)", "過買い(>65)"]:
        t = trades[trades['rsi_zone'] == zone]
        if len(t) > 0:
            print_stats(zone, t)

    # ──── スコアギャップ別（Gemini代替フィルター） ────
    print(f"\n{'─' * 72}")
    print("  【スコアギャップ別（大→信号が強い）】")
    trades['score_gap'] = trades.apply(
        lambda r: abs(r['buy_score'] - r['sell_score']), axis=1)
    for gap_label, gap_min, gap_max in [
        ("スコア差2(最低)", 2, 2),
        ("スコア差3", 3, 3),
        ("スコア差4以上(強)", 4, 99),
    ]:
        t = trades[(trades['score_gap'] >= gap_min) & (trades['score_gap'] <= gap_max)]
        if len(t) > 0:
            print_stats(gap_label, t)

    # ──── 曜日別 ────
    print(f"\n{'─' * 72}")
    print("  【曜日別】")
    day_names = ["月", "火", "水", "木", "金", "土", "日"]
    trades['weekday'] = trades['entry_time'].apply(
        lambda t: day_names[t.weekday()] if hasattr(t, 'weekday') else "?")
    for d in ["月", "火", "水", "木", "金"]:
        t = trades[trades['weekday'] == d]
        if len(t) > 0:
            print_stats(d + "曜日", t)

    # ──── トレーリングあり vs なし比較 ────
    print(f"\n{'─' * 72}")
    print("  【トレーリングSL あり vs なし 比較】")
    trades_no_trail = simulate_trades(df, df_sig, use_trailing=False)
    print_stats("トレーリングあり", trades)
    print_stats("トレーリングなし", trades_no_trail)

    # ──── スコア差フィルター別シミュレーション ────
    print(f"\n{'─' * 72}")
    print("  【Gemini強化後を想定: スコア差フィルター別シミュレーション】")
    print("  （差4以上のみエントリー = Geminiが弱シグナルを却下した場合の想定）")
    df_sig_gap = df_sig.copy()
    df_sig_gap['gap'] = (df_sig_gap['buy_score'] - df_sig_gap['sell_score']).abs()
    for min_gap, label in [(2, "全シグナル(差2以上)"), (3, "差3以上"), (4, "差4以上★"), (5, "差5以上")]:
        filtered = df_sig_gap.copy()
        filtered.loc[filtered['gap'] < min_gap, 'crossover'] = None
        t = simulate_trades(df, filtered, use_trailing=True)
        print_stats(label, t)

    # ──── ①②フィルター比較 ────
    print(f"\n{'─' * 72}")
    print("  【①RSIフィルター + ②SELL閾値強化 比較】")
    print(f"  ①RSI<{RSI_FILTER_THRESHOLD:.0f}のシグナルをスキップ")
    print(f"  ②SELLはスコア差{SELL_MIN_GAP}以上でないとスキップ（BUYは差2以上でOK）")
    combos = [
        (False, False, "フィルターなし（現状）"),
        (True,  False, "①RSIフィルターのみ"),
        (False, True,  "②SELL閾値強化のみ"),
        (True,  True,  "①+② 組み合わせ★"),
    ]
    for rsi_f, sell_f, label in combos:
        filtered = apply_filters(df_sig, rsi_filter=rsi_f, sell_gap_filter=sell_f)
        t = simulate_trades(df, filtered, use_trailing=True)
        print_stats(label, t)

    # ──── ③ADXフィルター比較 ────
    print(f"\n{'─' * 72}")
    print("  【③ADXフィルター比較】")
    print(f"  ③ADX<閾値（レンジ相場）のシグナルをスキップ")
    adx_combos = [
        (False, False, False,  0,  "フィルターなし（現状）"),
        (True,  False, False,  0,  "①RSIのみ（採用済み）"),
        (False, False, True,  20,  "③ADX<20のみ"),
        (False, False, True,  25,  "③ADX<25のみ"),
        (True,  False, True,  20,  "①RSI + ③ADX<20"),
        (True,  False, True,  25,  "①RSI + ③ADX<25"),
    ]
    for rsi_f, sell_f, adx_f, adx_th, label in adx_combos:
        filtered = apply_filters(df_sig, rsi_filter=rsi_f, sell_gap_filter=sell_f,
                                 adx_filter=adx_f, adx_threshold=adx_th if adx_th > 0 else ADX_FILTER_THRESHOLD)
        t = simulate_trades(df, filtered, use_trailing=True)
        print_stats(label, t)

    # ──── ④時間帯フィルター比較 ────
    print(f"\n{'─' * 72}")
    print("  【④時間帯フィルター比較】（UTC基準）")
    print("  東京:0-8h  ロンドン:8-13h  NY:13-22h  深夜:22-24h")
    sess_combos = [
        (None,                      "全時間帯（現状）"),
        (["深夜"],                   "深夜スキップ"),
        (["ロンドン"],               "ロンドンスキップ"),
        (["NY"],                    "NYスキップ"),
        (["東京"],                   "東京スキップ"),
        (["深夜", "東京"],           "深夜+東京スキップ"),
        (["深夜", "ロンドン"],       "深夜+ロンドンスキップ"),
        (["深夜", "NY"],            "深夜+NYスキップ"),
    ]
    for skip, label in sess_combos:
        filtered = apply_filters(df_sig, rsi_filter=True, skip_sessions=skip)
        t = simulate_trades(df, filtered, use_trailing=True)
        print_stats(f"①RSI+{label}", t)

    # ──── 連敗分析 ────
    print(f"\n{'─' * 72}")
    print("  【連敗分析】")
    streak = 0
    max_streak = 0
    for _, r in trades.iterrows():
        if r['pnl'] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    print(f"  最大連敗数: {max_streak}連敗")

    # ──── 苦手・得意まとめ ────
    print(f"\n{'=' * 72}")
    print("  【まとめ: 得意な局面 vs 苦手な局面】")

    # セッション最高・最低
    sess_stats = trades.groupby('session')['pnl'].sum()
    best_sess = sess_stats.idxmax() if len(sess_stats) > 0 else "-"
    worst_sess = sess_stats.idxmin() if len(sess_stats) > 0 else "-"

    # ADX最高・最低
    adx_stats = trades.groupby('adx_zone')['pnl'].sum()
    best_adx = adx_stats.idxmax() if len(adx_stats) > 0 else "-"
    worst_adx = adx_stats.idxmin() if len(adx_stats) > 0 else "-"

    # 方向
    dir_stats = trades.groupby('direction').apply(
        lambda x: len(x[x['pnl']>0])/len(x)*100)
    best_dir = dir_stats.idxmax() if len(dir_stats) > 0 else "-"

    print(f"  ✅ 得意: {best_sess} | {best_adx} | {'BUY' if best_dir=='UP_CROSS' else 'SELL'}方向")
    print(f"  ❌ 苦手: {worst_sess} | {worst_adx}")
    print()

    # ──── 全トレード一覧（最新20件） ────
    print(f"\n{'─' * 72}")
    print("  【直近20件のトレード】")
    print(f"  {'時刻':<20} {'方向':<12} {'エントリー':>8} {'決済':>8} {'損益':>8} {'理由':<8} {'ADX':>6}")
    for _, r in trades.tail(20).iterrows():
        direction_label = "🔴 BUY" if r['direction'] == "UP_CROSS" else "🔵 SELL"
        pnl_str = f"+{r['pnl']:.1f}" if r['pnl'] > 0 else f"{r['pnl']:.1f}"
        t_str = str(r['entry_time'])[:16]
        print(f"  {t_str:<20} {direction_label:<12} {r['entry_price']:>8.2f} {r['exit_price']:>8.2f} {pnl_str:>7}  {r['exit_reason']:<8} {r['adx']:>5.1f}")

    # ──── チャネルブレイク決済 比較 ────
    print(f"\n{'=' * 72}")
    print("  【チャネルブレイク決済 比較】（①RSIフィルター適用）")
    print("  エントリーはCOMPOSITEシグナル、決済方法のみ変更")
    print(f"  {'モード':<30} {'期間':>6}  結果")
    print("─" * 72)

    df_sig_rsi = apply_filters(df_sig, rsi_filter=True)

    # ベースライン
    t_base = simulate_trades(df, df_sig_rsi, use_trailing=True)
    print_stats("①現状（SL+トレーリング）      ", t_base)
    print()

    for period in [20, 30, 50]:
        for mode, label in [
            ("replace_tp",        f"②SL残し+チャネルTP              (期間{period})"),
            ("breakeven_channel",  f"⑤BE移動後チャネルTP             (期間{period})"),
            ("with_trailing",      f"③トレーリング+チャネル併用      (期間{period})"),
            ("only",               f"④チャネルのみ（SLなし）         (期間{period})"),
        ]:
            t = simulate_trades_channel(
                df, df_sig_rsi,
                channel_mode=mode, channel_period=period, channel_std=1.5,
                sl=SL_PIPS, tp=TP_PIPS,
                trail_trigger=TRAIL_TRIGGER, trail_width=TRAIL_WIDTH,
            )
            if len(t) > 0:
                wins = t[t['pnl'] > 0]
                losses = t[t['pnl'] <= 0]
                wr = len(wins) / len(t) * 100
                total = t['pnl'].sum()
                avg_w = wins['pnl'].mean() if len(wins) > 0 else 0
                avg_l = losses['pnl'].mean() if len(losses) > 0 else 0
                rr = abs(avg_w / avg_l) if avg_l != 0 else 0
                reasons = t['exit_reason'].value_counts().to_dict()
                reason_str = " ".join([f"{k}:{v}" for k, v in reasons.items()])
                bar_w = int(wr / 5)
                bar = "█" * bar_w + "░" * (20 - bar_w)
                print(f"  {label:<35} [{bar}] {wr:5.1f}%  取引:{len(t):3d}  合計:{total:+7.1f}$/oz  "
                      f"平均勝:{avg_w:+.1f} 平均負:{avg_l:+.1f}  RR:{rr:.2f}  [{reason_str}]")
            else:
                print(f"  {label:<35} データなし")
        print()

    # ──── ⑥動的SL比較 ────
    print(f"\n{'=' * 72}")
    print("  【⑥動的SL比較】チャネル下辺/上辺 ± バッファ でSLを毎足追随")
    print("  ※SLは有利方向にしか動かない（逆行時はその時点のSLで決済）")
    print("─" * 72)

    # ベースライン再掲
    print_stats("①現状（固定SL5$+トレーリング）  ", t_base)
    print_stats("②チャネルTP期間30（最良）        ",
                simulate_trades_channel(df, df_sig_rsi, channel_mode="replace_tp",
                                        channel_period=30, sl=SL_PIPS, tp=TP_PIPS,
                                        trail_trigger=TRAIL_TRIGGER, trail_width=TRAIL_WIDTH))
    print()

    for period in [20, 30, 50]:
        for buf in [2.0, 5.0, 8.0]:
            for use_tp, tp_label in [(False, "TPなし"), (True, f"TP{int(TP_PIPS)}$")]:
                label = f"⑥動的SL buf{int(buf)}$ {tp_label} (期間{period})"
                t = simulate_trades_dynamic_sl(
                    df, df_sig_rsi,
                    channel_period=period, channel_std=1.5,
                    sl_buffer=buf, use_fixed_tp=use_tp, tp=TP_PIPS,
                )
                if len(t) > 0:
                    wins = t[t['pnl'] > 0]
                    losses = t[t['pnl'] <= 0]
                    wr = len(wins) / len(t) * 100
                    total = t['pnl'].sum()
                    avg_w = wins['pnl'].mean() if len(wins) > 0 else 0
                    avg_l = losses['pnl'].mean() if len(losses) > 0 else 0
                    rr = abs(avg_w / avg_l) if avg_l != 0 else 0
                    reasons = t['exit_reason'].value_counts().to_dict()
                    reason_str = " ".join([f"{k}:{v}" for k, v in reasons.items()])
                    bar_w = int(wr / 5)
                    bar = "█" * bar_w + "░" * (20 - bar_w)
                    print(f"  {label:<40} [{bar}] {wr:5.1f}%  取引:{len(t):3d}  "
                          f"合計:{total:+7.1f}$/oz  平均勝:{avg_w:+.1f} 平均負:{avg_l:+.1f}  "
                          f"RR:{rr:.2f}  [{reason_str}]")
        print()

    # ──── 1年テスト（1h足） ────
    print(f"\n{'=' * 72}")
    print("  【1年バックテスト】1時間足 × 過去365日")
    print("  ※15分足は60日上限のため1時間足を使用。ロジックは同一。")
    print("=" * 72)
    df1y = yf.download("GC=F", period="365d", interval="1h", progress=False)
    if isinstance(df1y.columns, pd.MultiIndex):
        df1y.columns = df1y.columns.droplevel(1)
    print(f"  取得: {len(df1y)}本  {df1y.index[0]} 〜 {df1y.index[-1]}")
    df_sig1y = compute_composite(df1y)
    cross1y = df_sig1y['crossover'].notna().sum()
    print(f"  総シグナル数: {cross1y}件\n")

    # 1h足はSL/TPを広めに（1時間足はボラが大きい）
    SL1H, TP1H, TR1H, TW1H = 8.0, 25.0, 7.0, 5.0
    print(f"  SL:{SL1H}$/oz  TP:{TP1H}$/oz  トレイル開始:{TR1H}$/oz  幅:{TW1H}$/oz")
    print()

    combos1y = [
        (False, False, "フィルターなし（現状）"),
        (True,  True,  "①+② 組み合わせ★"),
    ]
    for rsi_f, sell_f, label in combos1y:
        filtered1y = apply_filters(df_sig1y, rsi_filter=rsi_f, sell_gap_filter=sell_f)
        t1y = simulate_trades(df1y, filtered1y, sl=SL1H, tp=TP1H,
                              trail_trigger=TR1H, trail_width=TW1H, use_trailing=True)
        if len(t1y) > 0:
            t1y['session'] = t1y['entry_time'].apply(get_session)
            t1y['adx_zone'] = t1y['adx'].apply(get_adx_zone)
            print_stats(label, t1y)

    # フィルターありで詳細分析
    print()
    filtered1y_best = apply_filters(df_sig1y, rsi_filter=True, sell_gap_filter=True)
    t1y_best = simulate_trades(df1y, filtered1y_best, sl=SL1H, tp=TP1H,
                               trail_trigger=TR1H, trail_width=TW1H, use_trailing=True)
    if len(t1y_best) > 0:
        t1y_best['session'] = t1y_best['entry_time'].apply(get_session)
        t1y_best['adx_zone'] = t1y_best['adx'].apply(get_adx_zone)
        t1y_best['weekday'] = t1y_best['entry_time'].apply(
            lambda t: ["月","火","水","木","金","土","日"][t.weekday()] if hasattr(t,'weekday') else "?")
        max_dd1y = calc_max_drawdown(t1y_best)
        wins1y = t1y_best[t1y_best['pnl'] > 0]
        losses1y = t1y_best[t1y_best['pnl'] <= 0]
        print(f"  ─ ①+②フィルターあり 詳細 ─")
        print(f"  最大DD: {max_dd1y:.1f}$/oz  平均利益:{wins1y['pnl'].mean():+.1f}  平均損失:{losses1y['pnl'].mean():+.1f}")
        print()
        print("  時間帯別:")
        for sess in ["東京(0-8UTC)", "ロンドン(8-13UTC)", "NY(13-22UTC)", "深夜(22-24UTC)"]:
            t = t1y_best[t1y_best['session'] == sess]
            if len(t) > 0: print_stats(sess, t)
        print()
        print("  ADX別:")
        for zone in ["レンジ(<20)", "移行(20-25)", "トレンド(>25)"]:
            t = t1y_best[t1y_best['adx_zone'] == zone]
            if len(t) > 0: print_stats(zone, t)
        print()
        print("  方向別:")
        for d, lbl in [("UP_CROSS","🔴 BUY"), ("DOWN_CROSS","🔵 SELL")]:
            t = t1y_best[t1y_best['direction'] == d]
            if len(t) > 0: print_stats(lbl, t)
        print()
        print("  曜日別:")
        for d in ["月","火","水","木","金"]:
            t = t1y_best[t1y_best['weekday'] == d]
            if len(t) > 0: print_stats(d+"曜日", t)

    # ──── 1時間足 × 動的SL比較 ────
    print(f"\n{'=' * 72}")
    print("  【1時間足 × ⑥動的SL比較】（①RSIフィルター適用）")
    print("  ※1時間足はボラが大きいためSL/TPパラメータを広め設定")
    print("─" * 72)

    SL1H, TP1H, TR1H, TW1H = 8.0, 25.0, 7.0, 5.0
    df_sig1y_rsi = apply_filters(df_sig1y, rsi_filter=True)

    # ベースライン（1h足）
    t1y_base = simulate_trades(df1y, df_sig1y_rsi, sl=SL1H, tp=TP1H,
                               trail_trigger=TR1H, trail_width=TW1H, use_trailing=True)
    print_stats("①現状1h（SL8$+トレーリング）    ", t1y_base)
    print()

    for period in [20, 30, 50]:
        for buf in [3.0, 6.0, 10.0]:
            for use_tp, tp_label in [(False, "TPなし"), (True, f"TP{int(TP1H)}$")]:
                label = f"⑥動的SL buf{int(buf)}$ {tp_label} (期間{period})"
                t = simulate_trades_dynamic_sl(
                    df1y, df_sig1y_rsi,
                    channel_period=period, channel_std=1.5,
                    sl_buffer=buf, use_fixed_tp=use_tp, tp=TP1H,
                )
                if len(t) > 0:
                    wins = t[t['pnl'] > 0]
                    losses = t[t['pnl'] <= 0]
                    wr = len(wins) / len(t) * 100
                    total = t['pnl'].sum()
                    avg_w = wins['pnl'].mean() if len(wins) > 0 else 0
                    avg_l = losses['pnl'].mean() if len(losses) > 0 else 0
                    rr = abs(avg_w / avg_l) if avg_l != 0 else 0
                    reasons = t['exit_reason'].value_counts().to_dict()
                    reason_str = " ".join([f"{k}:{v}" for k, v in reasons.items()])
                    bar_w = int(wr / 5)
                    bar = "█" * bar_w + "░" * (20 - bar_w)
                    print(f"  {label:<40} [{bar}] {wr:5.1f}%  取引:{len(t):3d}  "
                          f"合計:{total:+8.1f}$/oz  平均勝:{avg_w:+.1f} 平均負:{avg_l:+.1f}  "
                          f"RR:{rr:.2f}  [{reason_str}]")
        print()

    # ──── R/Sブレイク分析（重複確認） ────
    print(f"\n{'=' * 72}")
    print("  【R/Sブレイク検出 × 7指標 重複分析】")
    print("=" * 72)

    # ブレイク検出されたシグナルを数える
    breakout_signals = df_sig[df_sig['is_breakout'] | df_sig['is_breakdown']].copy()
    composite_signals = df_sig[df_sig['crossover'].notna()].copy()

    # ブレイク検出のうち、7指標シグナルと重複している割合
    breakout_with_composite = breakout_signals[breakout_signals['crossover'].notna()]
    breakout_without_composite = breakout_signals[breakout_signals['crossover'].isna()]

    print(f"\n  📊 シグナル検出数:")
    print(f"    7指標スコア3以上: {len(composite_signals)}件")
    print(f"    R/Sブレイク検出: {len(breakout_signals)}件")
    print(f"    両方で検出: {len(breakout_with_composite)}件（ブレイクの{len(breakout_with_composite)/len(breakout_signals)*100:.1f}%）")
    print(f"    ブレイクのみ: {len(breakout_without_composite)}件（ブレイクの{len(breakout_without_composite)/len(breakout_signals)*100:.1f}%）")
    print(f"\n  💡 解釈:")
    if len(breakout_without_composite) == 0:
        print(f"    ⚠️  ブレイク検出は7指標に完全に含まれています")
        print(f"       → ブレイク検出は新しい情報を追加していない可能性")
    else:
        print(f"    ✅ ブレイク検出のうち {len(breakout_without_composite)} 件は7指標にはない")
        print(f"       → ブレイク検出は新しい信号を補捉している")

    # ──── R/Sブレイク比較テスト ────
    print(f"\n{'=' * 72}")
    print("  【R/Sブレイク検出 比較テスト】")
    print("  期間: 過去60日（15分足）")
    print("=" * 72)

    breakout_scenarios = [
        (False, False, False, "none", "現在（RSIフィルターのみ）★"),
        (False, False, True, "single", "①ブレイク単独でエントリー"),
        (False, False, True, "and", "②ブレイク AND 7指標（両方）"),
        (False, False, True, "or", "③ブレイク OR 7指標（どちらか）"),
        (True, False, True, "single", "④ブレイク単独 + RSIフィルター"),
        (True, False, True, "and", "⑤ブレイク AND 7指標 + RSIフィルター"),
    ]

    breakout_results = []
    for rsi_f, sell_f, breakout_f, b_mode, label in breakout_scenarios:
        filtered = apply_filters(df_sig, rsi_filter=rsi_f, sell_gap_filter=sell_f,
                                breakout_filter=breakout_f, breakout_mode=b_mode)
        t = simulate_trades(df, filtered, use_trailing=True)

        if len(t) > 0:
            wins = t[t['pnl'] > 0]
            losses = t[t['pnl'] <= 0]
            win_rate = len(wins) / len(t) * 100 if len(t) > 0 else 0
            total_pnl = t['pnl'].sum()
            avg_pnl = t['pnl'].mean()
            max_dd = calc_max_drawdown(t)

            breakout_results.append({
                'scenario': label,
                'win_rate': win_rate,
                'num_trades': len(t),
                'total_pnl': total_pnl,
                'avg_pnl': avg_pnl,
                'max_drawdown': max_dd,
            })
            print_stats(label, t)
        else:
            print(f"  {label:<40}: シグナルなし")
            breakout_results.append({
                'scenario': label,
                'win_rate': 0,
                'num_trades': 0,
                'total_pnl': 0,
                'avg_pnl': 0,
                'max_drawdown': 0,
            })

    # 結果をCSVで保存
    print(f"\n{'─' * 72}")
    print("  【結果をCSV保存中...】")
    import csv
    results_df = pd.DataFrame(breakout_results)
    csv_filename = "backtest_results_breakout.csv"
    results_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    print(f"  ✅ {csv_filename} に保存しました")
    print(f"  結果サマリー:")
    for _, row in results_df.iterrows():
        print(f"    {row['scenario']:<40} | 勝率:{row['win_rate']:5.1f}% | 取引:{row['num_trades']:3.0f} | P&L:{row['total_pnl']:+7.1f}$/oz | DD:{row['max_drawdown']:+7.1f}$/oz")

    # ──── 決済タイミング最適化テスト（ADX適応的TP） ────
    print(f"\n{'=' * 72}")
    print("  【決済タイミング最適化：ADX適応的TP】")
    print("  トレンド時（ADX>25）：対面側R/Sラインまで保有")
    print("  レンジ時（ADX<25）：固定TP（15$/oz）で決済")
    print("=" * 72)

    # RSIフィルター版で比較
    df_sig_rsi = apply_filters(df_sig, rsi_filter=True, sell_gap_filter=False)

    tp_scenarios = [
        (False, "【現在】固定TP（15$/oz）"),
        (True,  "【改善】ADX適応的TP（トレンド/レンジ自動切替）"),
    ]

    tp_results = []
    for use_adaptive, label in tp_scenarios:
        t = simulate_trades(df, df_sig_rsi, use_trailing=True,
                          use_adx_adaptive_tp=use_adaptive, adx_trend_threshold=25.0)

        if len(t) > 0:
            wins = t[t['pnl'] > 0]
            losses = t[t['pnl'] <= 0]
            win_rate = len(wins) / len(t) * 100 if len(t) > 0 else 0
            total_pnl = t['pnl'].sum()
            avg_pnl = t['pnl'].mean()
            max_dd = calc_max_drawdown(t)

            tp_results.append({
                'scenario': label,
                'win_rate': win_rate,
                'num_trades': len(t),
                'total_pnl': total_pnl,
                'avg_pnl': avg_pnl,
                'max_drawdown': max_dd,
            })
            print_stats(label, t)
        else:
            print(f"  {label:<50}: シグナルなし")

    # 結果をCSVで保存
    print(f"\n{'─' * 72}")
    print("  【決済タイミング最適化結果をCSV保存中...】")
    tp_results_df = pd.DataFrame(tp_results)
    csv_filename2 = "backtest_results_tp_optimization.csv"
    tp_results_df.to_csv(csv_filename2, index=False, encoding='utf-8-sig')
    print(f"  ✅ {csv_filename2} に保存しました")
    print(f"  結果サマリー:")
    for _, row in tp_results_df.iterrows():
        print(f"    {row['scenario']:<50} | 勝率:{row['win_rate']:5.1f}% | 取引:{row['num_trades']:3.0f} | P&L:{row['total_pnl']:+7.1f}$/oz | DD:{row['max_drawdown']:+7.1f}$/oz")

    # ──── 決済タイミング最適化テスト（ADX適応的ドテン） ────
    print(f"\n{'=' * 72}")
    print("  【決済タイミング最適化テスト】")
    print("  ADX適応的ドテン無効化の効果測定")
    print("=" * 72)

    df_sig_rsi_base = apply_filters(df_sig, rsi_filter=True, sell_gap_filter=False)

    exit_timing_results = []

    # シナリオ1：現在（常にドテン）
    print(f"\n  ① 【現在】常にドテン + 固定TP（15$/oz）")
    t_curr = simulate_trades(df, df_sig_rsi_base, use_trailing=True)
    if len(t_curr) > 0:
        wins = t_curr[t_curr['pnl'] > 0]
        win_rate = len(wins) / len(t_curr) * 100
        total_pnl = t_curr['pnl'].sum()
        max_dd = calc_max_drawdown(t_curr)

        exit_timing_results.append({
            'scenario': '① 【現在】常にドテン',
            'win_rate': win_rate,
            'num_trades': len(t_curr),
            'total_pnl': total_pnl,
            'avg_pnl': t_curr['pnl'].mean(),
            'max_drawdown': max_dd,
        })
        print_stats("常にドテン", t_curr)

    # シナリオ2：ADX適応的ドテン無効化
    print(f"\n  ② 【改善】ADX適応的ドテン無効化 + 固定TP（15$/oz）")
    df_sig_rsi_adx = apply_adx_adaptive_flip(df_sig_rsi_base, adx_threshold=25.0)
    t_adx = simulate_trades(df, df_sig_rsi_adx, use_trailing=True)
    if len(t_adx) > 0:
        wins = t_adx[t_adx['pnl'] > 0]
        win_rate = len(wins) / len(t_adx) * 100
        total_pnl = t_adx['pnl'].sum()
        max_dd = calc_max_drawdown(t_adx)

        exit_timing_results.append({
            'scenario': '② 【改善】ADX適応的ドテン無効化',
            'win_rate': win_rate,
            'num_trades': len(t_adx),
            'total_pnl': total_pnl,
            'avg_pnl': t_adx['pnl'].mean(),
            'max_drawdown': max_dd,
        })
        print_stats("ADX適応的ドテン無効化", t_adx)

    # 結果をCSV保存
    print(f"\n{'─' * 72}")
    print("  【結果をCSV保存中...】")
    exit_timing_df = pd.DataFrame(exit_timing_results)
    csv_exit = "backtest_results_exit_timing.csv"
    exit_timing_df.to_csv(csv_exit, index=False, encoding='utf-8-sig')
    print(f"  ✅ {csv_exit} に保存しました")
    print(f"\n  結果サマリー:")
    for _, row in exit_timing_df.iterrows():
        improvement = ""
        if row['scenario'].startswith('②'):
            if len(exit_timing_results) > 0:
                prev_pnl = exit_timing_results[0]['total_pnl']
                diff = row['total_pnl'] - prev_pnl
                improvement = f"  (前比: {diff:+.1f}$/oz)"
        print(f"    {row['scenario']:<40} | 勝率:{row['win_rate']:5.1f}% | 取引:{row['num_trades']:3.0f} | P&L:{row['total_pnl']:+7.1f}$/oz{improvement}")

    # ──── ⑤b クロス転換決済戦略 ────
    print(f"\n{'=' * 72}")
    print("  【⑤b クロス転換決済戦略】（トレーリングなし・TP無効）")
    print("  エントリー: クロス発生  /  決済: 反対クロス or SL(-5$)")
    print("─" * 72)

    df_sig_flip = apply_filters(df_sig, rsi_filter=True)

    flip_scenarios = [
        ("①RSI+トレーリング（現状）",  True,   SL_PIPS, TP_PIPS),
        ("クロス転換決済 SL5$",        False,  5.0,     999.0),
        ("クロス転換決済 SL8$",        False,  8.0,     999.0),
        ("クロス転換決済 SL10$",       False,  10.0,    999.0),
        ("クロス転換決済 SLなし",       False,  999.0,   999.0),
    ]

    flip_base = None
    for lbl_f, use_tr, sl_f, tp_f in flip_scenarios:
        t_f = simulate_trades(df, df_sig_flip, sl=sl_f, tp=tp_f, use_trailing=use_tr)
        if len(t_f) == 0: continue
        wins_f   = t_f[t_f['pnl'] > 0]
        losses_f = t_f[t_f['pnl'] <= 0]
        wr_f     = len(wins_f) / len(t_f) * 100
        total_f  = t_f['pnl'].sum()
        avg_w_f  = wins_f['pnl'].mean()   if len(wins_f)   > 0 else 0
        avg_l_f  = losses_f['pnl'].mean() if len(losses_f) > 0 else 0
        rr_f     = abs(avg_w_f / avg_l_f) if avg_l_f != 0 else 0
        dd_f     = calc_max_drawdown(t_f)
        if flip_base is None:
            flip_base = total_f
        diff_f   = total_f - flip_base
        bar_w    = int(wr_f / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
        mark     = "✅" if total_f > flip_base and lbl_f != flip_scenarios[0][0] else ("──" if lbl_f == flip_scenarios[0][0] else "❌")
        # 決済理由の内訳
        reasons_f = t_f['exit_reason'].value_counts().to_dict() if 'exit_reason' in t_f.columns else {}
        reason_str = " ".join([f"{k}:{v}" for k, v in reasons_f.items()])
        print(f"  {mark} {lbl_f:<30} [{bar}] {wr_f:5.1f}%  取引:{len(t_f):3d}  "
              f"合計:{total_f:+8.1f}$/oz({diff_f:+.1f})  "
              f"平均勝:{avg_w_f:+.1f} 平均負:{avg_l_f:+.1f}  RR:{rr_f:.2f}  DD:{dd_f:+.1f}")

    # クロス転換決済の詳細（SL5$版）
    t_flip = simulate_trades(df, df_sig_flip, sl=5.0, tp=999.0, use_trailing=False)
    if len(t_flip) > 0:
        t_flip['hold_bars'] = (pd.to_datetime(t_flip['exit_time']) - pd.to_datetime(t_flip['entry_time'])).dt.total_seconds() / (15*60)
        t_flip['direction_lbl'] = t_flip['direction'].map({'UP_CROSS':'BUY','DOWN_CROSS':'SELL'})
        wins_flip = t_flip[t_flip['pnl'] > 0]
        losses_flip = t_flip[t_flip['pnl'] <= 0]
        print(f"\n  ─ クロス転換決済 SL5$ 詳細 ─")
        print(f"  平均保有バー数: 全体{t_flip['hold_bars'].mean():.1f}本  "
              f"勝ち{wins_flip['hold_bars'].mean():.1f}本  負け{losses_flip['hold_bars'].mean():.1f}本")
        print(f"  最大利益: {t_flip['pnl'].max():+.1f}$/oz  最大損失: {t_flip['pnl'].min():+.1f}$/oz")
        for d, lbl in [("UP_CROSS","BUY"), ("DOWN_CROSS","SELL")]:
            td = t_flip[t_flip['direction'] == d]
            if len(td) == 0: continue
            wd = td[td['pnl'] > 0]
            print(f"  {lbl}: 勝率{len(wd)/len(td)*100:.1f}%  取引{len(td)}件  "
                  f"合計{td['pnl'].sum():+.1f}$/oz  平均保有{td['hold_bars'].mean():.1f}本")

    # ──── ⑤c クロス転換ハイブリッド 全比較 ────
    print(f"\n{'=' * 72}")
    print("  【⑤c クロス転換ハイブリッド 全比較】")
    print("  BE=ブレイクイーブン移動後クロス保有 / ADX=ADX≥25のみ / Gap=スコア差≥4")
    print("─" * 72)

    df_sig_h = apply_filters(df_sig, rsi_filter=True)

    # (label, be_trigger, adx_min, min_score_gap)
    hybrid_scenarios = [
        # ── ベースライン ──
        ("①現状（トレーリング）",          None,  None, None,  True ),
        ("純粋クロス転換 SL5$",            None,  None, None,  False),
        # ── 単体ハイブリッド ──
        ("A. BE後クロス保有（trigger4$）",  4.0,   None, None,  False),
        ("A. BE後クロス保有（trigger5$）",  5.0,   None, None,  False),
        ("A. BE後クロス保有（trigger6$）",  6.0,   None, None,  False),
        ("B. ADX≥25のみ クロス転換",        None,  25.0, None,  False),
        ("B. ADX≥20のみ クロス転換",        None,  20.0, None,  False),
        ("C. スコア差≥3 クロス転換",        None,  None, 3,     False),
        ("C. スコア差≥4 クロス転換",        None,  None, 4,     False),
        # ── 2段ハイブリッド ──
        ("A+B. BE後+ADX≥25",              4.0,   25.0, None,  False),
        ("A+B. BE後+ADX≥20",              4.0,   20.0, None,  False),
        ("A+C. BE後+スコア差≥3",           4.0,   None, 3,     False),
        ("A+C. BE後+スコア差≥4",           4.0,   None, 4,     False),
        ("B+C. ADX≥25+スコア差≥3",        None,  25.0, 3,     False),
        ("B+C. ADX≥25+スコア差≥4",        None,  25.0, 4,     False),
        # ── 3段ハイブリッド ──
        ("A+B+C. BE+ADX≥25+差≥3",        4.0,   25.0, 3,     False),
        ("A+B+C. BE+ADX≥25+差≥4",        4.0,   25.0, 4,     False),
    ]

    # ベースライン値を取得
    t_base_h = simulate_trades(df, df_sig_h, use_trailing=True)
    base_h_total = t_base_h['pnl'].sum()

    print(f"\n  {'ラベル':<34} {'勝率':>6} {'取引':>5} {'合計P&L':>10} {'差':>7} {'DD':>7} {'RR':>5} {'決済内訳'}")
    print(f"  {'─'*34} {'─'*6} {'─'*5} {'─'*10} {'─'*7} {'─'*7} {'─'*5}")

    best_total = base_h_total
    results_h = []
    for lbl_h, be_t, adx_t, gap_t, use_tr in hybrid_scenarios:
        if use_tr:
            t_h = t_base_h
        else:
            t_h = simulate_trades_hybrid_flip(
                df, df_sig_h,
                sl=SL_PIPS, be_trigger=be_t,
                adx_min=adx_t, min_score_gap=gap_t,
            )
        if len(t_h) == 0:
            print(f"  ❓ {lbl_h:<34}: シグナルなし")
            continue
        wins_h   = t_h[t_h['pnl'] > 0]
        losses_h = t_h[t_h['pnl'] <= 0]
        wr_h     = len(wins_h) / len(t_h) * 100
        total_h  = t_h['pnl'].sum()
        avg_w_h  = wins_h['pnl'].mean()   if len(wins_h)   > 0 else 0
        avg_l_h  = losses_h['pnl'].mean() if len(losses_h) > 0 else 0
        rr_h     = abs(avg_w_h / avg_l_h) if avg_l_h != 0 else 0
        dd_h     = calc_max_drawdown(t_h)
        diff_h   = total_h - base_h_total
        reasons_h = t_h['exit_reason'].value_counts().to_dict() if 'exit_reason' in t_h.columns else {}
        reason_str_h = " ".join([f"{k}:{v}" for k, v in sorted(reasons_h.items())])
        mark_h   = "✅" if total_h > base_h_total else ("──" if use_tr else "❌")
        if total_h > best_total:
            best_total = total_h
            mark_h = "🏆"
        print(f"  {mark_h} {lbl_h:<34} {wr_h:6.1f}% {len(t_h):5d} {total_h:+10.1f} {diff_h:+7.1f} {dd_h:+7.1f} {rr_h:5.2f}  {reason_str_h}")
        results_h.append({
            'label': lbl_h, 'win_rate': round(wr_h,1), 'num_trades': len(t_h),
            'total': round(total_h,2), 'diff': round(diff_h,2),
            'avg_win': round(avg_w_h,2), 'avg_loss': round(avg_l_h,2),
            'rr': round(rr_h,2), 'max_dd': round(dd_h,2),
        })

    winners_h = [r for r in results_h if r['diff'] > 0]
    print(f"\n  改善あり: {len(winners_h)}/{len(results_h)-1}件（ベースライン除く）")
    if winners_h:
        best_h = max(winners_h, key=lambda x: x['total'])
        print(f"  🏆 総合最良: {best_h['label']}")
        print(f"     勝率:{best_h['win_rate']}%  取引:{best_h['num_trades']}  "
              f"合計:{best_h['total']:+.1f}$/oz  ベース差:{best_h['diff']:+.1f}$/oz  "
              f"RR:{best_h['rr']:.2f}  DD:{best_h['max_dd']:+.1f}$/oz")
        print(f"\n  📊 現状 vs 最良比較:")
        print(f"     現状:  勝率65.8%  取引158  合計+255.8$/oz  DD-22.5$/oz")
        print(f"     最良:  勝率{best_h['win_rate']}%  取引{best_h['num_trades']}  "
              f"合計{best_h['total']:+.1f}$/oz  DD{best_h['max_dd']:+.1f}$/oz")

    # ──── ⑥a トレンド/レンジ別勝率 ────
    print(f"\n{'=' * 72}")
    print("  【⑥a トレンド/レンジ別 詳細分析】（①RSIフィルター適用）")
    print("─" * 72)

    t_rsi = simulate_trades(df, apply_filters(df_sig, rsi_filter=True), use_trailing=True)
    t_rsi['adx_zone'] = t_rsi['adx'].apply(get_adx_zone)
    t_rsi['direction_lbl'] = t_rsi['direction'].map({'UP_CROSS':'BUY','DOWN_CROSS':'SELL'})

    zones = [
        ("レンジ(<20)",   "レンジ(<20)"),
        ("移行(20-25)",   "移行(20-25)"),
        ("トレンド(>25)", "トレンド(>25)"),
    ]
    dirs = [("BUY","BUY"), ("SELL","SELL")]

    print(f"\n  {'区分':<20}  {'勝率':>6}  {'取引':>5}  {'合計P&L':>10}  {'平均勝':>7}  {'平均負':>7}  {'RR':>5}")
    print(f"  {'─'*20}  {'─'*6}  {'─'*5}  {'─'*10}  {'─'*7}  {'─'*7}  {'─'*5}")

    for zone_key, zone_lbl in zones:
        tz = t_rsi[t_rsi['adx_zone'] == zone_key]
        if len(tz) == 0: continue
        w = tz[tz['pnl'] > 0]; l = tz[tz['pnl'] <= 0]
        wr = len(w)/len(tz)*100
        tot = tz['pnl'].sum()
        aw = w['pnl'].mean() if len(w)>0 else 0
        al = l['pnl'].mean() if len(l)>0 else 0
        rr = abs(aw/al) if al!=0 else 0
        mark = "✅" if wr >= 65 else ("⚠️ " if wr >= 55 else "❌")
        print(f"  {mark}{zone_lbl:<18}  {wr:6.1f}%  {len(tz):5d}  {tot:+10.1f}  {aw:+7.2f}  {al:+7.2f}  {rr:5.2f}")

        for dir_key, dir_lbl in dirs:
            td = tz[tz['direction_lbl'] == dir_key]
            if len(td) == 0: continue
            wd = td[td['pnl'] > 0]; ld = td[td['pnl'] <= 0]
            wrd = len(wd)/len(td)*100
            totd = td['pnl'].sum()
            awd = wd['pnl'].mean() if len(wd)>0 else 0
            ald = ld['pnl'].mean() if len(ld)>0 else 0
            rrd = abs(awd/ald) if ald!=0 else 0
            markd = "  ✅" if wrd >= 65 else ("  ⚠️ " if wrd >= 55 else "  ❌")
            print(f"  {markd}  └{dir_lbl} ({zone_lbl}){'':<6}  {wrd:6.1f}%  {len(td):5d}  {totd:+10.1f}  {awd:+7.2f}  {ald:+7.2f}  {rrd:5.2f}")

    print(f"\n  ── 方向別まとめ ──")
    for dir_key, dir_lbl in dirs:
        td = t_rsi[t_rsi['direction_lbl'] == dir_key]
        if len(td) == 0: continue
        wd = td[td['pnl'] > 0]; ld = td[td['pnl'] <= 0]
        wrd = len(wd)/len(td)*100
        print(f"  {dir_lbl:<6}  勝率:{wrd:5.1f}%  取引:{len(td):3d}  合計:{td['pnl'].sum():+8.1f}$/oz  "
              f"平均勝:{wd['pnl'].mean() if len(wd)>0 else 0:+.2f}  平均負:{ld['pnl'].mean() if len(ld)>0 else 0:+.2f}")

    print(f"\n  ── 解釈 ──")
    rng = t_rsi[t_rsi['adx_zone']=="レンジ(<20)"]
    trd = t_rsi[t_rsi['adx_zone']=="トレンド(>25)"]
    if len(rng)>0 and len(trd)>0:
        wr_rng = len(rng[rng['pnl']>0])/len(rng)*100
        wr_trd = len(trd[trd['pnl']>0])/len(trd)*100
        print(f"  レンジ勝率: {wr_rng:.1f}%  /  トレンド勝率: {wr_trd:.1f}%  →  差: {wr_trd-wr_rng:+.1f}pt")
        if wr_trd > wr_rng + 5:
            print(f"  → トレンド相場が得意。ADXフィルターで絞ると勝率上がるが取引数が減る。")
        elif wr_rng > wr_trd + 5:
            print(f"  → レンジ相場が得意。逆張り的な動きに強い可能性。")
        else:
            print(f"  → トレンド/レンジで大きな差なし。ADXフィルターの効果は限定的。")

    # ──── ⑥a-2 レンジ相場BUYスキップ ────
    print(f"\n{'─' * 72}")
    print("  ─ レンジ相場(ADX<20)のBUYをスキップ ─")
    print("  レンジ時BUY勝率50%=コイントス → スキップして損失を減らす")

    _sig_rsi = apply_filters(df_sig, rsi_filter=True)
    _base_total_rb = simulate_trades(df, _sig_rsi, use_trailing=True)['pnl'].sum()

    _sig_skip20 = _sig_rsi[~((_sig_rsi['adx'] < 20) & (_sig_rsi['crossover'] == 'UP_CROSS'))].copy()
    _sig_skip25 = _sig_rsi[~((_sig_rsi['adx'] < 25) & (_sig_rsi['crossover'] == 'UP_CROSS'))].copy()

    scenarios_rb = [
        ("①RSI（ベースライン）",              _sig_rsi),
        ("①RSI + レンジBUYスキップ(ADX<20)",  _sig_skip20),
        ("①RSI + レンジBUYスキップ(ADX<25)",  _sig_skip25),
    ]

    for lbl_rb, filtered_rb in scenarios_rb:
        t_rb = simulate_trades(df, filtered_rb, use_trailing=True)
        if len(t_rb) == 0: continue
        wins_rb   = t_rb[t_rb['pnl'] > 0]
        losses_rb = t_rb[t_rb['pnl'] <= 0]
        wr_rb     = len(wins_rb) / len(t_rb) * 100
        total_rb  = t_rb['pnl'].sum()
        avg_w_rb  = wins_rb['pnl'].mean()   if len(wins_rb)   > 0 else 0
        avg_l_rb  = losses_rb['pnl'].mean() if len(losses_rb) > 0 else 0
        rr_rb     = abs(avg_w_rb / avg_l_rb) if avg_l_rb != 0 else 0
        dd_rb     = calc_max_drawdown(t_rb)
        diff_rb   = total_rb - _base_total_rb
        bar_w     = int(wr_rb / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
        mark      = "✅" if total_rb > _base_total_rb else "❌"
        print(f"  {mark} {lbl_rb:<38} [{bar}] {wr_rb:5.1f}%  取引:{len(t_rb):3d}  "
              f"合計:{total_rb:+8.1f}$/oz({diff_rb:+.1f})  DD:{dd_rb:+.1f}  RR:{rr_rb:.2f}")

    # ──── ⑥d マルチタイムフレーム確認 ────
    print(f"\n{'=' * 72}")
    print("  【⑥d マルチタイムフレーム確認】（1時間足で方向一致のみ）")
    print("  15分足シグナルを1時間足のトレンド方向でフィルタリング")
    print("─" * 72)

    try:
        print("  1時間足データ取得中...")
        df1h_mtf = yf.download("GC=F", period="60d", interval="1h", progress=False)
        if isinstance(df1h_mtf.columns, pd.MultiIndex):
            df1h_mtf.columns = df1h_mtf.columns.get_level_values(0)
        df1h_mtf.index = pd.to_datetime(df1h_mtf.index)
        if df1h_mtf.index.tz is not None:
            df1h_mtf.index = df1h_mtf.index.tz_localize(None)
        df1h_mtf = df1h_mtf.dropna(subset=['Close'])
        print(f"  取得: {len(df1h_mtf)}本")

        close1h = df1h_mtf['Close']
        # 1h足の各種方向指標を計算
        ema20_1h  = close1h.ewm(span=20, adjust=False).mean()
        ema50_1h  = close1h.ewm(span=50, adjust=False).mean()
        ema200_1h = close1h.ewm(span=min(200, len(close1h)-1), adjust=False).mean()
        macd1h    = close1h.ewm(span=12, adjust=False).mean() - close1h.ewm(span=26, adjust=False).mean()
        macd1h_sig = macd1h.ewm(span=9, adjust=False).mean()
        adx1h, di_p1h, di_m1h = calculate_adx(df1h_mtf['High'], df1h_mtf['Low'], close1h)

        # 1h方向をDataFrameにまとめ
        df1h_dir = pd.DataFrame({
            'ema_bull':  ema20_1h > ema50_1h,        # EMA20>EMA50
            'ema200_bull': close1h > ema200_1h,       # 終値>EMA200
            'macd_bull': macd1h > macd1h_sig,         # MACD>シグナル
            'di_bull':   di_p1h > di_m1h,             # DI+>DI-
        }, index=df1h_mtf.index)

        # 15分足シグナルの各タイムスタンプに対して1h方向をマッピング
        df_sig_rsi_mtf = apply_filters(df_sig, rsi_filter=True).copy()

        # インデックスをdatetime64[ns]に統一
        df1h_dir.index = df1h_dir.index.astype('datetime64[ns]')

        def map_1h_direction(sig_times, col):
            """15分足のタイムスタンプを1時間足の方向にマッピング"""
            result = []
            idx_arr = df1h_dir.index.values  # numpy datetime64配列
            for t in sig_times:
                t_ns = np.datetime64(t, 'ns')
                mask = idx_arr <= t_ns
                if mask.any():
                    result.append(df1h_dir[col].values[mask][-1])
                else:
                    result.append(None)
            return result

        sig_times = pd.to_datetime(df_sig_rsi_mtf['time'])
        df_sig_rsi_mtf = df_sig_rsi_mtf.copy()
        df_sig_rsi_mtf['1h_ema_bull']   = map_1h_direction(sig_times, 'ema_bull')
        df_sig_rsi_mtf['1h_ema200_bull']= map_1h_direction(sig_times, 'ema200_bull')
        df_sig_rsi_mtf['1h_macd_bull']  = map_1h_direction(sig_times, 'macd_bull')
        df_sig_rsi_mtf['1h_di_bull']    = map_1h_direction(sig_times, 'di_bull')

        def mtf_filter(s, col):
            """1h方向と15分足シグナル方向の一致チェック"""
            buy_ok  = (s['crossover'] == 'UP_CROSS')   & (s[col] == True)
            sell_ok = (s['crossover'] == 'DOWN_CROSS') & (s[col] == False)
            return buy_ok | sell_ok

        mtf_base_total = simulate_trades(df, df_sig_rsi_mtf, use_trailing=True)['pnl'].sum()

        scenarios_mtf = [
            ("①RSI（ベースライン）",             df_sig_rsi_mtf,                          None),
            ("MTF: 1h EMA20>50 一致",           df_sig_rsi_mtf[mtf_filter(df_sig_rsi_mtf, '1h_ema_bull')].copy(),   '1h_ema_bull'),
            ("MTF: 1h 終値>EMA200 一致",         df_sig_rsi_mtf[mtf_filter(df_sig_rsi_mtf, '1h_ema200_bull')].copy(),'1h_ema200_bull'),
            ("MTF: 1h MACD方向 一致",            df_sig_rsi_mtf[mtf_filter(df_sig_rsi_mtf, '1h_macd_bull')].copy(),  '1h_macd_bull'),
            ("MTF: 1h DI方向 一致",              df_sig_rsi_mtf[mtf_filter(df_sig_rsi_mtf, '1h_di_bull')].copy(),    '1h_di_bull'),
            # 複合条件
            ("MTF: EMA+MACD両方一致",
             df_sig_rsi_mtf[
                 mtf_filter(df_sig_rsi_mtf, '1h_ema_bull') &
                 mtf_filter(df_sig_rsi_mtf, '1h_macd_bull')
             ].copy(), None),
            ("MTF: EMA+DI両方一致",
             df_sig_rsi_mtf[
                 mtf_filter(df_sig_rsi_mtf, '1h_ema_bull') &
                 mtf_filter(df_sig_rsi_mtf, '1h_di_bull')
             ].copy(), None),
            ("MTF: EMA+MACD+DI全一致",
             df_sig_rsi_mtf[
                 mtf_filter(df_sig_rsi_mtf, '1h_ema_bull') &
                 mtf_filter(df_sig_rsi_mtf, '1h_macd_bull') &
                 mtf_filter(df_sig_rsi_mtf, '1h_di_bull')
             ].copy(), None),
        ]

        print(f"\n  {'フィルター':<32}  {'勝率':>6}  {'取引':>5}  {'合計P&L':>10}  {'ベース差':>8}  {'DD':>7}  {'RR':>5}")
        print(f"  {'─'*32}  {'─'*6}  {'─'*5}  {'─'*10}  {'─'*8}  {'─'*7}  {'─'*5}")

        mtf_results = []
        for lbl_mtf, filtered_mtf, _ in scenarios_mtf:
            t_mtf = simulate_trades(df, filtered_mtf, use_trailing=True)
            if len(t_mtf) == 0:
                print(f"  ❓ {lbl_mtf:<32}: シグナルなし")
                continue
            w = t_mtf[t_mtf['pnl'] > 0]; l = t_mtf[t_mtf['pnl'] <= 0]
            wr    = len(w) / len(t_mtf) * 100
            total = t_mtf['pnl'].sum()
            aw    = w['pnl'].mean()  if len(w) > 0 else 0
            al    = l['pnl'].mean()  if len(l) > 0 else 0
            rr    = abs(aw/al)       if al != 0 else 0
            dd    = calc_max_drawdown(t_mtf)
            diff  = total - mtf_base_total
            mark  = "✅" if total > mtf_base_total else "❌"
            print(f"  {mark} {lbl_mtf:<32}  {wr:6.1f}%  {len(t_mtf):5d}  {total:+10.1f}  {diff:+8.1f}  {dd:+7.1f}  {rr:5.2f}")
            mtf_results.append({'label': lbl_mtf, 'win_rate': round(wr,1),
                                 'num_trades': len(t_mtf), 'total': round(total,2),
                                 'diff': round(diff,2), 'rr': round(rr,2), 'max_dd': round(dd,2)})

        winners_mtf = [r for r in mtf_results if r['total'] > mtf_base_total]
        print(f"\n  改善あり: {len(winners_mtf)}/{len(mtf_results)}件")
        if winners_mtf:
            best_mtf = max(winners_mtf, key=lambda x: x['total'])
            print(f"  🏆 最良: {best_mtf['label']}")
            print(f"     勝率:{best_mtf['win_rate']}%  取引:{best_mtf['num_trades']}  "
                  f"合計:{best_mtf['total']:+.1f}$/oz  ベース差:{best_mtf['diff']:+.1f}$/oz")
        else:
            print(f"  ※全パターンでベースライン(+255.8$/oz)を下回りました")

    except Exception as e:
        import traceback
        print(f"  ⚠️  MTFテストエラー: {e}")
        traceback.print_exc()

    # ──── ⑥b ADX/DI フィルター比較 ────
    print(f"\n{'=' * 72}")
    print("  【⑥b ADX/DI フィルター比較】（15分足 × 過去60日）")
    print("  ベースライン: ①RSIフィルターのみ (+255.8$/oz, 65.8%)")
    print("─" * 72)

    df_sig_base = apply_filters(df_sig, rsi_filter=True)

    def run_adx_filter(label, mask_fn):
        filtered = df_sig_base[mask_fn(df_sig_base)].copy()
        t = simulate_trades(df, filtered, use_trailing=True)
        if len(t) == 0:
            print(f"  {label:<42}: シグナルなし")
            return None
        wins   = t[t['pnl'] > 0]
        losses = t[t['pnl'] <= 0]
        wr     = len(wins) / len(t) * 100
        total  = t['pnl'].sum()
        avg_w  = wins['pnl'].mean()   if len(wins)   > 0 else 0
        avg_l  = losses['pnl'].mean() if len(losses) > 0 else 0
        rr     = abs(avg_w / avg_l)   if avg_l != 0 else 0
        dd     = calc_max_drawdown(t)
        diff   = total - base_total
        bar_w  = int(wr / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
        mark   = "✅" if total > base_total else "❌"
        print(f"  {mark} {label:<40} [{bar}] {wr:5.1f}%  取引:{len(t):3d}  "
              f"合計:{total:+8.1f}$/oz({diff:+.1f})  DD:{dd:+.1f}  RR:{rr:.2f}")
        return {'label': label, 'win_rate': round(wr,1), 'num_trades': len(t),
                'total': round(total,2), 'diff': round(diff,2),
                'avg_win': round(avg_w,2), 'avg_loss': round(avg_l,2),
                'rr': round(rr,2), 'max_dd': round(dd,2)}

    base_total = df_sig_base.pipe(lambda s: simulate_trades(df, s, use_trailing=True))['pnl'].sum()
    t_base_adx = simulate_trades(df, df_sig_base, use_trailing=True)
    base_total  = t_base_adx['pnl'].sum()
    print_stats("  ①RSI（ベースライン）", t_base_adx)
    print()

    adx_results = []

    print("  ─ A. DI方向フィルター ─")
    print("    BUY時はDI+>DI-、SELL時はDI->DI+ のみ取る")
    r = run_adx_filter(
        "A1. DI方向一致",
        lambda s: (
            ((s['crossover']=='UP_CROSS')   & (s['di_p'] > s['di_m'])) |
            ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p']))
        )
    )
    if r: adx_results.append(r)
    print()

    print("  ─ B. DIスプレッドフィルター（|DI+−DI-| > 閾値） ─")
    for thr in [5, 10, 15, 20]:
        r = run_adx_filter(
            f"B{thr}. |DI+−DI-|>{thr}",
            lambda s, t=thr: (s['di_p'] - s['di_m']).abs() > t
        )
        if r: adx_results.append(r)
    print()

    print("  ─ C. ADX上昇フィルター（直近3本でADX上昇中） ─")
    adx_rising = df_sig['adx'] > df_sig['adx'].shift(3)
    df_sig_rising = df_sig_base[df_sig_base.index.isin(df_sig_base[adx_rising.reindex(df_sig_base.index, fill_value=False)].index)]

    # ADX上昇を別途計算
    adx_arr = df_sig_base['adx'].values
    adx_rise_mask = np.zeros(len(df_sig_base), dtype=bool)
    for ii in range(3, len(df_sig_base)):
        if adx_arr[ii] > adx_arr[ii-3]:
            adx_rise_mask[ii] = True
    df_sig_base2 = df_sig_base.copy()
    df_sig_base2['adx_rising'] = adx_rise_mask

    r = run_adx_filter(
        "C1. ADX上昇中",
        lambda s: s['adx_rising'] if 'adx_rising' in s.columns else pd.Series(True, index=s.index)
    )
    # 直接計算
    filtered_c = df_sig_base2[df_sig_base2['adx_rising']].copy()
    t_c = simulate_trades(df, filtered_c, use_trailing=True)
    if len(t_c) > 0:
        wins_c = t_c[t_c['pnl'] > 0]
        losses_c = t_c[t_c['pnl'] <= 0]
        wr_c   = len(wins_c) / len(t_c) * 100
        total_c = t_c['pnl'].sum()
        avg_w_c = wins_c['pnl'].mean()   if len(wins_c)   > 0 else 0
        avg_l_c = losses_c['pnl'].mean() if len(losses_c) > 0 else 0
        rr_c    = abs(avg_w_c / avg_l_c) if avg_l_c != 0 else 0
        dd_c    = calc_max_drawdown(t_c)
        diff_c  = total_c - base_total
        bar_w   = int(wr_c / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
        mark    = "✅" if total_c > base_total else "❌"
        print(f"  {mark} {'C1. ADX上昇中':<40} [{bar}] {wr_c:5.1f}%  取引:{len(t_c):3d}  "
              f"合計:{total_c:+8.1f}$/oz({diff_c:+.1f})  DD:{dd_c:+.1f}  RR:{rr_c:.2f}")
        adx_results.append({'label': 'C1. ADX上昇中', 'win_rate': round(wr_c,1),
                             'num_trades': len(t_c), 'total': round(total_c,2),
                             'diff': round(diff_c,2), 'avg_win': round(avg_w_c,2),
                             'avg_loss': round(avg_l_c,2), 'rr': round(rr_c,2), 'max_dd': round(dd_c,2)})
    print()

    print("  ─ D. ADX閾値 + DI方向組み合わせ ─")
    for adx_thr in [20, 25]:
        r = run_adx_filter(
            f"D{adx_thr}. ADX>{adx_thr} + DI方向一致",
            lambda s, t=adx_thr: (
                (s['adx'] >= t) & (
                    ((s['crossover']=='UP_CROSS')   & (s['di_p'] > s['di_m'])) |
                    ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p']))
                )
            )
        )
        if r: adx_results.append(r)
    print()

    print("  ─ E. ADX帯域別TP調整 ─")
    for adx_thr, tp_trend, tp_range in [(25, 20.0, 10.0), (25, 25.0, 12.0)]:
        t_e = simulate_trades(df, df_sig_base, use_trailing=True,
                              use_adx_adaptive_tp=True, adx_trend_threshold=adx_thr)
        if len(t_e) > 0:
            wins_e = t_e[t_e['pnl'] > 0]
            losses_e = t_e[t_e['pnl'] <= 0]
            wr_e    = len(wins_e) / len(t_e) * 100
            total_e = t_e['pnl'].sum()
            avg_w_e = wins_e['pnl'].mean()   if len(wins_e)   > 0 else 0
            avg_l_e = losses_e['pnl'].mean() if len(losses_e) > 0 else 0
            rr_e    = abs(avg_w_e / avg_l_e) if avg_l_e != 0 else 0
            dd_e    = calc_max_drawdown(t_e)
            diff_e  = total_e - base_total
            bar_w   = int(wr_e / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
            mark    = "✅" if total_e > base_total else "❌"
            lbl     = f"E. ADX適応TP(閾値{adx_thr})"
            print(f"  {mark} {lbl:<40} [{bar}] {wr_e:5.1f}%  取引:{len(t_e):3d}  "
                  f"合計:{total_e:+8.1f}$/oz({diff_e:+.1f})  DD:{dd_e:+.1f}  RR:{rr_e:.2f}")
            adx_results.append({'label': lbl, 'win_rate': round(wr_e,1),
                                 'num_trades': len(t_e), 'total': round(total_e,2),
                                 'diff': round(diff_e,2), 'avg_win': round(avg_w_e,2),
                                 'avg_loss': round(avg_l_e,2), 'rr': round(rr_e,2), 'max_dd': round(dd_e,2)})
    print()

    print("  ─ F. ①RSI + 上位フィルター組み合わせ ─")
    combos = [
        ("F1. RSI + DI方向一致",
         lambda s: ((s['crossover']=='UP_CROSS') & (s['di_p'] > s['di_m'])) |
                   ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p']))),
        ("F2. RSI + ADX>20 + DI方向",
         lambda s: (s['adx'] >= 20) & (
             ((s['crossover']=='UP_CROSS') & (s['di_p'] > s['di_m'])) |
             ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p'])))),
        ("F3. RSI + ADX>25 + DI方向",
         lambda s: (s['adx'] >= 25) & (
             ((s['crossover']=='UP_CROSS') & (s['di_p'] > s['di_m'])) |
             ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p'])))),
        ("F4. RSI + |DI差|>10 + DI方向",
         lambda s: ((s['di_p'] - s['di_m']).abs() > 10) & (
             ((s['crossover']=='UP_CROSS') & (s['di_p'] > s['di_m'])) |
             ((s['crossover']=='DOWN_CROSS') & (s['di_m'] > s['di_p'])))),
    ]
    for lbl_f, mask_f in combos:
        r = run_adx_filter(lbl_f, mask_f)
        if r: adx_results.append(r)
    print()

    # サマリー
    if adx_results:
        winners = [r for r in adx_results if r['total'] > base_total]
        print(f"  ── サマリー ──")
        print(f"  ベースライン(①RSI): 勝率65.8%  取引158  合計+255.8$/oz")
        print(f"  改善あり: {len(winners)}/{len(adx_results)}件")
        if winners:
            best = max(winners, key=lambda x: x['total'])
            print(f"  🏆 最良: {best['label']}")
            print(f"     勝率:{best['win_rate']}%  取引:{best['num_trades']}  "
                  f"合計:{best['total']:+.1f}$/oz  ベース差:{best['diff']:+.1f}$/oz  RR:{best['rr']:.2f}")

    # ──── ⑥c パーフェクトオーダーフィルター ────
    print(f"\n{'=' * 72}")
    print("  【⑥c パーフェクトオーダーフィルター】（25MA / 75MA / 200MA）")
    print("  BUY: 25MA>75MA>200MA  /  SELL: 25MA<75MA<200MA")
    print("─" * 72)

    df_sig_po = df_sig.copy()
    # NaN除去（SMAが揃うのに200本必要）
    df_sig_po = df_sig_po.dropna(subset=['sma25', 'sma75', 'sma200'])

    def po_buy(s):
        return (s['crossover'] == 'UP_CROSS') & (s['sma25'] > s['sma75']) & (s['sma75'] > s['sma200'])
    def po_sell(s):
        return (s['crossover'] == 'DOWN_CROSS') & (s['sma25'] < s['sma75']) & (s['sma75'] < s['sma200'])
    def po_either(s):
        return po_buy(s) | po_sell(s)

    po_base_total = simulate_trades(df, apply_filters(df_sig, rsi_filter=True), use_trailing=True)['pnl'].sum()

    scenarios_po = [
        ("PO単独（RSIなし）",        df_sig_po[po_either(df_sig_po)].copy()),
        ("PO + RSIフィルター",       apply_filters(df_sig_po[po_either(df_sig_po)].copy(), rsi_filter=True)),
        ("PO BUYのみ + RSI",         apply_filters(df_sig_po[po_buy(df_sig_po)].copy(), rsi_filter=True)),
        ("PO SELLのみ + RSI",        apply_filters(df_sig_po[po_sell(df_sig_po)].copy(), rsi_filter=True)),
        # 緩和版：75MA>200MAのみ（大きなトレンド確認だけ）
        ("緩和PO(75>200のみ) + RSI",
         apply_filters(df_sig_po[
             ((df_sig_po['crossover']=='UP_CROSS')   & (df_sig_po['sma75'] > df_sig_po['sma200'])) |
             ((df_sig_po['crossover']=='DOWN_CROSS') & (df_sig_po['sma75'] < df_sig_po['sma200']))
         ].copy(), rsi_filter=True)),
    ]

    print(f"\n  ベースライン(①RSI): 勝率65.8%  取引158  合計+255.8$/oz\n")
    po_results = []
    for lbl_po, filtered_po in scenarios_po:
        t_po = simulate_trades(df, filtered_po, use_trailing=True)
        if len(t_po) == 0:
            print(f"  {'❓'} {lbl_po:<42}: シグナルなし（PO条件に合うシグナルがない）")
            continue
        wins_po   = t_po[t_po['pnl'] > 0]
        losses_po = t_po[t_po['pnl'] <= 0]
        wr_po     = len(wins_po) / len(t_po) * 100
        total_po  = t_po['pnl'].sum()
        avg_w_po  = wins_po['pnl'].mean()   if len(wins_po)   > 0 else 0
        avg_l_po  = losses_po['pnl'].mean() if len(losses_po) > 0 else 0
        rr_po     = abs(avg_w_po / avg_l_po) if avg_l_po != 0 else 0
        dd_po     = calc_max_drawdown(t_po)
        diff_po   = total_po - po_base_total
        bar_w     = int(wr_po / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
        mark      = "✅" if total_po > po_base_total else "❌"
        print(f"  {mark} {lbl_po:<42} [{bar}] {wr_po:5.1f}%  取引:{len(t_po):3d}  "
              f"合計:{total_po:+8.1f}$/oz({diff_po:+.1f})  DD:{dd_po:+.1f}  RR:{rr_po:.2f}")
        po_results.append({'label': lbl_po, 'win_rate': round(wr_po,1),
                            'num_trades': len(t_po), 'total': round(total_po,2),
                            'diff': round(diff_po,2), 'rr': round(rr_po,2), 'max_dd': round(dd_po,2)})

    if po_results:
        winners_po = [r for r in po_results if r['total'] > po_base_total]
        print(f"\n  改善あり: {len(winners_po)}/{len(po_results)}件")
        if winners_po:
            best_po = max(winners_po, key=lambda x: x['total'])
            print(f"  🏆 最良: {best_po['label']}")
            print(f"     勝率:{best_po['win_rate']}%  取引:{best_po['num_trades']}  "
                  f"合計:{best_po['total']:+.1f}$/oz  ベース差:{best_po['diff']:+.1f}$/oz  RR:{best_po['rr']:.2f}")
        else:
            print(f"  ※全パターンでベースライン(+255.8$/oz)を下回りました")

    # ──── ⑦ スキャルピング再エントリー戦略 ────
    print(f"\n{'=' * 72}")
    print("  【⑦ スキャルピング再エントリー戦略】（15分足 × RSIフィルター）")
    print("  トレーリング決済後、チャネル継続+ADX≥閾値なら即再エントリー")
    print("─" * 72)

    df_sig_rsi_scalp = apply_filters(df_sig, rsi_filter=True)

    # ベースライン（現状 RSI+トレーリング）
    t_base = simulate_trades(df, df_sig_rsi_scalp, use_trailing=True)
    wins_b = t_base[t_base['pnl'] > 0]
    base_total = t_base['pnl'].sum()
    base_wr    = len(wins_b) / len(t_base) * 100 if len(t_base) > 0 else 0
    print(f"\n  ① ベースライン（現状RSI+トレーリング）")
    print_stats("  現状", t_base)

    SPREAD = 0.5  # XAUUSD往復スプレッド（$/oz）
    base_spread_cost = len(t_base) * SPREAD
    print(f"  ※スプレッド考慮: {SPREAD}$/oz × {len(t_base)}取引 = -{base_spread_cost:.1f}$/oz")
    print(f"  ※ベースライン（スプレッドなし）: {base_total:+.1f}$/oz → "
          f"（スプレッドあり）: {base_total - base_spread_cost:+.1f}$/oz\n")

    print(f"  ━ スキャルピング再エントリー比較 ━")
    scalp_results = []
    for adx_min in [20.0, 25.0]:
        for trail_trigger, trail_width in [(2.0, 1.5), (2.5, 1.5), (3.0, 2.0), (4.0, 2.5)]:
            for period in [20, 30, 50]:
                df_tr, df_sess = simulate_trades_scalping(
                    df, df_sig_rsi_scalp,
                    trail_trigger=trail_trigger, trail_width=trail_width,
                    adx_min=adx_min, channel_period=period,
                    max_reentry=10,
                )
                if len(df_tr) == 0:
                    continue
                wins = df_tr[df_tr['pnl'] > 0]
                losses = df_tr[df_tr['pnl'] <= 0]
                wr    = len(wins) / len(df_tr) * 100
                total = df_tr['pnl'].sum()
                avg_w = wins['pnl'].mean()   if len(wins)   > 0 else 0
                avg_l = losses['pnl'].mean() if len(losses) > 0 else 0
                rr    = abs(avg_w / avg_l)   if avg_l != 0 else 0
                n_sess  = len(df_sess)
                n_reent = len(df_tr[df_tr['reentry_no'] > 0])
                reasons = df_tr['exit_reason'].value_counts().to_dict()
                reason_str = " ".join([f"{k}:{v}" for k, v in reasons.items()])
                spread_cost  = len(df_tr) * SPREAD
                total_net    = total - spread_cost
                diff_vs_base = total_net - (base_total - base_spread_cost)
                diff_pct = diff_vs_base / abs(base_total - base_spread_cost) * 100 if base_total != base_spread_cost else 0
                label = f"TT{trail_trigger:.1f} TW{trail_width:.1f} P{period} ADX{int(adx_min)}"
                bar_w = int(wr / 5)
                bar   = "█" * bar_w + "░" * (20 - bar_w)
                print(f"  {label:<28} [{bar}] {wr:5.1f}%  取引:{len(df_tr):3d}(再:{n_reent:2d}) "
                      f"スプレッドなし:{total:+7.1f}  スプレッドあり:{total_net:+7.1f}$/oz({diff_pct:+.0f}%)  "
                      f"RR:{rr:.2f}  [{reason_str}]")
                scalp_results.append({
                    'label': label, 'adx_min': adx_min,
                    'trail_trigger': trail_trigger, 'trail_width': trail_width,
                    'channel_period': period,
                    'win_rate': round(wr, 1), 'num_trades': len(df_tr),
                    'num_reentry': n_reent, 'num_sessions': n_sess,
                    'total_pnl': round(total, 2),
                    'total_net': round(total_net, 2),
                    'diff_vs_base': round(diff_vs_base, 2),
                    'avg_win': round(avg_w, 2), 'avg_loss': round(avg_l, 2), 'rr': round(rr, 2),
                })
        print()

    # ベスト表示
    if scalp_results:
        best = max(scalp_results, key=lambda x: x['total_net'])
        base_net = base_total - base_spread_cost
        print(f"\n  🏆 スキャルピングベスト（スプレッド込み）: {best['label']}")
        print(f"     勝率:{best['win_rate']}%  取引:{best['num_trades']}  "
              f"スプレッドなし:{best['total_pnl']:+.1f}$/oz  スプレッドあり:{best['total_net']:+.1f}$/oz")
        print(f"\n  📊 ベース比較（スプレッド0.5$/oz込み）:")
        print(f"     ベースライン: 勝率{base_wr:.1f}%  取引{len(t_base)}  "
              f"スプレッドなし:{base_total:+.1f}$/oz  スプレッドあり:{base_net:+.1f}$/oz")
        print(f"     スキャルピング最良: 勝率{best['win_rate']}%  取引{best['num_trades']}  "
              f"スプレッドあり:{best['total_net']:+.1f}$/oz  差:{best['diff_vs_base']:+.1f}$/oz")

    # ──── ⑧ クロス継続中の再エントリー戦略 ────
    print(f"\n{'=' * 72}")
    print("  【⑧ クロス継続中の再エントリー戦略】（15分足 × RSIフィルター）")
    print("  反対クロスが出るまで、決済後も同方向に再エントリー継続")
    print("  ※通常のSL/TP/トレーリングパラメータをそのまま使用")
    print("─" * 72)

    df_sig_rsi_re = apply_filters(df_sig, rsi_filter=True)

    SPREAD = 0.5
    t_base_re = simulate_trades(df, df_sig_rsi_re, use_trailing=True)
    base_re_total = t_base_re['pnl'].sum()
    base_re_wr    = len(t_base_re[t_base_re['pnl'] > 0]) / len(t_base_re) * 100
    base_re_net   = base_re_total - len(t_base_re) * SPREAD
    print(f"\n  ① ベースライン（現状）")
    print_stats("  現状", t_base_re)
    print(f"     スプレッドあり({SPREAD}$/oz×{len(t_base_re)}): {base_re_net:+.1f}$/oz\n")

    print(f"  ━ 再エントリー比較 ━")
    # (label, max_reentry, trail_only, max_bars)
    scenarios_re = [
        ("制限なし max1",          1,  False, None),
        ("制限なし max3",          3,  False, None),
        ("制限なし max5",          5,  False, None),
        ("TRAIL後のみ max3",       3,  True,  None),
        ("TRAIL後のみ max5",       5,  True,  None),
        ("N本制限20(5h) max5",     5,  False, 20),
        ("N本制限40(10h) max5",    5,  False, 40),
        ("TRAIL後+N本20 max5",     5,  True,  20),
        ("TRAIL後+N本40 max5",     5,  True,  40),
    ]

    re_results = []
    for label_re, max_re, trail_only, max_bars in scenarios_re:
        t_re = simulate_trades_active_reentry(
            df, df_sig_rsi_re, max_reentry=max_re,
            trail_only=trail_only, max_bars=max_bars,
        )
        if len(t_re) == 0:
            continue
        wins   = t_re[t_re['pnl'] > 0]
        losses = t_re[t_re['pnl'] <= 0]
        wr     = len(wins) / len(t_re) * 100
        total  = t_re['pnl'].sum()
        avg_w  = wins['pnl'].mean()   if len(wins)   > 0 else 0
        avg_l  = losses['pnl'].mean() if len(losses) > 0 else 0
        rr     = abs(avg_w / avg_l)   if avg_l != 0 else 0
        spread_cost = len(t_re) * SPREAD
        total_net   = total - spread_cost
        diff_net    = total_net - base_re_net
        n_reent     = len(t_re[t_re['reentry_no'] > 0])
        reasons     = t_re['exit_reason'].value_counts().to_dict()
        reason_str  = " ".join([f"{k}:{v}" for k, v in reasons.items()])
        bar_w = int(wr / 5)
        bar   = "█" * bar_w + "░" * (20 - bar_w)
        print(f"  {label_re:<22} [{bar}] {wr:5.1f}%  "
              f"取引:{len(t_re):4d}(再:{n_reent:3d})  "
              f"スプレッドなし:{total:+8.1f}  スプレッドあり:{total_net:+8.1f}$/oz  "
              f"ベース差:{diff_net:+.1f}$/oz  RR:{rr:.2f}  [{reason_str}]")
        re_results.append({
            'label': label_re, 'max_reentry': max_re,
            'trail_only': trail_only, 'max_bars': max_bars,
            'win_rate': round(wr, 1),
            'num_trades': len(t_re), 'num_reentry': n_reent,
            'total_pnl': round(total, 2), 'total_net': round(total_net, 2),
            'diff_vs_base': round(diff_net, 2),
            'avg_win': round(avg_w, 2), 'avg_loss': round(avg_l, 2), 'rr': round(rr, 2),
        })

    if re_results:
        best = max(re_results, key=lambda x: x['total_net'])
        print(f"\n  🏆 ベスト: 最大{best['max_reentry']}回再エントリー")
        print(f"     勝率:{best['win_rate']}%  取引:{best['num_trades']}(再:{best['num_reentry']})  "
              f"スプレッドあり:{best['total_net']:+.1f}$/oz  ベース差:{best['diff_vs_base']:+.1f}$/oz")
        print(f"\n  📊 まとめ:")
        print(f"     ベースライン:       勝率{base_re_wr:.1f}%  取引{len(t_base_re):3d}  "
              f"スプレッドあり:{base_re_net:+.1f}$/oz")
        for r in re_results:
            sign = "✅" if r['total_net'] > base_re_net else "❌"
            print(f"     {sign} {r['label']:<22}: "
                  f"勝率{r['win_rate']:5.1f}%  取引{r['num_trades']:4d}  "
                  f"スプレッドあり:{r['total_net']:+8.1f}$/oz  差:{r['diff_vs_base']:+.1f}$/oz")

    # ──── ⑨ 5分足 vs 15分足 比較 ────
    print(f"\n{'=' * 72}")
    print("  【⑨ 5分足バックテスト】（過去60日）")
    print("  ※MACDパラメータは15分足と同じ（比較目的）")
    print("─" * 72)

    try:
        print("  5分足データ取得中...")
        df5m_raw = yf.download("GC=F", period="60d", interval="5m", progress=False)
        if isinstance(df5m_raw.columns, pd.MultiIndex):
            df5m_raw.columns = df5m_raw.columns.get_level_values(0)
        df5m_raw.index = pd.to_datetime(df5m_raw.index)
        if df5m_raw.index.tz is not None:
            df5m_raw.index = df5m_raw.index.tz_localize(None)
        df5m_raw = df5m_raw.dropna(subset=['Close'])
        print(f"  取得: {len(df5m_raw)}本 ({df5m_raw.index[0].date()} ～ {df5m_raw.index[-1].date()})")

        df5m = df5m_raw.copy()
        df5m_sig = compute_composite(df5m)

        SPREAD = 0.5
        # 15分足ベースラインの再掲
        t15_base = simulate_trades(df, apply_filters(df_sig, rsi_filter=True), use_trailing=True)
        t15_net  = t15_base['pnl'].sum() - len(t15_base) * SPREAD

        print(f"\n  ── フィルター比較 ──")
        scenarios_5m = [
            ("フィルターなし",      False, False),
            ("①RSIフィルターのみ", True,  False),
        ]
        results_5m = []
        for label5, rsi_f, sell_f in scenarios_5m:
            df5m_f = apply_filters(df5m_sig, rsi_filter=rsi_f, sell_gap_filter=sell_f)
            t5 = simulate_trades(df5m, df5m_f, use_trailing=True)
            if len(t5) == 0:
                print(f"  {label5}: シグナルなし"); continue
            wins   = t5[t5['pnl'] > 0]
            losses = t5[t5['pnl'] <= 0]
            wr     = len(wins) / len(t5) * 100
            total  = t5['pnl'].sum()
            avg_w  = wins['pnl'].mean()   if len(wins)   > 0 else 0
            avg_l  = losses['pnl'].mean() if len(losses) > 0 else 0
            rr     = abs(avg_w / avg_l)   if avg_l != 0 else 0
            total_net = total - len(t5) * SPREAD
            bar_w = int(wr / 5); bar = "█" * bar_w + "░" * (20 - bar_w)
            print(f"  {label5:<22} [{bar}] {wr:5.1f}%  取引:{len(t5):4d}  "
                  f"スプレッドなし:{total:+8.1f}  スプレッドあり:{total_net:+8.1f}$/oz  "
                  f"RR:{rr:.2f}  平均勝:{avg_w:+.2f} 平均負:{avg_l:+.2f}")
            results_5m.append({'label': label5, 'win_rate': round(wr,1),
                                'num_trades': len(t5), 'total': round(total,2),
                                'total_net': round(total_net,2),
                                'avg_win': round(avg_w,2), 'avg_loss': round(avg_l,2), 'rr': round(rr,2)})

        print(f"\n  ── 15分足 vs 5分足 サマリー ──")
        print(f"  {'':22}  {'勝率':>6}  {'取引数':>5}  {'スプレッドあり':>12}")
        print(f"  {'15分足①RSIフィルター':22}  {len(t15_base[t15_base['pnl']>0])/len(t15_base)*100:6.1f}%"
              f"  {len(t15_base):5d}  {t15_net:+12.1f}$/oz  ← 現在の本番")
        for r in results_5m:
            diff = r['total_net'] - t15_net
            sign = "✅" if r['total_net'] > t15_net else "❌"
            print(f"  {sign} 5分足 {r['label']:<16}  {r['win_rate']:6.1f}%"
                  f"  {r['num_trades']:5d}  {r['total_net']:+12.1f}$/oz  (15分比:{diff:+.1f})")

        # 月間換算
        print(f"\n  ── 月間換算（0.01ロット マイクロ口座） ──")
        days = (df5m_raw.index[-1] - df5m_raw.index[0]).days
        months = max(days / 30, 1)
        lot = 0.01; contract = 1  # マイクロ: 1oz/lot
        print(f"  期間: {days}日 ({months:.1f}ヶ月)  ロット:{lot}  契約サイズ:{contract}oz")
        print(f"  15分足①RSI: {t15_net/months*lot*contract*150:+.0f}円/月")
        for r in results_5m:
            mo = r['total_net'] / months * lot * contract * 150
            print(f"  5分足 {r['label']}: {mo:+.0f}円/月")

    except Exception as e:
        print(f"  ⚠️  5分足テストエラー: {e}")

    print(f"\n{'=' * 72}")
    print("  バックテスト完了")
    print("=" * 72)


if __name__ == "__main__":
    main()
