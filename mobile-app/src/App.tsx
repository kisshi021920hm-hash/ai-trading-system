import { useEffect, useState } from "react";
import { io, Socket } from "socket.io-client";
import { LocalNotifications } from "@capacitor/local-notifications";
import { PushNotifications } from "@capacitor/push-notifications";

// ==================== 型定義 ====================
interface Signal {
  crossover: "UP_CROSS" | "DOWN_CROSS" | null;
  rsi: number;
  signal_line: number;
  latest_close: number;
  ai_valid: boolean | null;
  ai_confidence: number | null;
  ai_reason: string | null;
  generated_at: string;
}

// ==================== 設定 ====================
const RENDER_URL = import.meta.env.VITE_RENDER_URL ?? "https://ai-trading-system-81jb.onrender.com";

// ==================== メインコンポーネント ====================
export default function App() {
  const [signal, setSignal] = useState<Signal | null>(null);
  const [history, setHistory] = useState<Signal[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    // FCMプッシュ通知の登録
    PushNotifications.requestPermissions().then(result => {
      if (result.receive === "granted") {
        PushNotifications.register();
      }
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

    // 接続時に最新シグナルを即座に取得
    socket.on("connect", async () => {
      setConnected(true);
      try {
        const res = await fetch(`${RENDER_URL}/latest-signal`);
        const data = await res.json();
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
          };
          setSignal(s);
          setHistory((prev) => [s, ...prev].slice(0, 50));
        }
      } catch (_) {}
    });

    // アプリがフォアグラウンドに戻ったとき再接続
    const handleVisibilityChange = () => {
      if (!document.hidden && !socket.connected) {
        socket.connect();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    socket.on("signal", (data: Signal) => {
      setSignal(data);
      setHistory((prev) => [data, ...prev].slice(0, 50));

      // クロスオーバーかつ AI が有効と判定した場合のみ通知
      if (data.crossover && data.ai_valid) {
        const direction = data.crossover === "UP_CROSS" ? "📈 買いシグナル" : "📉 売りシグナル";
        const notifId = Math.floor(Math.random() * 100000);
        LocalNotifications.schedule({
          notifications: [
            {
              id: notifId,
              title: `GOLD ${direction}`,
              body: `信頼度: ${data.ai_confidence}%  理由: ${data.ai_reason}`,
              schedule: { at: new Date() },
              channelId: "gold-signal",
              sound: "default",
            },
          ],
        });
        // 5分後に通知を自動消去
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

  // ==================== UI ====================
  return (
    <div style={styles.container}>
      <h1 style={styles.title}>GOLD AI トレーダー</h1>

      {/* 接続状態 */}
      <div style={{ ...styles.badge, background: connected ? "#22c55e" : "#ef4444" }}>
        {connected ? "● 接続中" : "○ 切断"}
      </div>

      {/* 最新シグナル */}
      {signal ? (
        <div style={styles.card}>
          <h2 style={styles.cardTitle}>最新シグナル</h2>
          <Row label="クロスオーバー" value={signal.crossover ?? "なし"} />
          <Row label="RSI" value={signal.rsi?.toFixed(2)} />
          <Row label="シグナルライン" value={signal.signal_line?.toFixed(2)} />
          <Row label="終値" value={signal.latest_close?.toFixed(2)} />

          {signal.crossover && (
            <>
              <hr style={styles.divider} />
              <Row
                label="AI 判定"
                value={signal.ai_valid ? "✅ 有効" : "❌ ダマシ"}
                highlight={signal.ai_valid ? "#22c55e" : "#ef4444"}
              />
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
  title: { textAlign: "center", fontSize: 22, margin: "0 0 12px" },
  badge: { display: "inline-block", padding: "4px 12px", borderRadius: 99, fontSize: 13, marginBottom: 16, color: "#fff" },
  card: { background: "#1e293b", borderRadius: 12, padding: 16, marginBottom: 16 },
  cardTitle: { margin: "0 0 12px", fontSize: 16, color: "#94a3b8" },
  row: { display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 14 },
  label: { color: "#94a3b8" },
  value: { fontWeight: "bold" },
  divider: { border: "none", borderTop: "1px solid #334155", margin: "12px 0" },
  timestamp: { fontSize: 11, color: "#64748b", textAlign: "right", margin: "8px 0 0" },
  waiting: { textAlign: "center", color: "#64748b", marginTop: 40 },
  historyRow: { display: "flex", justifyContent: "space-between", fontSize: 12, padding: "6px 0", borderBottom: "1px solid #1e293b" },
};
