import { useEffect, useRef, useState } from "react";
import { io, Socket } from "socket.io-client";
import { LocalNotifications } from "@capacitor/local-notifications";
import { PushNotifications } from "@capacitor/push-notifications";
import { Haptics } from "@capacitor/haptics";
import { Capacitor } from "@capacitor/core";
import StatusDashboard from "./StatusDashboard";

// ==================== 型定義 ====================
interface CompositeData {
  buy_score: number;
  sell_score: number;
  buy_reasons: string[];
  sell_reasons: string[];
  ema20: number;
  ema50: number;
  ema_long: number;
  bb_upper: number;
  bb_lower: number;
  stoch_k: number;
  stoch_d: number;
  adx: number;
  di_plus: number;
  di_minus: number;
  atr: number;
  is_trending: boolean;
}

interface Signal {
  crossover: "UP_CROSS" | "DOWN_CROSS" | null;
  rsi: number;
  signal_line: number;
  main_line?: number;
  latest_close: number;
  ai_valid: boolean | null;
  ai_confidence: number | null;
  ai_reason: string | null;
  ai_sl_suggestion?: number | null;
  ai_tp_suggestion?: number | null;
  ai_trailing_trigger?: number | null;
  ai_trailing_width?: number | null;
  ai_key_level?: string;
  composite?: CompositeData;
  generated_at: string;
  timeframe?: number;
  test_mode?: boolean;
  crossover_mode?: string;
  db_id?: number;
  trading_mode?: string;
  auto_executed?: boolean;
}

interface Trade {
  id: number;
  signal_id: number | null;
  direction: "BUY" | "SELL";
  entry_price: number;
  entry_time: string;
  exit_price: number | null;
  exit_time: string | null;
  profit_loss: number | null;
  pips: number | null;
  status: "OPEN" | "CLOSED_PROFIT" | "CLOSED_LOSS" | "CANCELLED";
  notes: string | null;
}

interface ActionLog {
  timestamp: string;
  level: string;
  message: string;
}

interface TodayStats {
  date: string;
  total_signals: number;
  total_trades: number;
  open_trades: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  total_pips: number;
  avg_profit: number;
  confidence_avg: number | null;
}

// ==================== 設定 ====================
// v3B-rebuild
const APP_VERSION = "1.29";
const RENDER_URL = import.meta.env.VITE_RENDER_URL ?? "https://ai-trading-system-81jb.onrender.com";
const TIMEFRAMES = [1, 5, 15, 30, 60] as const;

// ==================== 振動ユーティリティ ====================
async function doVibrate(duration: number, count: number, gap: number) {
  for (let i = 0; i < count; i++) {
    try { await Haptics.vibrate({ duration }); } catch (_) {}
    if (i < count - 1) await new Promise(r => setTimeout(r, gap));
  }
}

// ==================== メインコンポーネント ====================
export default function App() {
  const [signal, setSignal] = useState<Signal | null>(null);
  const [history, setHistory] = useState<Signal[]>([]);
  const [connected, setConnected] = useState(false);
  const [lastSocketReceived, setLastSocketReceived] = useState<Date | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"signal" | "analytics" | "dashboard">("signal");
  const [trades, setTrades] = useState<Trade[]>([]);
  const [todayStats, setTodayStats] = useState<TodayStats | null>(null);
  const [actionLogs, setActionLogs] = useState<ActionLog[]>([]);
  const [entryModalOpen, setEntryModalOpen] = useState(false);
  const [closeModalTrade, setCloseModalTrade] = useState<Trade | null>(null);
  const [entryPrice, setEntryPrice] = useState("");
  const [entryDirection, setEntryDirection] = useState<"BUY" | "SELL">("BUY");
  const [exitPrice, setExitPrice] = useState("");
  const [tradeLoading, setTradeLoading] = useState(false);
  const [tf, setTf] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_tf") ?? "30"); } catch { return 30; }
  });
  const [mode, setMode] = useState<"PRODUCTION" | "TEST">(() => {
    try { return (localStorage.getItem("gt_mode") as any) ?? "PRODUCTION"; } catch { return "PRODUCTION"; }
  });
  const [crossoverMode, setCrossoverMode] = useState<"RSI" | "MACD" | "RSI_MACD" | "COMPOSITE">(() => {
    try { return (localStorage.getItem("gt_crossover") as any) ?? "COMPOSITE"; } catch { return "COMPOSITE"; }
  });
  const [tradingMode, setTradingMode] = useState<"MANUAL" | "SEMI_AUTO" | "FULL_AUTO" | "AI_CLOSE_MODE">(() => {
    try { return (localStorage.getItem("gt_trading_mode") as any) ?? "MANUAL"; } catch { return "MANUAL"; }
  });
  const [autoThreshold, setAutoThreshold] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_auto_threshold") ?? "70"); } catch { return 70; }
  });
  const [entryThreshold, setEntryThreshold] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_entry_threshold") ?? "60"); } catch { return 60; }
  });
  const [useKeyLevels, setUseKeyLevels] = useState<boolean>(() => {
    try { return (localStorage.getItem("gt_use_key_levels") ?? "true") === "true"; } catch { return true; }
  });
  const [useRangeMode, setUseRangeMode] = useState<boolean>(() => {
    try { return (localStorage.getItem("gt_use_range_mode") ?? "false") === "true"; } catch { return false; }
  });
  const [srLevels, setSrLevels] = useState<{
    resistance: number[]; support: number[]; broken_resistance: number[];
    current_price: number; updated_at: string;
  }>({ resistance: [], support: [], broken_resistance: [], current_price: 0, updated_at: "" });
  const [executing, setExecuting] = useState(false);
  const [execMsg, setExecMsg] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [vibDuration, setVibDuration] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_vib_duration") ?? "700"); } catch { return 700; }
  });
  const [vibCount, setVibCount] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_vib_count") ?? "3"); } catch { return 3; }
  });
  const [vibGap, setVibGap] = useState<number>(() => {
    try { return parseInt(localStorage.getItem("gt_vib_gap") ?? "150"); } catch { return 150; }
  });
  const [hybridSlConfig, setHybridSlConfig] = useState({
    initial_sl_price: 5,
    trailing_trigger_price: 5,
    trailing_sl_price: 3,
    initial_tp_price: 15,
    enabled: false,   // 推奨: OFF（Gemini任せ）
  });
  const [reentryEnabled, setReentryEnabled] = useState(true);       // 推奨: ON
  const [aiExitEnabled, setAiExitEnabled] = useState(true);         // 推奨: ON
  const [rsiFilterEnabled, setRsiFilterEnabled] = useState(true);   // 推奨: ON (RSI<35スキップ)
  const [crossFlipEnabled, setCrossFlipEnabled] = useState(false);  // クロス転換モード（デフォルトOFF）
  const [lotTrailing, setLotTrailing]           = useState("0.3");  // トレーリングロット
  const [lotCrossFlip, setLotCrossFlip]         = useState("0.1");  // クロス転換ロット

  const tradingModeRef = useRef(tradingMode);
  useEffect(() => { tradingModeRef.current = tradingMode; }, [tradingMode]);

  const vibDurationRef = useRef(vibDuration);
  const vibCountRef = useRef(vibCount);
  const vibGapRef = useRef(vibGap);
  useEffect(() => { vibDurationRef.current = vibDuration; try { localStorage.setItem("gt_vib_duration", String(vibDuration)); } catch {} }, [vibDuration]);
  useEffect(() => { vibCountRef.current = vibCount;   try { localStorage.setItem("gt_vib_count",    String(vibCount));    } catch {} }, [vibCount]);
  useEffect(() => { vibGapRef.current = vibGap;       try { localStorage.setItem("gt_vib_gap",      String(vibGap));      } catch {} }, [vibGap]);

  const fetchTrades = async () => {
    try {
      const r = await fetch(`${RENDER_URL}/api/trades`);
      if (r.ok) setTrades(await r.json());
    } catch (_) {}
  };

  const fetchTodayStats = async () => {
    try {
      const r = await fetch(`${RENDER_URL}/api/stats/today`);
      if (r.ok) setTodayStats(await r.json());
    } catch (_) {}
  };

  const fetchActionLog = async () => {
    try {
      const r = await fetch(`${RENDER_URL}/api/action-log?limit=30`);
      if (r.ok) setActionLogs(await r.json());
    } catch (_) {}
  };

  const [fcmStatus, setFcmStatus] = useState<string>("初期化中...");

  useEffect(() => {
    // FCMプッシュ通知の登録（ネイティブのみ）
    if (Capacitor.isNativePlatform()) {
      PushNotifications.requestPermissions().then(result => {
        if (result.receive === "granted") {
          setFcmStatus("登録中...");
          PushNotifications.register();
        } else {
          setFcmStatus("⚠️ 通知許可なし");
        }
      });
      PushNotifications.addListener("registration", async (token) => {
        setFcmStatus("✅ FCM登録済");
        try { localStorage.setItem("gt_fcm_token", token.value); } catch (_) {}
        try {
          await fetch(`${RENDER_URL}/register-token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: token.value }),
          });
        } catch (_) { setFcmStatus("⚠️ サーバー送信失敗"); }
      });
      PushNotifications.addListener("registrationError", (err) => {
        setFcmStatus(`❌ FCM失敗: ${err.error}`);
      });
    }

    LocalNotifications.requestPermissions();
    // gold-trade-v3: エントリー/決済（強振動）
    LocalNotifications.createChannel({
      id: "gold-trade-v3",
      name: "エントリー/決済通知",
      importance: 5,
      vibration: true,
      lights: true,
      description: "エントリー・決済時の通知（強振動）",
    });
    // gold-signal-v3: クロスオーバー通知（AI_CLOSE_MODEでは振動なし）
    LocalNotifications.createChannel({
      id: "gold-signal-v3",
      name: "シグナル通知",
      importance: 4,
      vibration: false,
      lights: true,
      description: "クロスオーバーシグナルの通知",
    });

    const socket: Socket = io(RENDER_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });

    socket.on("disconnect", () => setConnected(false));

    socket.on("connect", async () => {
      setConnected(true);
      // FCMトークン登録のみ（設定POSTは行わない→「設定変更」ログ連発を防止）
      try {
        const savedFcmToken = localStorage.getItem("gt_fcm_token");
        if (savedFcmToken) {
          await fetch(`${RENDER_URL}/register-token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: savedFcmToken }),
          }).catch(() => {});
        }
      } catch (_) {}
      // ハイブリッドSL設定を初回接続時に取得
      fetch(`${RENDER_URL}/api/settings/hybrid-sl`).then(r => r.json()).then(cfg => {
        if (cfg && typeof cfg.initial_sl_price === "number") setHybridSlConfig(cfg);
      }).catch(() => {});
      // 再エントリー設定を取得
      fetch(`${RENDER_URL}/api/settings/reentry`).then(r => r.json()).then(cfg => {
        if (cfg && typeof cfg.enabled === "boolean") setReentryEnabled(cfg.enabled);
      }).catch(() => {});
      // 5分AI決済監視設定を取得
      fetch(`${RENDER_URL}/api/settings/ai-exit`).then(r => r.json()).then(cfg => {
        if (cfg && typeof cfg.enabled === "boolean") setAiExitEnabled(cfg.enabled);
      }).catch(() => {});
      // RSIフィルター設定を取得
      fetch(`${RENDER_URL}/api/settings/rsi-filter`).then(r => r.json()).then(cfg => {
        if (cfg && typeof cfg.enabled === "boolean") setRsiFilterEnabled(cfg.enabled);
      }).catch(() => {});
      // クロス転換設定を取得
      fetch(`${RENDER_URL}/api/settings/cross-flip`).then(r => r.json()).then(cfg => {
        if (cfg && typeof cfg.enabled === "boolean") setCrossFlipEnabled(cfg.enabled);
        if (cfg && cfg.lot_trailing)   setLotTrailing(String(cfg.lot_trailing));
        if (cfg && cfg.lot_cross_flip) setLotCrossFlip(String(cfg.lot_cross_flip));
      }).catch(() => {});
      // 最新シグナルとS/R水準のみ取得
      try {
        const [sigRes, srRes] = await Promise.all([
          fetch(`${RENDER_URL}/latest-signal`),
          fetch(`${RENDER_URL}/api/sr-levels`).catch(() => null),
        ]);
        const data = await sigRes.json();
        if (data && data.rsi) {
          const s: Signal = {
            crossover: data.crossover,
            rsi: data.rsi,
            signal_line: data.signal_line,
            main_line: data.main_line,
            crossover_mode: data.crossover_mode,
            composite: data.composite,
            latest_close: data.latest_close,
            ai_valid: data.ai_valid,
            ai_confidence: data.ai_confidence,
            ai_reason: data.ai_reason,
            ai_sl_suggestion: data.ai_sl_suggestion,
            ai_tp_suggestion: data.ai_tp_suggestion,
            ai_trailing_trigger: data.ai_trailing_trigger,
            ai_trailing_width: data.ai_trailing_width,
            ai_key_level: data.ai_key_level,
            generated_at: data.created_at ?? data.generated_at,
            timeframe: data.timeframe,
            test_mode: data.test_mode,
            db_id: data.db_id,
          };
          setSignal(s);
          setLastSocketReceived(new Date());
          setHistory((prev) => {
            if (prev.length > 0 && prev[0].generated_at === s.generated_at) return prev;
            return [s, ...prev].slice(0, 50);
          });
        }
        if (srRes) {
          const srData = await srRes.json().catch(() => null);
          if (srData && srData.current_price > 0) setSrLevels(srData);
        }
      } catch (_) {}
    });

    const handleVisibilityChange = () => {
      if (!document.hidden && !socket.connected) socket.connect();
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    socket.on("candle_update", (data: Signal) => {
      setSignal(data);
      setLastSocketReceived(new Date());
      // 同じgenerated_atの再配信はhistoryに追加しない
      setHistory((prev) => {
        if (prev.length > 0 && prev[0].generated_at === data.generated_at) return prev;
        return [data, ...prev].slice(0, 50);
      });
    });

    socket.on("signal", (data: Signal) => {
      setSignal(data);
      setHistory((prev) => [data, ...prev].slice(0, 50));
      setLastSocketReceived(new Date());
      // シグナル受信時にS/R水準も更新
      fetch(`${RENDER_URL}/api/sr-levels`).then(r => r.json()).then(sr => {
        if (sr && sr.current_price > 0) setSrLevels(sr);
      }).catch(() => {});

      if (data.crossover) {
        const isAiCloseMode = tradingModeRef.current === "AI_CLOSE_MODE";
        // AI_CLOSE_MODEではクロス時はHaptics振動しない（エントリー/決済FCM通知で振動）
        if (!isAiCloseMode) {
          doVibrate(vibDurationRef.current, vibCountRef.current, vibGapRef.current);
        }

        const direction = data.crossover === "UP_CROSS" ? "📈 買いシグナル" : "📉 売りシグナル";
        const label = data.test_mode ? "🧪 TEST " : "";
        const notifId = Math.floor(Math.random() * 100000);
        const slTp = data.ai_sl_suggestion && data.ai_tp_suggestion
          ? ` | SL:${data.ai_sl_suggestion} TP:${data.ai_tp_suggestion}`
          : "";
        const aiBody = data.ai_valid !== null
          ? `${data.ai_valid ? "✅" : "⚠️"} 信頼度:${data.ai_confidence}% ${data.ai_reason ?? ""}${slTp}`
          : `シグナル検出${slTp}`;
        // AI_CLOSE_MODEはシグナルチャンネル（振動なし）、それ以外はトレードチャンネル（振動あり）
        const channelId = isAiCloseMode ? "gold-signal-v3" : "gold-trade-v3";
        LocalNotifications.schedule({
          notifications: [{
            id: notifId,
            title: `${label}GOLD ${direction}`,
            body: aiBody,
            schedule: { at: new Date() },
            channelId,
            sound: "default",
          }],
        });
        setTimeout(() => {
          LocalNotifications.cancel({ notifications: [{ id: notifId }] });
        }, 5 * 60 * 1000);
      }
    });

    // 15分ごとに最新シグナルを再取得（EAが同じデータを送っていても表示を更新）
    const refreshInterval = setInterval(async () => {
      try {
        const res = await fetch(`${RENDER_URL}/latest-signal`);
        const data = await res.json();
        if (data && data.rsi) {
          setSignal(data);
          setLastSocketReceived(new Date());
        }
      } catch (_) {}
    }, 15 * 60 * 1000);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearInterval(refreshInterval);
      socket.disconnect();
      if (Capacitor.isNativePlatform()) {
        PushNotifications.removeAllListeners();
      }
    };
  }, []);

  useEffect(() => {
    if (activeTab === "analytics") {
      fetchTrades();
      fetchTodayStats();
      fetchActionLog();
    }
  }, [activeTab]);

  // ==================== 設定保存 ====================
  const saveSettings = async () => {
    setSaving(true);
    try {
      await Promise.all([
        fetch(`${RENDER_URL}/api/settings/timeframe`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timeframe: tf }),
        }),
        fetch(`${RENDER_URL}/api/settings/mode`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }),
        fetch(`${RENDER_URL}/api/settings/crossover`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ crossover_mode: crossoverMode }),
        }),
        fetch(`${RENDER_URL}/api/settings/trading-mode`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ trading_mode: tradingMode }),
        }),
        fetch(`${RENDER_URL}/api/settings/auto-threshold`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ threshold: autoThreshold }),
        }),
        fetch(`${RENDER_URL}/api/settings/entry-threshold`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ threshold: entryThreshold }),
        }),
        fetch(`${RENDER_URL}/api/settings/key-levels`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ use_key_levels: useKeyLevels }),
        }),
        fetch(`${RENDER_URL}/api/settings/range-mode`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ use_range_mode: useRangeMode }),
        }).catch(() => {}),
        fetch(`${RENDER_URL}/api/settings/hybrid-sl`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(hybridSlConfig),
        }).catch(() => {}),
        fetch(`${RENDER_URL}/api/settings/reentry`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: reentryEnabled }),
        }).catch(() => {}),
        fetch(`${RENDER_URL}/api/settings/ai-exit`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: aiExitEnabled }),
        }).catch(() => {}),
        fetch(`${RENDER_URL}/api/settings/rsi-filter`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: rsiFilterEnabled }),
        }).catch(() => {}),
        fetch(`${RENDER_URL}/api/settings/cross-flip`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: crossFlipEnabled,
            lot_trailing: parseFloat(lotTrailing) || 0.3,
            lot_cross_flip: parseFloat(lotCrossFlip) || 0.1,
          }),
        }).catch(() => {}),
      ]);
      try {
        localStorage.setItem("gt_tf", String(tf));
        localStorage.setItem("gt_mode", mode);
        localStorage.setItem("gt_crossover", crossoverMode);
        localStorage.setItem("gt_trading_mode", tradingMode);
        localStorage.setItem("gt_auto_threshold", String(autoThreshold));
        localStorage.setItem("gt_entry_threshold", String(entryThreshold));
        localStorage.setItem("gt_use_key_levels", String(useKeyLevels));
        localStorage.setItem("gt_use_range_mode", String(useRangeMode));
      } catch (_) {}
      setSaveMsg("✅ 保存しました");
      setTimeout(() => { setSaveMsg(""); setSettingsOpen(false); }, 1500);
    } catch (_) {
      setSaveMsg("❌ 保存失敗");
    } finally {
      setSaving(false);
    }
  };

  // ==================== SEMI_AUTO 実行 ====================
  const executeSemiAuto = async () => {
    if (!signal?.crossover || !signal?.ai_valid) return;
    setExecuting(true);
    setExecMsg("");
    const direction = signal.crossover === "UP_CROSS" ? "BUY" : "SELL";
    try {
      const r = await fetch(`${RENDER_URL}/api/execute-order`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_id: signal.db_id,
          direction,
          entry_price: signal.latest_close,
          sl_pips: 20,
          tp_pips: 40,
        }),
      });
      const result = await r.json();
      if (r.ok && result.success) {
        setExecMsg(`✅ ${direction}注文完了！`);
        await fetchTrades();
        await fetchTodayStats();
      } else {
        setExecMsg(`❌ 失敗: ${result.error ?? "不明なエラー"}`);
      }
    } catch (_) {
      setExecMsg("❌ 通信エラー");
    } finally {
      setExecuting(false);
      setTimeout(() => setExecMsg(""), 4000);
    }
  };

  // ==================== トレード記録 ====================
  const recordEntry = async () => {
    if (!entryPrice) return;
    setTradeLoading(true);
    try {
      const r = await fetch(`${RENDER_URL}/api/trades`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_id: signal?.db_id ?? null,
          direction: entryDirection,
          entry_price: parseFloat(entryPrice),
        }),
      });
      if (r.ok) {
        setEntryModalOpen(false);
        setEntryPrice("");
        await fetchTrades();
        await fetchTodayStats();
      }
    } catch (_) {}
    setTradeLoading(false);
  };

  const recordExit = async () => {
    if (!closeModalTrade || !exitPrice) return;
    setTradeLoading(true);
    try {
      const r = await fetch(`${RENDER_URL}/api/trades/${closeModalTrade.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exit_price: parseFloat(exitPrice) }),
      });
      if (r.ok) {
        setCloseModalTrade(null);
        setExitPrice("");
        await fetchTrades();
        await fetchTodayStats();
      }
    } catch (_) {}
    setTradeLoading(false);
  };

  // ==================== UI ====================
  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <div>
          <h1 style={styles.title}>GOLD AI トレーダー</h1>
          <div style={{ fontSize: 11, color: "#475569", marginTop: 2 }}>v{APP_VERSION}</div>
        </div>
        <button style={styles.settingsBtn} onClick={() => setSettingsOpen(true)}>⚙️</button>
      </div>

      <div style={{ ...styles.badge, background: connected ? "#22c55e" : "#ef4444" }}>
        {connected ? "● 接続中" : "○ 切断"}
      </div>
      <div style={{ fontSize: 11, color: fcmStatus.startsWith("✅") ? "#22c55e" : fcmStatus.startsWith("❌") || fcmStatus.startsWith("⚠️") ? "#f59e0b" : "#64748b", marginLeft: 8 }}>
        🔔 {fcmStatus}
      </div>

      {/* タブナビゲーション */}
      <div style={styles.tabBar}>
        <button
          style={{ ...styles.tab, ...(activeTab === "signal" ? styles.tabActive : {}) }}
          onClick={() => setActiveTab("signal")}
        >
          📊 シグナル
        </button>
        <button
          style={{ ...styles.tab, ...(activeTab === "analytics" ? styles.tabActive : {}) }}
          onClick={() => setActiveTab("analytics")}
        >
          📈 アナリティクス
        </button>
        <button
          style={{ ...styles.tab, ...(activeTab === "dashboard" ? styles.tabActive : {}) }}
          onClick={() => setActiveTab("dashboard")}
        >
          📋 ダッシュボード
        </button>
      </div>

      {/* ===== シグナルタブ ===== */}
      {activeTab === "signal" && (
        <>
          {signal ? (
            <div style={styles.card}>
              <div style={styles.cardHeader}>
                <h2 style={styles.cardTitle}>最新シグナル</h2>
                {signal.test_mode && <span style={styles.testBadge}>🧪 TEST</span>}
                {signal.timeframe && <span style={styles.tfBadge}>{signal.timeframe}分足</span>}
                {signal.crossover_mode && <span style={styles.modeBadge}>{signal.crossover_mode}</span>}
              </div>
              {signal.crossover && <Row label="クロスオーバー" value={signal.crossover} />}
              <Row label="RSI" value={signal.rsi?.toFixed(2)} />
              <Row
                label={signal.crossover_mode === "MACD" || signal.crossover_mode === "COMPOSITE" ? "MACDライン" : "RSIライン"}
                value={signal.main_line?.toFixed(4) ?? signal.rsi?.toFixed(2)}
              />
              <Row
                label={signal.crossover_mode === "RSI" || signal.crossover_mode === "RSI_MACD" ? (signal.crossover_mode === "RSI" ? "RSI Signal SMA" : "MACDシグナル(正規化)") : "MACDシグナル"}
                value={signal.signal_line?.toFixed(4)}
              />
              <Row label="終値" value={signal.latest_close?.toFixed(2)} />
              {signal.crossover_mode === "COMPOSITE" && signal.composite && (
                <>
                  <hr style={styles.divider} />
                  <div style={styles.scoreBar}>
                    <span style={{ color: "#22c55e", minWidth: 56 }}>買い {signal.composite.buy_score}pt</span>
                    <div style={styles.scoreTrack}>
                      <div style={{
                        ...styles.scoreFill,
                        width: `${signal.composite.buy_score / (signal.composite.buy_score + signal.composite.sell_score + 0.01) * 100}%`,
                        background: signal.crossover === "UP_CROSS" ? "#22c55e" : signal.crossover === "DOWN_CROSS" ? "#ef4444" : "#64748b"
                      }} />
                    </div>
                    <span style={{ color: "#ef4444", minWidth: 56, textAlign: "right" as const }}>売り {signal.composite.sell_score}pt</span>
                  </div>
                  {!signal.crossover && (
                    <div style={{ fontSize: 11, color: "#64748b", textAlign: "center" as const, marginBottom: 6 }}>
                      待機中（5点以上かつ対辺より2点超でシグナル発火）
                    </div>
                  )}
                  <div style={styles.reasonTags}>
                    {signal.composite.buy_reasons.map((r, i) => (
                      <span key={`b${i}`} style={{ ...styles.reasonTag, background: "#14532d", color: "#86efac" }}>{r}</span>
                    ))}
                    {signal.composite.sell_reasons.map((r, i) => (
                      <span key={`s${i}`} style={{ ...styles.reasonTag, background: "#7f1d1d", color: "#fca5a5" }}>{r}</span>
                    ))}
                  </div>
                  <Row label="ADX" value={`${signal.composite.adx} (${signal.composite.is_trending ? "トレンド相場" : "レンジ相場"})`} />
                </>
              )}
              {!signal.crossover && (
                <>
                  <hr style={styles.divider} />
                  <Row label="状態" value="待機中（クロスなし）" highlight="#64748b" />
                  <Row label="AI 判定" value="— 判定なし" highlight="#64748b" />
                  <Row label="信頼度" value="0%" highlight="#64748b" />
                </>
              )}
              {signal.crossover && (
                <>
                  <hr style={styles.divider} />
                  <Row
                    label="AI 判定"
                    value={
                      signal.ai_reason?.includes("クールダウン") || signal.ai_reason?.includes("スキップ")
                        ? "⏸ 保留中"
                        : signal.ai_reason?.includes("クォータ")
                        ? "⚠️ 判定不能"
                        : signal.ai_valid === null
                        ? "⏳ 待機中"
                        : signal.ai_valid
                        ? "✅ 有効"
                        : "❌ ダマシ"
                    }
                    highlight={
                      signal.ai_reason?.includes("クールダウン") || signal.ai_reason?.includes("スキップ")
                        ? "#94a3b8"
                        : signal.ai_reason?.includes("クォータ")
                        ? "#f59e0b"
                        : signal.ai_valid === null
                        ? "#94a3b8"
                        : signal.ai_valid
                        ? "#22c55e"
                        : "#ef4444"
                    }
                  />
                  {signal.ai_confidence != null && <Row label="信頼度" value={`${signal.ai_confidence}%`} />}
                  <Row label="理由" value={signal.ai_reason ?? ""} />
                  {/* Gemini設定パネル（SL/TP提案・トレーリング設定） */}
                  {signal.ai_valid && (signal.ai_sl_suggestion != null || signal.ai_trailing_trigger != null) && (
                    <div style={{ background: "#0f2a1a", border: "1px solid #166534", borderRadius: 8, padding: "10px 12px", marginTop: 4 }}>
                      <div style={{ fontSize: 11, color: "#4ade80", fontWeight: "bold", marginBottom: 6 }}>🤖 Gemini設定（SL/TPオフ時に適用）</div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 12px", fontSize: 12 }}>
                        {signal.ai_sl_suggestion != null && (
                          <><span style={{ color: "#94a3b8" }}>初期SL</span><span style={{ color: "#fca5a5", fontWeight: "bold" }}>${signal.ai_sl_suggestion}</span></>
                        )}
                        {signal.ai_tp_suggestion != null && (
                          <><span style={{ color: "#94a3b8" }}>初期TP</span><span style={{ color: "#86efac", fontWeight: "bold" }}>${signal.ai_tp_suggestion}</span></>
                        )}
                        {signal.ai_trailing_trigger != null && (
                          <><span style={{ color: "#94a3b8" }}>トレイル開始</span><span style={{ color: "#38bdf8", fontWeight: "bold" }}>{signal.ai_trailing_trigger}$/oz</span></>
                        )}
                        {signal.ai_trailing_width != null && (
                          <><span style={{ color: "#94a3b8" }}>トレイル幅</span><span style={{ color: "#38bdf8", fontWeight: "bold" }}>{signal.ai_trailing_width}$/oz</span></>
                        )}
                      </div>
                    </div>
                  )}
                  {signal.ai_key_level && <Row label="注目水準" value={signal.ai_key_level} />}
                </>
              )}
              <p style={styles.timestamp}>{new Date(signal.generated_at).toLocaleString("ja-JP")}</p>
              {lastSocketReceived && (
                <p style={{ fontSize: 10, color: "#22c55e", textAlign: "right" as const, margin: "2px 0 0" }}>
                  ✅ 最終受信: {lastSocketReceived.toLocaleTimeString("ja-JP")}
                </p>
              )}
              {signal.crossover && signal.ai_valid && (
                <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                  {/* SEMI_AUTO: MT5自動注文ボタン */}
                  {tradingMode === "SEMI_AUTO" && (
                    <button
                      style={{ ...styles.btnEntry, background: signal.crossover === "UP_CROSS" ? "#22c55e" : "#ef4444", opacity: executing ? 0.6 : 1 }}
                      onClick={executeSemiAuto}
                      disabled={executing}
                    >
                      {executing ? "送信中..." : signal.crossover === "UP_CROSS" ? "✅ BUY 実行（MT5自動注文）" : "✅ SELL 実行（MT5自動注文）"}
                    </button>
                  )}
                  {/* 手動記録ボタン（MANUALまたはSEMI_AUTOで記録のみ） */}
                  {tradingMode !== "FULL_AUTO" && (
                    <button
                      style={{ ...styles.btnEntry, background: "#334155", fontSize: 13 }}
                      onClick={() => {
                        setEntryDirection(signal.crossover === "UP_CROSS" ? "BUY" : "SELL");
                        setEntryPrice(signal.latest_close?.toFixed(2) ?? "");
                        setEntryModalOpen(true);
                      }}
                    >
                      {signal.crossover === "UP_CROSS" ? "📝 BUY 手動記録" : "📝 SELL 手動記録"}
                    </button>
                  )}
                  {execMsg && (
                    <div style={{ textAlign: "center", fontWeight: "bold", fontSize: 14, color: execMsg.startsWith("✅") ? "#22c55e" : "#ef4444" }}>
                      {execMsg}
                    </div>
                  )}
                </div>
              )}
              {/* FULL_AUTO: 自動実行済みバッジ */}
              {signal.auto_executed && (
                <div style={{ ...styles.badge, background: "#14532d", marginTop: 8, display: "block" }}>
                  🤖 FULL_AUTO 自動実行済み
                </div>
              )}
            </div>
          ) : (
            <p style={styles.waiting}>シグナル待機中...</p>
          )}

          {/* S/R水準カード */}
          {srLevels.current_price > 0 && (
            <div style={{ ...styles.card, padding: "12px 14px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <h2 style={{ ...styles.cardTitle, margin: 0 }}>📐 S/R水準</h2>
                <span style={{ fontSize: 10, color: "#475569" }}>{srLevels.updated_at} 更新</span>
              </div>
              <div style={{ fontSize: 12, color: "#64748b", marginBottom: 6 }}>
                現在価格: <span style={{ color: "#f8fafc", fontWeight: "bold" }}>${srLevels.current_price.toFixed(2)}</span>
              </div>
              {srLevels.resistance.map((r, i) => (
                <div key={`r${i}`} style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", borderBottom: "1px solid #1e293b" }}>
                  <span style={{ color: "#f87171", fontSize: 12 }}>🔴 レジスタンス</span>
                  <span style={{ color: "#f87171", fontSize: 12, fontWeight: "bold" }}>
                    ${r.toFixed(2)} <span style={{ color: "#64748b", fontWeight: "normal" }}>(+{(r - srLevels.current_price).toFixed(2)})</span>
                  </span>
                </div>
              ))}
              {srLevels.broken_resistance.map((r, i) => (
                <div key={`br${i}`} style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", borderBottom: "1px solid #1e293b" }}>
                  <span style={{ color: "#22c55e", fontSize: 12 }}>✅ 旧レジ→サポート</span>
                  <span style={{ color: "#22c55e", fontSize: 12, fontWeight: "bold" }}>
                    ${r.toFixed(2)} <span style={{ color: "#64748b", fontWeight: "normal" }}>(-{(srLevels.current_price - r).toFixed(2)})</span>
                  </span>
                </div>
              ))}
              {srLevels.support.map((s, i) => (
                <div key={`s${i}`} style={{ display: "flex", justifyContent: "space-between", padding: "3px 0", borderBottom: "1px solid #1e293b" }}>
                  <span style={{ color: "#4ade80", fontSize: 12 }}>🟢 サポート</span>
                  <span style={{ color: "#4ade80", fontSize: 12, fontWeight: "bold" }}>
                    ${s.toFixed(2)} <span style={{ color: "#64748b", fontWeight: "normal" }}>(-{(srLevels.current_price - s).toFixed(2)})</span>
                  </span>
                </div>
              ))}
              {srLevels.resistance.length === 0 && srLevels.support.length === 0 && (
                <div style={{ fontSize: 11, color: "#475569" }}>S/R水準なし（EAからのデータ待ち）</div>
              )}
            </div>
          )}

          {history.length > 0 && (
            <div style={styles.card}>
              <h2 style={styles.cardTitle}>履歴（直近{history.length}件）</h2>
              {history.map((s, i) => (
                <div key={i} style={styles.historyRow}>
                  <span>{new Date(s.generated_at).toLocaleString("ja-JP")}</span>
                  <span style={{ color: s.crossover === "UP_CROSS" ? "#22c55e" : s.crossover === "DOWN_CROSS" ? "#ef4444" : "#aaa" }}>
                    {s.crossover ?? "なし"}
                  </span>
                  <span>{s.ai_confidence != null ? `${s.ai_confidence}%` : "-"}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* ===== アナリティクスタブ ===== */}
      {activeTab === "analytics" && (
        <>
          {/* 本日の統計 */}
          {todayStats && (
            <div style={styles.card}>
              <div style={styles.cardHeader}>
                <h2 style={styles.cardTitle}>📊 本日の成績</h2>
                <span style={styles.tfBadge}>{todayStats.date}</span>
              </div>
              <div style={styles.statsGrid}>
                <StatBox label="シグナル数" value={String(todayStats.total_signals)} />
                <StatBox label="取引数" value={String(todayStats.total_trades)} />
                <StatBox label="勝率" value={`${todayStats.win_rate}%`} color={todayStats.win_rate >= 60 ? "#22c55e" : "#f59e0b"} />
                <StatBox label="合計Pips" value={`${todayStats.total_pips > 0 ? "+" : ""}${todayStats.total_pips}`} color={todayStats.total_pips >= 0 ? "#22c55e" : "#ef4444"} />
                <StatBox label="勝" value={String(todayStats.win_count)} color="#22c55e" />
                <StatBox label="負" value={String(todayStats.loss_count)} color="#ef4444" />
              </div>
              {todayStats.confidence_avg && (
                <Row label="平均信頼度" value={`${todayStats.confidence_avg}%`} />
              )}
              {todayStats.open_trades > 0 && (
                <div style={{ ...styles.badge, background: "#f59e0b", marginTop: 8 }}>
                  オープン中: {todayStats.open_trades}件
                </div>
              )}
            </div>
          )}

          {/* 注文記録ボタン */}
          <button
            style={styles.btnEntryManual}
            onClick={() => {
              setEntryDirection("BUY");
              setEntryPrice(signal?.latest_close?.toFixed(2) ?? "");
              setEntryModalOpen(true);
            }}
          >
            ＋ 注文を手動記録
          </button>

          {/* Gemini実績ログ */}
          <div style={styles.card}>
            <div style={styles.cardHeader}>
              <h2 style={styles.cardTitle}>🤖 Gemini実績ログ</h2>
              <button style={styles.refreshBtn} onClick={fetchActionLog}>🔄</button>
            </div>
            {actionLogs.length === 0 ? (
              <p style={styles.waiting}>ログなし（エントリー・決済時に記録）</p>
            ) : (
              actionLogs.map((log, i) => {
                const msg = log.message;
                const isEntry = msg.includes("エントリー実行") || msg.includes("Gemini判定");
                const isClose = msg.includes("force_close") || msg.includes("決済実行");
                const isReason = msg.includes("判定理由");
                const color = isEntry ? "#22c55e" : isClose ? "#ef4444" : "#94a3b8";
                return (
                  <div key={i} style={{ borderBottom: "1px solid #334155", paddingBottom: 6, marginBottom: 6 }}>
                    <div style={{ fontSize: 10, color: "#475569", marginBottom: 2 }}>
                      {new Date(log.timestamp).toLocaleString("ja-JP")}
                    </div>
                    <div style={{ fontSize: 12, color, wordBreak: "break-all" as const }}>
                      {isReason ? <span style={{ color: "#94a3b8" }}>{msg}</span> : msg}
                    </div>
                  </div>
                );
              })
            )}
          </div>

          {/* トレード一覧 */}
          <div style={styles.card}>
            <div style={styles.cardHeader}>
              <h2 style={styles.cardTitle}>📋 トレード履歴</h2>
              <button style={styles.refreshBtn} onClick={() => { fetchTrades(); fetchTodayStats(); }}>🔄</button>
            </div>
            {trades.length === 0 ? (
              <p style={styles.waiting}>記録なし</p>
            ) : (
              trades.map(t => (
                <div key={t.id} style={styles.tradeRow}>
                  <div style={styles.tradeTop}>
                    <span style={{ color: t.direction === "BUY" ? "#22c55e" : "#ef4444", fontWeight: "bold" }}>
                      {t.direction === "BUY" ? "📈" : "📉"} {t.direction}
                    </span>
                    <span style={styles.tradeStatus(t.status)}>{statusLabel(t.status)}</span>
                  </div>
                  <div style={styles.tradeDetail}>
                    <span>エントリー: {t.entry_price}</span>
                    {t.exit_price && <span>決済: {t.exit_price}</span>}
                    {t.pips != null && (
                      <span style={{ color: t.pips >= 0 ? "#22c55e" : "#ef4444" }}>
                        {t.pips >= 0 ? "+" : ""}{t.pips}pips
                      </span>
                    )}
                  </div>
                  <div style={styles.tradeTime}>{new Date(t.entry_time).toLocaleString("ja-JP")}</div>
                  {t.status === "OPEN" && (
                    <button
                      style={styles.btnClose}
                      onClick={() => {
                        setCloseModalTrade(t);
                        setExitPrice(signal?.latest_close?.toFixed(2) ?? "");
                      }}
                    >
                      決済記録
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        </>
      )}

      {/* ===== 注文記録モーダル ===== */}
      {entryModalOpen && (
        <div style={styles.overlay}>
          <div style={styles.modal}>
            <h2 style={styles.modalTitle}>📝 注文記録</h2>
            <div style={styles.radioGroup}>
              <label style={styles.radioLabel}>
                <input type="radio" checked={entryDirection === "BUY"} onChange={() => setEntryDirection("BUY")} />
                <span style={{ color: "#22c55e" }}>📈 BUY（買い）</span>
              </label>
              <label style={styles.radioLabel}>
                <input type="radio" checked={entryDirection === "SELL"} onChange={() => setEntryDirection("SELL")} />
                <span style={{ color: "#ef4444" }}>📉 SELL（売り）</span>
              </label>
            </div>
            <div style={styles.inputGroup}>
              <label style={styles.inputLabel}>エントリー価格</label>
              <input
                style={styles.input}
                type="number"
                step="0.01"
                value={entryPrice}
                onChange={e => setEntryPrice(e.target.value)}
                placeholder="例: 2350.50"
              />
            </div>
            <div style={styles.modalBtns}>
              <button style={styles.btnSave} onClick={recordEntry} disabled={tradeLoading || !entryPrice}>
                {tradeLoading ? "記録中..." : "記録する"}
              </button>
              <button style={styles.btnCancel} onClick={() => { setEntryModalOpen(false); setEntryPrice(""); }}>
                キャンセル
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== ダッシュボードタブ ===== */}
      {activeTab === "dashboard" && (
        <StatusDashboard renderUrl={RENDER_URL} />
      )}

      {/* ===== 決済記録モーダル ===== */}
      {closeModalTrade && (
        <div style={styles.overlay}>
          <div style={styles.modal}>
            <h2 style={styles.modalTitle}>💰 決済記録</h2>
            <Row label="方向" value={closeModalTrade.direction} highlight={closeModalTrade.direction === "BUY" ? "#22c55e" : "#ef4444"} />
            <Row label="エントリー価格" value={closeModalTrade.entry_price} />
            <div style={styles.inputGroup}>
              <label style={styles.inputLabel}>決済価格</label>
              <input
                style={styles.input}
                type="number"
                step="0.01"
                value={exitPrice}
                onChange={e => setExitPrice(e.target.value)}
                placeholder="例: 2360.00"
              />
            </div>
            {exitPrice && (
              <div style={{ textAlign: "center", marginBottom: 12 }}>
                {(() => {
                  const ep = parseFloat(exitPrice);
                  const enp = Number(closeModalTrade.entry_price);
                  const pl = ((ep - enp) * (closeModalTrade.direction === "BUY" ? 1 : -1)).toFixed(2);
                  const pips = (parseFloat(pl) * 10).toFixed(1);
                  const positive = parseFloat(pl) >= 0;
                  return (
                    <span style={{ color: positive ? "#22c55e" : "#ef4444", fontWeight: "bold", fontSize: 16 }}>
                      {positive ? "✅ +" : "❌ "}{pl}$ ({positive ? "+" : ""}{pips}pips)
                    </span>
                  );
                })()}
              </div>
            )}
            <div style={styles.modalBtns}>
              <button style={styles.btnSave} onClick={recordExit} disabled={tradeLoading || !exitPrice}>
                {tradeLoading ? "記録中..." : "決済記録"}
              </button>
              <button style={styles.btnCancel} onClick={() => { setCloseModalTrade(null); setExitPrice(""); }}>
                キャンセル
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 設定パネル */}
      {settingsOpen && (
        <div style={styles.overlay}>
          <div style={styles.settingsPanel}>
            <h2 style={styles.settingsTitle}>⚙️ 設定</h2>

            {/* 自動化モード（最上部に配置） */}
            <div style={styles.settingsSection}>
              <h3 style={styles.settingsSectionTitle}>🤖 自動化モード</h3>
              <div style={styles.radioGroup}>
                <label style={styles.radioLabel}>
                  <input type="radio" name="tmode2" checked={tradingMode === "MANUAL"} onChange={() => setTradingMode("MANUAL")} />
                  <span>🔵 手動（シグナル表示のみ）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="tmode2" checked={tradingMode === "SEMI_AUTO"} onChange={() => setTradingMode("SEMI_AUTO")} />
                  <span>🟡 セミ自動（ボタンでMT5注文）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="tmode2" checked={tradingMode === "FULL_AUTO"} onChange={() => setTradingMode("FULL_AUTO")} />
                  <span>🟢 半自動（Geminiエントリー＋SL/トレーリング自動決済）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="tmode2" checked={tradingMode === "AI_CLOSE_MODE"} onChange={() => setTradingMode("AI_CLOSE_MODE")} />
                  <span>🔴 全自動（Geminiエントリー＋Gemini決済判断）</span>
                </label>
              </div>
              {tradingMode === "FULL_AUTO" && (
                <div style={{ marginTop: 10, padding: "8px 12px", background: "#14532d", borderRadius: 6, fontSize: 12, color: "#86efac" }}>
                  ✅ <b>Gemini判定</b>でエントリー（信頼度 {autoThreshold}% 以上）<br/>
                  ✅ 決済はアプリ設定の<b>初期SL＋トレーリング</b>が自動管理<br/>
                  <div style={{ marginTop: 8 }}>
                    <label style={styles.inputLabel}>エントリー閾値: <b style={{ color: "#22c55e" }}>{autoThreshold}%</b></label>
                    <input type="range" min={50} max={95} step={5} value={autoThreshold}
                      onChange={e => setAutoThreshold(parseInt(e.target.value))}
                      style={{ width: "100%", accentColor: "#22c55e" }} />
                  </div>
                </div>
              )}
              {tradingMode === "AI_CLOSE_MODE" && (
                <div style={{ marginTop: 10, padding: "8px 12px", background: "#7f1d1d", borderRadius: 6, fontSize: 12, color: "#fca5a5" }}>
                  🤖 <b>Gemini判定</b>でエントリー（信頼度 {entryThreshold}% 以上）<br/>
                  🤖 <b>クロス発生ごと</b>にGemini決済判断（60秒クールダウン）<br/>
                  🤖 決済指示はEAへ自動送信（force_close）
                  <div style={{ marginTop: 8 }}>
                    <label style={styles.inputLabel}>エントリー閾値: <b style={{ color: "#fca5a5" }}>{entryThreshold}%</b></label>
                    <input type="range" min={50} max={95} step={5} value={entryThreshold}
                      onChange={e => setEntryThreshold(parseInt(e.target.value))}
                      style={{ width: "100%", accentColor: "#ef4444" }} />
                  </div>
                </div>
              )}
            </div>
            <hr style={styles.divider} />

            <div style={styles.settingsSection}>
              <h3 style={styles.settingsSectionTitle}>📊 時間足</h3>
              <div style={styles.radioGroup}>
                {TIMEFRAMES.map(t => (
                  <label key={t} style={styles.radioLabel}>
                    <input type="radio" name="tf" checked={tf === t} onChange={() => setTf(t)} />
                    <span>{t}分足{t <= 15 ? " 🧪" : " 🚀"}</span>
                  </label>
                ))}
              </div>
            </div>

            <div style={styles.settingsSection}>
              <h3 style={styles.settingsSectionTitle}>🎯 運用モード</h3>
              <div style={styles.radioGroup}>
                <label style={styles.radioLabel}>
                  <input type="radio" name="mode" checked={mode === "TEST"} onChange={() => setMode("TEST")} />
                  <span>🧪 テスト（AI省略・高速）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="mode" checked={mode === "PRODUCTION"} onChange={() => setMode("PRODUCTION")} />
                  <span>🚀 本運用（AI使用）</span>
                </label>
              </div>
            </div>

            <div style={styles.settingsSection}>
              <h3 style={styles.settingsSectionTitle}>📈 クロスオーバー方式</h3>
              <div style={styles.radioGroup}>
                <label style={styles.radioLabel}>
                  <input type="radio" name="crossover" checked={crossoverMode === "COMPOSITE"} onChange={() => setCrossoverMode("COMPOSITE")} />
                  <span>⭐ COMPOSITE（推奨：7指標複合スコア）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="crossover" checked={crossoverMode === "RSI"} onChange={() => setCrossoverMode("RSI")} />
                  <span>RSIシグナルクロス（RSIと移動平均）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="crossover" checked={crossoverMode === "MACD"} onChange={() => setCrossoverMode("MACD")} />
                  <span>MACDクロス（MACDとシグナルライン）</span>
                </label>
                <label style={styles.radioLabel}>
                  <input type="radio" name="crossover" checked={crossoverMode === "RSI_MACD"} onChange={() => setCrossoverMode("RSI_MACD")} />
                  <span>RSI_MACDクロス（RSIがMACDシグナルを上抜け/下抜け）</span>
                </label>
              </div>
            </div>

            <div style={styles.infoBox}>
              <p style={{ margin: "0 0 6px", fontSize: 12 }}>
                <b>⭐ COMPOSITE:</b> EMA×2 + MACD + RSI + ストキャスティクス + BB + ADX の7指標を点数化。5点以上で発火。GeminiがSL/TP提案。
              </p>
              <p style={{ margin: "0 0 6px", fontSize: 12 }}>
                <b>RSI:</b> RSI(14)がその9SMAを上抜け/下抜けを検出。
              </p>
              <p style={{ margin: "0 0 6px", fontSize: 12 }}>
                <b>MACD:</b> チャート下段の赤線(MACD)と青線(シグナル)のクロスを検出。
              </p>
              <p style={{ margin: 0, fontSize: 12 }}>
                <b>本運用:</b> 30〜60分足推奨。AIが信頼度を判定。
              </p>
            </div>


            {/* レジスタンス・サポート判断トグル */}
            <div style={{ background: "#1e293b", border: `1px solid ${useKeyLevels ? "#334155" : "#f59e0b"}`, borderRadius: 8, padding: "10px 14px", marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div>
                <div style={{ fontSize: 13, color: "#cbd5e1", fontWeight: "bold" }}>📐 S/R水準をGemini判断に含める</div>
                <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                  {useKeyLevels ? "ON: レジスタンス・サポート近接を考慮" : "OFF: 以前のロジック（テクニカルのみ）"}
                </div>
              </div>
              <label style={{ position: "relative", display: "inline-block", width: 48, height: 26, cursor: "pointer" }}>
                <input type="checkbox" checked={useKeyLevels} onChange={e => setUseKeyLevels(e.target.checked)} style={{ opacity: 0, width: 0, height: 0 }} />
                <span style={{
                  position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                  background: useKeyLevels ? "#22c55e" : "#475569",
                  borderRadius: 26, transition: "0.3s"
                }} />
                <span style={{
                  position: "absolute", top: 3, left: useKeyLevels ? 25 : 3, width: 20, height: 20,
                  background: "#fff", borderRadius: "50%", transition: "0.3s"
                }} />
              </label>
            </div>

            {/* レンジ逆張りモード トグル */}
            <div style={{ background: "#1e293b", border: `1px solid ${useRangeMode ? "#f59e0b" : "#334155"}`, borderRadius: 8, padding: "10px 14px", marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div>
                <div style={{ fontSize: 13, color: "#cbd5e1", fontWeight: "bold" }}>📊 レンジ逆張りモード</div>
                <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                  {useRangeMode ? "ON: S/R間の反転エントリー（ADX<25）" : "OFF: トレンドフォロー専用"}
                </div>
              </div>
              <label style={{ position: "relative", display: "inline-block", width: 48, height: 26, cursor: "pointer" }}>
                <input type="checkbox" checked={useRangeMode} onChange={e => setUseRangeMode(e.target.checked)} style={{ opacity: 0, width: 0, height: 0 }} />
                <span style={{
                  position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                  background: useRangeMode ? "#f59e0b" : "#475569",
                  borderRadius: 26, transition: "0.3s"
                }} />
                <span style={{
                  position: "absolute", top: 3, left: useRangeMode ? 25 : 3, width: 20, height: 20,
                  background: "#fff", borderRadius: "50%", transition: "0.3s"
                }} />
              </label>
            </div>

            {/* ハイブリッドSL/TP設定 */}
            <div style={styles.settingsSection}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
                <h3 style={{ ...styles.settingsSectionTitle, margin: 0 }}>🛡️ SL / TP 設定</h3>
                <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <span style={{ fontSize: 12, color: hybridSlConfig.enabled ? "#22c55e" : "#64748b" }}>
                    {hybridSlConfig.enabled ? "ON（設定値優先）" : "OFF（Gemini任せ）"}
                  </span>
                  <span style={{ position: "relative", display: "inline-block", width: 44, height: 24 }}>
                    <input type="checkbox" checked={hybridSlConfig.enabled}
                      onChange={e => setHybridSlConfig(c => ({ ...c, enabled: e.target.checked }))}
                      style={{ opacity: 0, width: 0, height: 0 }} />
                    <span style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                      background: hybridSlConfig.enabled ? "#22c55e" : "#475569",
                      borderRadius: 24, transition: "0.3s" }} />
                    <span style={{ position: "absolute", top: 2, left: hybridSlConfig.enabled ? 22 : 2,
                      width: 20, height: 20, background: "#fff", borderRadius: "50%", transition: "0.3s" }} />
                  </span>
                </label>
              </div>
              {!hybridSlConfig.enabled && (
                <div style={{ padding: "8px 10px", background: "#1e3a5f", borderRadius: 6, fontSize: 12, color: "#93c5fd", marginBottom: 10 }}>
                  OFFのとき: SL/TPはGemini提案値を使用。提案がない場合はSL=2$/oz・TP=4$/oz固定。
                </div>
              )}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, fontSize: 13 }}>
                <div>
                  <label style={{ display: "block", marginBottom: 4, color: "#cbd5e1" }}>初期SL（$/oz）</label>
                  <input type="number" step="0.5" min="0.5" max="50"
                    value={hybridSlConfig.initial_sl_price}
                    onChange={e => { const v = parseFloat(e.target.value); if (!isNaN(v)) setHybridSlConfig(c => ({ ...c, initial_sl_price: v })); }}
                    style={{ width: "100%", padding: "6px", background: "#0f172a", color: "#f1f5f9", border: "1px solid #475569", borderRadius: 4, boxSizing: "border-box" }} />
                  <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94a3b8" }}>エントリーから何$/oz離す</p>
                </div>
                <div>
                  <label style={{ display: "block", marginBottom: 4, color: "#10b981" }}>初期TP（$/oz）</label>
                  <input type="number" step="0.5" min="0.5" max="200"
                    value={hybridSlConfig.initial_tp_price ?? 15}
                    onChange={e => { const v = parseFloat(e.target.value); if (!isNaN(v)) setHybridSlConfig(c => ({ ...c, initial_tp_price: v })); }}
                    style={{ width: "100%", padding: "6px", background: "#0f172a", color: "#10b981", border: "1px solid #10b981", borderRadius: 4, boxSizing: "border-box" }} />
                  <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94a3b8" }}>エントリーから何$/oz利確</p>
                </div>
                <div>
                  <label style={{ display: "block", marginBottom: 4, color: "#cbd5e1" }}>トレーリング開始（$/oz）</label>
                  <input type="number" step="0.5" min="0.5" max="50"
                    value={hybridSlConfig.trailing_trigger_price}
                    onChange={e => { const v = parseFloat(e.target.value); if (!isNaN(v)) setHybridSlConfig(c => ({ ...c, trailing_trigger_price: v })); }}
                    style={{ width: "100%", padding: "6px", background: "#0f172a", color: "#f1f5f9", border: "1px solid #475569", borderRadius: 4, boxSizing: "border-box" }} />
                  <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94a3b8" }}>有利方向に何$/oz動いたら開始</p>
                </div>
                <div>
                  <label style={{ display: "block", marginBottom: 4, color: "#cbd5e1" }}>トレーリングSL幅（$/oz）</label>
                  <input type="number" step="0.5" min="0.5" max="20"
                    value={hybridSlConfig.trailing_sl_price}
                    onChange={e => { const v = parseFloat(e.target.value); if (!isNaN(v)) setHybridSlConfig(c => ({ ...c, trailing_sl_price: v })); }}
                    style={{ width: "100%", padding: "6px", background: "#0f172a", color: "#f1f5f9", border: "1px solid #475569", borderRadius: 4, boxSizing: "border-box" }} />
                  <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94a3b8" }}>最高値/最安値から何$/oz戻したらSL</p>
                </div>
              </div>
            </div>

            {/* 再エントリー設定 */}
            <div style={{ background: "#1e293b", borderRadius: 10, padding: "12px 14px", marginBottom: 10, marginTop: 10 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13, color: "#f1f5f9", fontWeight: "bold" }}>🔄 クロス継続再エントリー</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3 }}>
                    FULL_AUTO限定。ポジションなし＋クロス継続中に5分ごとに再エントリー
                  </div>
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <span style={{ fontSize: 12, color: reentryEnabled ? "#22c55e" : "#64748b" }}>
                    {reentryEnabled ? "ON" : "OFF"}
                  </span>
                  <span style={{ position: "relative", display: "inline-block", width: 44, height: 24 }}>
                    <input type="checkbox" checked={reentryEnabled}
                      onChange={e => setReentryEnabled(e.target.checked)}
                      style={{ opacity: 0, width: 0, height: 0 }} />
                    <span style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                      background: reentryEnabled ? "#22c55e" : "#475569",
                      borderRadius: 24, transition: "0.3s" }} />
                    <span style={{ position: "absolute", top: 2, left: reentryEnabled ? 22 : 2,
                      width: 20, height: 20, background: "#fff", borderRadius: "50%", transition: "0.3s" }} />
                  </span>
                </label>
              </div>
              {reentryEnabled && (
                <div style={{ padding: "8px 10px", background: "#1a3a1a", borderRadius: 6, fontSize: 11, color: "#86efac", marginTop: 8 }}>
                  ⚠️ 同方向クロスが続く限り5分ごとにエントリーを試みます。相場急変時は注意してください。
                </div>
              )}
            </div>

            {/* 5分AI決済監視 */}
            <div style={{ background: "#1e293b", borderRadius: 10, padding: "12px 14px", marginBottom: 10 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13, color: "#f1f5f9", fontWeight: "bold" }}>🤖 5分AI決済監視</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3 }}>
                    保有中ポジションをGeminiが5分ごとに判断。危険と判断したら決済
                  </div>
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <span style={{ fontSize: 12, color: aiExitEnabled ? "#f87171" : "#64748b" }}>
                    {aiExitEnabled ? "ON" : "OFF"}
                  </span>
                  <span style={{ position: "relative", display: "inline-block", width: 44, height: 24 }}>
                    <input type="checkbox" checked={aiExitEnabled}
                      onChange={e => setAiExitEnabled(e.target.checked)}
                      style={{ opacity: 0, width: 0, height: 0 }} />
                    <span style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                      background: aiExitEnabled ? "#ef4444" : "#475569",
                      borderRadius: 24, transition: "0.3s" }} />
                    <span style={{ position: "absolute", top: 2, left: aiExitEnabled ? 22 : 2,
                      width: 20, height: 20, background: "#fff", borderRadius: "50%", transition: "0.3s" }} />
                  </span>
                </label>
              </div>
              {aiExitEnabled && (
                <div style={{ padding: "8px 10px", background: "#3a1a1a", borderRadius: 6, fontSize: 11, color: "#fca5a5", marginTop: 8 }}>
                  ⚠️ AI_CLOSE_MODE（15分監視）と併用すると頻繁にGeminiが呼ばれます。Geminiクォータに注意。
                </div>
              )}
            </div>

            {/* RSIフィルター */}
            <div style={{ background: "#1e293b", borderRadius: 10, padding: "12px 14px", marginBottom: 10 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13, color: "#f1f5f9", fontWeight: "bold" }}>📉 RSIフィルター</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3 }}>
                    RSI&lt;35のシグナルをスキップ（勝率+3.3%実証）
                  </div>
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <span style={{ fontSize: 12, color: rsiFilterEnabled ? "#22c55e" : "#64748b" }}>
                    {rsiFilterEnabled ? "ON" : "OFF"}
                  </span>
                  <span style={{ position: "relative", display: "inline-block", width: 44, height: 24 }}>
                    <input type="checkbox" checked={rsiFilterEnabled}
                      onChange={e => setRsiFilterEnabled(e.target.checked)}
                      style={{ opacity: 0, width: 0, height: 0 }} />
                    <span style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                      background: rsiFilterEnabled ? "#22c55e" : "#475569",
                      borderRadius: 24, transition: "0.3s" }} />
                    <span style={{ position: "absolute", top: 2, left: rsiFilterEnabled ? 22 : 2,
                      width: 20, height: 20, background: "#fff", borderRadius: "50%", transition: "0.3s" }} />
                  </span>
                </label>
              </div>
            </div>

            {/* クロス転換モード */}
            <div style={{ background: crossFlipEnabled ? "#0f2a1a" : "#1e293b", borderRadius: 10, padding: "12px 14px", marginBottom: 10, border: crossFlipEnabled ? "1px solid #22c55e" : "1px solid transparent" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13, color: "#f1f5f9", fontWeight: "bold" }}>🔀 クロス転換モード（長期）</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3 }}>
                    スコア差≥3 + DI方向一致で別ポジション追加<br/>
                    反対クロスまで保持・SL5$固定（Magic:20261002）
                  </div>
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                  <span style={{ fontSize: 12, color: crossFlipEnabled ? "#22c55e" : "#64748b" }}>
                    {crossFlipEnabled ? "ON" : "OFF"}
                  </span>
                  <span style={{ position: "relative", display: "inline-block", width: 44, height: 24 }}>
                    <input type="checkbox" checked={crossFlipEnabled}
                      onChange={e => setCrossFlipEnabled(e.target.checked)}
                      style={{ opacity: 0, width: 0, height: 0 }} />
                    <span style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0,
                      background: crossFlipEnabled ? "#22c55e" : "#475569",
                      borderRadius: 24, transition: "0.3s" }} />
                    <span style={{ position: "absolute", top: 2, left: crossFlipEnabled ? 22 : 2,
                      width: 20, height: 20, background: "#fff", borderRadius: "50%", transition: "0.3s" }} />
                  </span>
                </label>
              </div>
              {/* ロット設定（常に表示） */}
              <div style={{ marginTop: 10, display: "flex", gap: 10 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }}>トレーリング ロット</div>
                  <input
                    type="number" step="0.01" min="0.01" value={lotTrailing}
                    onChange={e => setLotTrailing(e.target.value)}
                    style={{ width: "100%", background: "#0f172a", color: "#f1f5f9", border: "1px solid #334155",
                      borderRadius: 6, padding: "6px 8px", fontSize: 13, boxSizing: "border-box" }}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }}>クロス転換 ロット</div>
                  <input
                    type="number" step="0.01" min="0.01" value={lotCrossFlip}
                    onChange={e => setLotCrossFlip(e.target.value)}
                    style={{ width: "100%", background: "#0f172a", color: "#f1f5f9", border: "1px solid #334155",
                      borderRadius: 6, padding: "6px 8px", fontSize: 13, boxSizing: "border-box" }}
                  />
                </div>
              </div>
              {crossFlipEnabled && (
                <div style={{ marginTop: 8, padding: "6px 8px", background: "#052e16", borderRadius: 6, fontSize: 11, color: "#86efac" }}>
                  ✅ 有効中: トレーリング{lotTrailing}lot + クロス転換{lotCrossFlip}lot で同時運用
                </div>
              )}
            </div>

            <hr style={styles.divider} />

            {saveMsg && <div style={styles.saveMsg}>{saveMsg}</div>}

            {/* 振動設定 */}
            <div style={{ background: "#1e293b", borderRadius: 10, padding: "12px 14px", marginBottom: 10 }}>
              <div style={{ fontSize: 13, color: "#94a3b8", marginBottom: 10 }}>📳 振動カスタム設定</div>

              {/* 振動時間 */}
              <div style={{ marginBottom: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, color: "#cbd5e1", marginBottom: 4 }}>
                  <span>振動時間</span><span style={{ color: "#38bdf8", fontWeight: "bold" }}>{vibDuration}ms</span>
                </div>
                <input type="range" min={100} max={2000} step={50} value={vibDuration}
                  onChange={e => setVibDuration(Number(e.target.value))}
                  style={{ width: "100%", accentColor: "#3b82f6" }} />
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#475569" }}>
                  <span>100ms</span><span>2000ms</span>
                </div>
              </div>

              {/* 回数 */}
              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 13, color: "#cbd5e1", marginBottom: 6 }}>回数</div>
                <div style={{ display: "flex", gap: 6 }}>
                  {[1,2,3,4,5].map(n => (
                    <button key={n} onClick={() => setVibCount(n)}
                      style={{ flex: 1, padding: "7px 0", border: "none", borderRadius: 8, fontSize: 14, cursor: "pointer",
                        background: vibCount === n ? "#3b82f6" : "#334155",
                        color: vibCount === n ? "#fff" : "#94a3b8",
                        fontWeight: vibCount === n ? "bold" : "normal" }}>
                      {n}
                    </button>
                  ))}
                </div>
              </div>

              {/* 間隔（2回以上の場合のみ） */}
              {vibCount > 1 && (
                <div style={{ marginBottom: 10 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, color: "#cbd5e1", marginBottom: 4 }}>
                    <span>振動の間隔</span><span style={{ color: "#38bdf8", fontWeight: "bold" }}>{vibGap}ms</span>
                  </div>
                  <input type="range" min={50} max={500} step={50} value={vibGap}
                    onChange={e => setVibGap(Number(e.target.value))}
                    style={{ width: "100%", accentColor: "#3b82f6" }} />
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#475569" }}>
                    <span>50ms</span><span>500ms</span>
                  </div>
                </div>
              )}

              {/* パターンプレビュー */}
              <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 10, height: 20 }}>
                {Array.from({ length: vibCount }).map((_, i) => (
                  <div key={i} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <div style={{ height: 16, background: "#f59e0b", borderRadius: 3,
                      width: Math.max(8, Math.round(vibDuration / 50)) + "px" }} />
                    {i < vibCount - 1 && <div style={{ height: 16, width: Math.max(4, Math.round(vibGap / 30)) + "px" }} />}
                  </div>
                ))}
              </div>

              {/* テストボタン */}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  style={{ flex: 1, padding: "10px", background: "#0f172a", color: "#7dd3fc", border: "1px solid #334155", borderRadius: 8, fontSize: 13, cursor: "pointer" }}
                  onClick={() => doVibrate(vibDuration, vibCount, vibGap)}
                >
                  📳 振動テスト
                </button>
                <button
                  style={{ flex: 1, padding: "10px", background: "#1e40af", color: "#fff", border: "none", borderRadius: 8, fontSize: 13, cursor: "pointer" }}
                  onClick={async () => {
                    doVibrate(vibDuration, vibCount, vibGap);
                    try {
                      await LocalNotifications.schedule({
                        notifications: [{
                          id: 99999,
                          title: "🔔 テスト通知",
                          body: `振動 ${vibDuration}ms × ${vibCount}回`,
                          schedule: { at: new Date(Date.now() + 300) },
                          channelId: "gold-trade-v3",
                          sound: "default",
                        }],
                      });
                    } catch (e) { alert("通知エラー: " + e); }
                  }}
                >
                  🔔 通知テスト
                </button>
              </div>
            </div>

            <div style={styles.settingsBtns}>
              <button style={styles.btnSave} onClick={saveSettings} disabled={saving}>
                {saving ? "保存中..." : "保存"}
              </button>
              <button style={styles.btnCancel} onClick={() => setSettingsOpen(false)}>
                キャンセル
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ==================== サブコンポーネント ====================
function Row({ label, value, highlight }: { label: string; value: string | number; highlight?: string }) {
  return (
    <div style={styles.row}>
      <span style={styles.label}>{label}</span>
      <span style={{ ...styles.value, color: highlight ?? "#f1f5f9" }}>{value}</span>
    </div>
  );
}

function StatBox({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={styles.statBox}>
      <div style={{ ...styles.statValue, color: color ?? "#f1f5f9" }}>{value}</div>
      <div style={styles.statLabel}>{label}</div>
    </div>
  );
}

function statusLabel(status: Trade["status"]): string {
  switch (status) {
    case "OPEN": return "🔵 OPEN";
    case "CLOSED_PROFIT": return "✅ 利益";
    case "CLOSED_LOSS": return "❌ 損失";
    case "CANCELLED": return "⚪ キャンセル";
  }
}

// ==================== スタイル ====================
const styles: Record<string, any> = {
  container: { maxWidth: 480, margin: "0 auto", padding: "16px", fontFamily: "sans-serif", background: "#0f172a", minHeight: "100vh", color: "#f1f5f9" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 },
  title: { fontSize: 22, margin: 0 },
  settingsBtn: { background: "none", border: "none", fontSize: 24, cursor: "pointer", color: "#94a3b8", padding: "4px 8px" },
  badge: { display: "inline-block", padding: "4px 12px", borderRadius: 99, fontSize: 13, marginBottom: 16, color: "#fff" },
  tabBar: { display: "flex", gap: 8, marginBottom: 16 },
  tab: { flex: 1, padding: "10px 0", background: "#1e293b", border: "none", borderRadius: 8, color: "#94a3b8", fontSize: 14, cursor: "pointer", fontWeight: "bold" },
  tabActive: { background: "#334155", color: "#f1f5f9", borderBottom: "2px solid #22c55e" },
  card: { background: "#1e293b", borderRadius: 12, padding: 16, marginBottom: 16 },
  cardHeader: { display: "flex", alignItems: "center", gap: 8, marginBottom: 12, flexWrap: "wrap" as const },
  cardTitle: { margin: 0, fontSize: 16, color: "#94a3b8" },
  testBadge: { fontSize: 11, background: "#854d0e", color: "#fef08a", padding: "2px 6px", borderRadius: 4 },
  tfBadge: { fontSize: 11, background: "#1e3a5f", color: "#93c5fd", padding: "2px 6px", borderRadius: 4 },
  modeBadge: { fontSize: 11, background: "#3b1f5f", color: "#c4b5fd", padding: "2px 6px", borderRadius: 4 },
  row: { display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 14 },
  label: { color: "#94a3b8" },
  value: { fontWeight: "bold" },
  divider: { border: "none", borderTop: "1px solid #334155", margin: "12px 0" },
  timestamp: { fontSize: 11, color: "#64748b", textAlign: "right", margin: "8px 0 0" },
  waiting: { textAlign: "center", color: "#64748b", marginTop: 20, padding: 12 },
  historyRow: { display: "flex", justifyContent: "space-between", fontSize: 12, padding: "6px 0", borderBottom: "1px solid #1e293b" },
  statsGrid: { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginBottom: 12 },
  statBox: { background: "#0f172a", borderRadius: 8, padding: "10px 6px", textAlign: "center" as const },
  statValue: { fontSize: 18, fontWeight: "bold", marginBottom: 4 },
  statLabel: { fontSize: 11, color: "#64748b" },
  tradeRow: { background: "#0f172a", borderRadius: 8, padding: 12, marginBottom: 8 },
  tradeTop: { display: "flex", justifyContent: "space-between", marginBottom: 6 },
  tradeDetail: { display: "flex", gap: 12, fontSize: 13, marginBottom: 4, flexWrap: "wrap" as const },
  tradeTime: { fontSize: 11, color: "#64748b", marginBottom: 6 },
  tradeStatus: (status: string) => ({
    fontSize: 12, padding: "2px 8px", borderRadius: 4,
    background: status === "OPEN" ? "#1e3a5f" : status === "CLOSED_PROFIT" ? "#14532d" : status === "CLOSED_LOSS" ? "#7f1d1d" : "#374151",
    color: status === "OPEN" ? "#93c5fd" : status === "CLOSED_PROFIT" ? "#86efac" : status === "CLOSED_LOSS" ? "#fca5a5" : "#9ca3af",
  }),
  btnEntry: { width: "100%", marginTop: 12, padding: "10px", background: "#22c55e", color: "#fff", border: "none", borderRadius: 8, fontSize: 14, fontWeight: "bold", cursor: "pointer" },
  btnEntryManual: { width: "100%", marginBottom: 12, padding: "12px", background: "#334155", color: "#f1f5f9", border: "2px solid #475569", borderRadius: 8, fontSize: 14, fontWeight: "bold", cursor: "pointer" },
  btnClose: { width: "100%", padding: "8px", background: "#f59e0b", color: "#fff", border: "none", borderRadius: 6, fontSize: 13, fontWeight: "bold", cursor: "pointer", marginTop: 4 },
  refreshBtn: { background: "none", border: "none", fontSize: 16, cursor: "pointer", color: "#94a3b8", marginLeft: "auto" },
  overlay: { position: "fixed", top: 0, left: 0, right: 0, bottom: 0, background: "rgba(0,0,0,0.75)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  modal: { background: "#1e293b", borderRadius: 16, padding: 24, width: "90%", maxWidth: 380, color: "#f1f5f9" },
  modalTitle: { margin: "0 0 20px", fontSize: 18, textAlign: "center" },
  modalBtns: { display: "flex", gap: 12, marginTop: 16 },
  inputGroup: { marginBottom: 16 },
  inputLabel: { display: "block", fontSize: 13, color: "#94a3b8", marginBottom: 6 },
  input: { width: "100%", padding: "10px 12px", background: "#0f172a", border: "1px solid #334155", borderRadius: 8, color: "#f1f5f9", fontSize: 16, boxSizing: "border-box" as const },
  settingsPanel: { background: "#1e293b", borderRadius: 16, padding: 24, width: "90%", maxWidth: 380, color: "#f1f5f9", maxHeight: "90vh", overflowY: "auto" as const },
  settingsTitle: { margin: "0 0 20px", fontSize: 20, textAlign: "center" },
  settingsSection: { marginBottom: 20 },
  settingsSectionTitle: { fontSize: 14, color: "#64b5f6", margin: "0 0 10px" },
  radioGroup: { display: "flex", flexDirection: "column", gap: 10 },
  radioLabel: { display: "flex", alignItems: "center", gap: 10, cursor: "pointer", fontSize: 14 },
  infoBox: { background: "rgba(0,0,0,0.3)", borderLeft: "4px solid #64b5f6", padding: "10px 12px", borderRadius: 4, marginBottom: 20 },
  saveMsg: { background: "#166534", color: "#86efac", padding: "10px", borderRadius: 8, textAlign: "center", marginBottom: 12, fontSize: 14 },
  settingsBtns: { display: "flex", gap: 12 },
  btnSave: { flex: 1, padding: "12px", background: "#22c55e", color: "#fff", border: "none", borderRadius: 8, fontSize: 14, fontWeight: "bold", cursor: "pointer" },
  btnCancel: { flex: 1, padding: "12px", background: "#ef4444", color: "#fff", border: "none", borderRadius: 8, fontSize: 14, fontWeight: "bold", cursor: "pointer" },
  scoreBar: { display: "flex", alignItems: "center", gap: 8, marginBottom: 8, fontSize: 12, fontWeight: "bold" },
  scoreTrack: { flex: 1, height: 8, background: "#334155", borderRadius: 4, overflow: "hidden" },
  scoreFill: { height: "100%", borderRadius: 4 },
  reasonTags: { display: "flex", flexWrap: "wrap" as const, gap: 4, marginBottom: 8 },
  reasonTag: { fontSize: 10, background: "#1e3a5f", color: "#93c5fd", padding: "2px 6px", borderRadius: 4 },
};
