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
  const [tf, setTf] = useState(30);
  const [mode, setMode] = useState<"PRODUCTION" | "TEST">("PRODUCTION");
  const [crossoverMode, setCrossoverMode] = useState<"RSI" | "MACD">("RSI");
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

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

    // ローカル通知の許可を取得
    LocalNotifications.requestPermissions();

    // 振動ありの通知チャンネルを作成
    LocalNotifications.createChannel({
      id: "gold-signal",
      name: "GOLDシグナル通知",
      importance: 5,
      vibration: true,
      sound: "default",
      description: "GOLDトレードシグナルの通知",
    });

    // WebSocket 接続（自動再接続あり）
    const socket: Socket = io(RENDER_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });

    socket.on("disconnect", () => setConnected(false));

    // 接続時に最新シグナルと設定を取得
    socket.on("connect", async () => {
      setConnected(true);
      try {
        const [sigRes, setRes] = await Promise.all([
          fetch(`${RENDER_URL}/latest-signal`),
          fetch(`${RENDER_URL}/api/settings/current`),
        ]);
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
          };
          setSignal(s);
          setHistory((prev) => [s, ...prev].slice(0, 50));
        }
        const settings = await setRes.json();
        if (settings.timeframe) setTf(settings.timeframe);
        if (settings.mode) setMode(settings.mode);
        if (settings.crossover_mode) setCrossoverMode(settings.crossover_mode);
      } catch (_) {}
    });

    // アプリがフォアグラウンドに戻ったとき再接続
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

  // ==================== 設定保存 ====================
  const saveSettings = async () => {
    setSaving(true);
    try {
      await Promise.all([
        fetch(`${RENDER_URL}/api/settings/timeframe`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timeframe: tf }),
        }),
        fetch(`${RENDER_URL}/api/settings/mode`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }),
        fetch(`${RENDER_URL}/api/settings/crossover`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ crossover_mode: crossoverMode }),
        }),
      ]);
      setSaveMsg("✅ 保存しました");
      setTimeout(() => { setSaveMsg(""); setSettingsOpen(false); }, 1500);
    } catch (_) {
      setSaveMsg("❌ 保存失敗");
    } finally {
      setSaving(false);
    }
  };

  // ==================== UI ====================
  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <h1 style={styles.title}>GOLD AI トレーダー</h1>
        <button style={styles.settingsBtn} onClick={() => setSettingsOpen(true)}>⚙️</button>
      </div>

      {/* 接続状態 */}
      <div style={{ ...styles.badge, background: connected ? "#22c55e" : "#ef4444" }}>
        {connected ? "● 接続中" : "○ 切断"}
      </div>

      {/* 最新シグナル */}
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
          <Row label={signal.crossover_mode === "MACD" ? "MACDライン" : "RSIライン"} value={signal.main_line?.toFixed(4) ?? signal.rsi?.toFixed(2)} />
          <Row label={signal.crossover_mode === "MACD" ? "MACDシグナル" : "シグナルライン"} value={signal.signal_line?.toFixed(4)} />
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
        </div>
      ) : (
        <p style={styles.waiting}>シグナル待機中...</p>
      )}

      {/* 履歴 */}
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

// ==================== スタイル ====================
const styles: Record<string, React.CSSProperties> = {
  container: { maxWidth: 480, margin: "0 auto", padding: "16px", fontFamily: "sans-serif", background: "#0f172a", minHeight: "100vh", color: "#f1f5f9" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 },
  title: { fontSize: 22, margin: 0 },
  settingsBtn: { background: "none", border: "none", fontSize: 24, cursor: "pointer", color: "#94a3b8", padding: "4px 8px" },
  badge: { display: "inline-block", padding: "4px 12px", borderRadius: 99, fontSize: 13, marginBottom: 16, color: "#fff" },
  card: { background: "#1e293b", borderRadius: 12, padding: 16, marginBottom: 16 },
  cardHeader: { display: "flex", alignItems: "center", gap: 8, marginBottom: 12 },
  cardTitle: { margin: 0, fontSize: 16, color: "#94a3b8" },
  testBadge: { fontSize: 11, background: "#854d0e", color: "#fef08a", padding: "2px 6px", borderRadius: 4 },
  tfBadge: { fontSize: 11, background: "#1e3a5f", color: "#93c5fd", padding: "2px 6px", borderRadius: 4 },
  modeBadge: { fontSize: 11, background: "#3b1f5f", color: "#c4b5fd", padding: "2px 6px", borderRadius: 4 },
  row: { display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 14 },
  label: { color: "#94a3b8" },
  value: { fontWeight: "bold" },
  divider: { border: "none", borderTop: "1px solid #334155", margin: "12px 0" },
  timestamp: { fontSize: 11, color: "#64748b", textAlign: "right", margin: "8px 0 0" },
  waiting: { textAlign: "center", color: "#64748b", marginTop: 40 },
  historyRow: { display: "flex", justifyContent: "space-between", fontSize: 12, padding: "6px 0", borderBottom: "1px solid #1e293b" },
  overlay: { position: "fixed", top: 0, left: 0, right: 0, bottom: 0, background: "rgba(0,0,0,0.75)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  settingsPanel: { background: "#1e293b", borderRadius: 16, padding: 24, width: "90%", maxWidth: 380, color: "#f1f5f9" },
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
