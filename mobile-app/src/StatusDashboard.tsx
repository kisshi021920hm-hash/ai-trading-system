import { useEffect, useState } from "react";

interface StatusLog {
  recorded_at: string;
  server_uptime_hours: number;
  settings_timeframe: number;
  settings_crossover_mode: string;
  settings_trading_mode: string;
  test_mode: boolean;
  gemini_state: string;
  gemini_last_direction: string;
  gemini_approved: boolean;
  gemini_last_call_ago_sec: number | null;
  ea_alive: boolean;
  ea_last_heartbeat_ago_sec: number | null;
  ea_last_signal_push_ago_sec: number | null;
  ea_signal_loop_mode: string;
  ea_buy_score: number | null;
  ea_sell_score: number | null;
  ea_adx: number | null;
  fcm_token_count: number;
  recent_trade_count: number;
  today_total_pips: number;
}

interface StatusDashboardProps {
  renderUrl: string;
}

export default function StatusDashboard({ renderUrl }: StatusDashboardProps) {
  const [logs, setLogs] = useState<StatusLog[]>([]);
  const [latest, setLatest] = useState<StatusLog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchLogs = async () => {
      try {
        setLoading(true);
        setError(null);

        // 最新ログを取得（Flask 経由）
        const resp = await fetch(`${renderUrl}/api/status-logs/latest?limit=10`);

        if (resp.ok) {
          const data = await resp.json();
          setLogs(data);
          if (data.length > 0) {
            setLatest(data[0]);
          }
        } else {
          setError(`データ取得失敗: ${resp.status}`);
        }
      } catch (err) {
        setError(`エラー: ${err instanceof Error ? err.message : "不明"}`);
      } finally {
        setLoading(false);
      }
    };

    fetchLogs();
    const interval = setInterval(fetchLogs, 30000); // 30秒ごと
    return () => clearInterval(interval);
  }, [renderUrl]);

  if (loading) {
    return (
      <div style={{ padding: "20px", textAlign: "center" }}>
        <p>📊 ダッシュボード読み込み中...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: "20px", color: "#c41e3a" }}>
        <p>❌ {error}</p>
      </div>
    );
  }

  if (!latest) {
    return (
      <div style={{ padding: "20px", textAlign: "center" }}>
        <p>📊 データなし</p>
      </div>
    );
  }

  return (
    <div style={{ padding: "15px", backgroundColor: "#f5f5f5", minHeight: "100vh" }}>
      <h2 style={{ marginTop: 0 }}>📊 システムダッシュボード</h2>

      {/* KPI タイル */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", marginBottom: "20px" }}>
        {/* EA稼働状態 */}
        <div
          style={{
            padding: "12px",
            backgroundColor: latest.ea_alive ? "#d4edda" : "#f8d7da",
            borderRadius: "8px",
            borderLeft: `4px solid ${latest.ea_alive ? "#28a745" : "#c41e3a"}`,
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>EA稼働</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold" }}>
            {latest.ea_alive ? "🟢 稼働中" : "🔴 停止"}
          </p>
          {latest.ea_last_heartbeat_ago_sec !== null && (
            <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#666" }}>
              {latest.ea_last_heartbeat_ago_sec}秒前
            </p>
          )}
        </div>

        {/* Gemini状態 */}
        <div
          style={{
            padding: "12px",
            backgroundColor: latest.gemini_approved ? "#d4edda" : "#fff3cd",
            borderRadius: "8px",
            borderLeft: `4px solid ${latest.gemini_approved ? "#28a745" : "#ffc107"}`,
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>Gemini</p>
          <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold" }}>
            {latest.gemini_approved ? "✅ 承認済み" : "⏳ 待機中"}
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#666" }}>
            {latest.gemini_last_direction || "なし"}
          </p>
        </div>

        {/* スコア */}
        <div
          style={{
            padding: "12px",
            backgroundColor: "#e7f3ff",
            borderRadius: "8px",
            borderLeft: "4px solid #0066cc",
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>最新スコア</p>
          <p style={{ margin: "0", fontSize: "14px", fontWeight: "bold" }}>
            買:{latest.ea_buy_score ?? "-"} / 売:{latest.ea_sell_score ?? "-"}
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#666" }}>
            ADX: {latest.ea_adx ? latest.ea_adx.toFixed(1) : "-"}
          </p>
        </div>

        {/* FCM登録 */}
        <div
          style={{
            padding: "12px",
            backgroundColor: "#e8e5ff",
            borderRadius: "8px",
            borderLeft: "4px solid #6c5ce7",
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>FCM登録</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold" }}>
            {latest.fcm_token_count}台
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#666" }}>
            接続済みデバイス
          </p>
        </div>

        {/* サーバー稼働時間 */}
        <div
          style={{
            padding: "12px",
            backgroundColor: "#e0f2f1",
            borderRadius: "8px",
            borderLeft: "4px solid #009688",
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>稼働時間</p>
          <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold" }}>
            {latest.server_uptime_hours.toFixed(1)}時間
          </p>
        </div>

        {/* 本日損益 */}
        <div
          style={{
            padding: "12px",
            backgroundColor: latest.today_total_pips >= 0 ? "#d4edda" : "#f8d7da",
            borderRadius: "8px",
            borderLeft: `4px solid ${latest.today_total_pips >= 0 ? "#28a745" : "#c41e3a"}`,
          }}
        >
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#666" }}>本日Pips</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold" }}>
            {latest.today_total_pips >= 0 ? "+" : ""}{latest.today_total_pips.toFixed(1)}
          </p>
        </div>
      </div>

      {/* 詳細情報 */}
      <div style={{ backgroundColor: "white", padding: "15px", borderRadius: "8px", marginBottom: "20px" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px" }}>⚙️ 設定</h3>
        <div style={{ fontSize: "13px", lineHeight: "1.8" }}>
          <p style={{ margin: "6px 0" }}>
            <strong>時間足:</strong> {latest.settings_timeframe}分
          </p>
          <p style={{ margin: "6px 0" }}>
            <strong>クロスオーバー:</strong> {latest.settings_crossover_mode}
          </p>
          <p style={{ margin: "6px 0" }}>
            <strong>トレードモード:</strong> {latest.settings_trading_mode}
          </p>
          <p style={{ margin: "6px 0" }}>
            <strong>モード:</strong> {latest.test_mode ? "🧪 TEST" : "🚀 PRODUCTION"}
          </p>
        </div>
      </div>

      {/* シグナルループモード */}
      <div style={{ backgroundColor: "white", padding: "15px", borderRadius: "8px", marginBottom: "20px" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px" }}>📡 シグナル</h3>
        <p style={{ margin: "0", fontSize: "13px" }}>
          <strong>ソース:</strong> {latest.ea_signal_loop_mode}
        </p>
        {latest.ea_last_signal_push_ago_sec !== null && (
          <p style={{ margin: "6px 0 0 0", fontSize: "13px" }}>
            <strong>最終受信:</strong> {latest.ea_last_signal_push_ago_sec}秒前
          </p>
        )}
      </div>

      {/* ログ履歴 */}
      <div style={{ backgroundColor: "white", padding: "15px", borderRadius: "8px" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px" }}>📋 ログ履歴</h3>
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              fontSize: "12px",
              borderCollapse: "collapse",
            }}
          >
            <thead>
              <tr style={{ backgroundColor: "#f0f0f0", borderBottom: "1px solid #ddd" }}>
                <th style={{ padding: "8px", textAlign: "left" }}>時刻</th>
                <th style={{ padding: "8px", textAlign: "center" }}>EA</th>
                <th style={{ padding: "8px", textAlign: "center" }}>Gemini</th>
                <th style={{ padding: "8px", textAlign: "right" }}>スコア</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #eee" }}>
                  <td style={{ padding: "8px" }}>
                    {new Date(log.recorded_at).toLocaleTimeString("ja-JP", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </td>
                  <td style={{ padding: "8px", textAlign: "center" }}>
                    {log.ea_alive ? "🟢" : "🔴"}
                  </td>
                  <td style={{ padding: "8px", textAlign: "center" }}>
                    {log.gemini_approved ? "✅" : "⏳"}
                  </td>
                  <td style={{ padding: "8px", textAlign: "right" }}>
                    {log.ea_buy_score ?? "-"}/{log.ea_sell_score ?? "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
