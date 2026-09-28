import { useEffect, useState } from "react";
import { io, Socket } from "socket.io-client";
import { LocalNotifications } from "@capacitor/local-notifications";
import { PushNotifications } from "@capacitor/push-notifications";

// ==================== 型定義 ====================
interface Signal {
  crossover: "UP_CROSS" | "DOWN_CROSS" | null;
  rsi: number;
  signal_line: number;
  main_line?: number;
  latest_close: number;
  ai_valid: boolean | null;
  ai_confidence: number | null;
  ai_reason: string | null;
  generated_at: string;
  timeframe?: number;
  test_mode?: boolean;
  crossover_mode?: string;
  db_id?: number;
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
const RENDER_URL = import.meta.env.VITE_RENDER_URL ?? "https://ai-trading-system-81jb.onrender.com";
const TIMEFRAMES = [1, 5, 15, 30, 60] as const;

// ==================== メインコンポーネント ====================
export default function App() {
  const [signal, setSignal] = useState<Signal | null>(null);
  const [history, setHistory] = useState<Signal[]>([]);
  const [connected, setConnected] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"signal" | "analytics">("signal");
  const [trades, setTrades] = useState<Trade[]>([]);
  const [todayStats, setTodayStats] = useState<TodayStats | null>(null);
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
  const [crossoverMode, setCrossoverMode] = useState<"RSI" | "MACD" | "RSI_MACD">(() => {
    try { return (localStorage.getItem("gt_crossover") as any) ?? "RSI"; } catch { return "RSI"; }
  });
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

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

  useEffect(() => {
    // FCMプッシュ通知の登録
    PushNotifications.requestPermissions().then(result => {
      if (result.receive === "granted") PushNotifications.register();
    });
    PushNotifications.addListener("registration", async (token) => {
      try {
        await fetch(`${RENDER_URL}/register-token`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: token.value }),
        });
      } catch (_) {}
    });

    LocalNotifications.requestPermissions();
    LocalNotifications.createChannel({
      id: "gold-signal",
      name: "GOLDシグナル通知",
      importance: 5,
      vibration: true,
      sound: "default",
      description: "GOLDトレードシグナルの通知",
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
      try {
        const savedTf = parseInt(localStorage.getItem("gt_tf") ?? "30");
        const savedMode = (localStorage.getItem("gt_mode") ?? "PRODUCTION") as "PRODUCTION" | "TEST";
        const savedCrossover = (localStorage.getItem("gt_crossover") ?? "RSI") as "RSI" | "MACD" | "RSI_MACD";

        await Promise.all([
          fetch(`${RENDER_URL}/api/settings/timeframe`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timeframe: savedTf }),
          }),
          fetch(`${RENDER_URL}/api/settings/mode`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: savedMode }),
          }),
          fetch(`${RENDER_URL}/api/settings/crossover`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ crossover_mode: savedCrossover }),
          }),
        ]);

        setTf(savedTf);
        setMode(savedMode);
        setCrossoverMode(savedCrossover);

        const sigRes = await fetch(`${RENDER_URL}/latest-signal`);
        const data = await sigRes.json();
        if (data && data.rsi) {
          const s: Signal = {
            crossover: data.crossover,
            rsi: data.rsi,
            signal_line: data.signal_line,
            latest_close: data.latest_close,
            ai_valid: data.ai_valid,
            ai_confidence: data.ai_confidence,
            ai_reason: data.ai_reason,
            generated_at: data.created_at ?? data.generated_at,
            timeframe: data.timeframe,
            test_mode: data.test_mode,
            db_id: data.db_id,
          };
          setSignal(s);
          setHistory((prev) => [s, ...prev].slice(0, 50));
        }
      } catch (_) {}
    });

    const handleVisibilityChange = () => {
      if (!document.hidden && !socket.connected) socket.connect();
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    socket.on("signal", (data: Signal) => {
      setSignal(data);
      setHistory((prev) => [data, ...prev].slice(0, 50));

      if (data.crossover && data.ai_valid) {
        const direction = data.crossover === "UP_CROSS" ? "📈 買いシグナル" : "📉 売りシグナル";
        const label = data.test_mode ? "🧪 TEST " : "";
        const notifId = Math.floor(Math.random() * 100000);
        LocalNotifications.schedule({
          notifications: [{
            id: notifId,
            title: `${label}GOLD ${direction}`,
            body: `信頼度: ${data.ai_confidence}%  ${data.ai_reason}`,
            schedule: { at: new Date() },
            channelId: "gold-signal",
            sound: "default",
          }],
        });
        setTimeout(() => {
          LocalNotifications.cancel({ notifications: [{ id: notifId }] });
        }, 5 * 60 * 1000);
      }
    });

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      socket.disconnect();
    };
  }, []);

  useEffect(() => {
    if (activeTab === "analytics") {
      fetchTrades();
      fetchTodayStats();
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
      ]);
      try {
        localStorage.setItem("gt_tf", String(tf));
        localStorage.setItem("gt_mode", mode);
        localStorage.setItem("gt_crossover", crossoverMode);
      } catch (_) {}
      setSaveMsg("✅ 保存しました");
      setTimeout(() => { setSaveMsg(""); setSettingsOpen(false); }, 1500);
    } catch (_) {
      setSaveMsg("❌ 保存失敗");
    } finally {
      setSaving(false);
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
        <h1 style={styles.title}>GOLD AI トレーダー</h1>
        <button style={styles.settingsBtn} onClick={() => setSettingsOpen(true)}>⚙️</button>
      </div>

      <div style={{ ...styles.badge, background: connected ? "#22c55e" : "#ef4444" }}>
        {connected ? "● 接続中" : "○ 切断"}
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
              <Row label="クロスオーバー" value={signal.crossover ?? "なし"} />
              <Row label="RSI" value={signal.rsi?.toFixed(2)} />
              <Row
                label={signal.crossover_mode === "MACD" ? "MACDライン" : "RSIライン"}
                value={signal.main_line?.toFixed(4) ?? signal.rsi?.toFixed(2)}
              />
              <Row
                label={signal.crossover_mode === "MACD" ? "MACDシグナル" : signal.crossover_mode === "RSI_MACD" ? "MACDシグナル(正規化)" : "RSIシグナルSMA"}
                value={signal.signal_line?.toFixed(4)}
              />
              <Row label="終値" value={signal.latest_close?.toFixed(2)} />
              {signal.crossover && (
                <>
                  <hr style={styles.divider} />
                  <Row label="AI 判定" value={signal.ai_valid ? "✅ 有効" : "❌ ダマシ"} highlight={signal.ai_valid ? "#22c55e" : "#ef4444"} />
                  <Row label="信頼度" value={`${signal.ai_confidence}%`} />
                  <Row label="理由" value={signal.ai_reason ?? ""} />
                </>
              )}
              <p style={styles.timestamp}>{new Date(signal.generated_at).toLocaleString("ja-JP")}</p>
              {signal.crossover && signal.ai_valid && (
                <button
                  style={styles.btnEntry}
                  onClick={() => {
                    setEntryDirection(signal.crossover === "UP_CROSS" ? "BUY" : "SELL");
                    setEntryPrice(signal.latest_close?.toFixed(2) ?? "");
                    setEntryModalOpen(true);
                  }}
                >
                  {signal.crossover === "UP_CROSS" ? "📈 BUY 注文記録" : "📉 SELL 注文記録"}
                </button>
              )}
            </div>
          ) : (
            <p style={styles.waiting}>シグナル待機中...</p>
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
                <b>RSI:</b> RSI(14)がその9SMAを上抜け/下抜けを検出。
              </p>
              <p style={{ margin: "0 0 6px", fontSize: 12 }}>
                <b>MACD:</b> チャート下段の赤線(MACD)と青線(シグナル)のクロスを検出。
              </p>
              <p style={{ margin: 0, fontSize: 12 }}>
                <b>本運用:</b> 30〜60分足推奨。AIが信頼度を判定。
              </p>
            </div>

            {saveMsg && <div style={styles.saveMsg}>{saveMsg}</div>}

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
};
