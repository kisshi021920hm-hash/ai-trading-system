import { useEffect, useState } from "react";

interface SystemLog {
  timestamp: string;
  level: string;
  message: string;
}

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
  gemini_model: string;
  gemini_model_fallback_count: number;
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
  // AI 決済判定履歴
  ai_decision_timestamp: string | null;
  ai_decision_type: string | null;
  ai_confidence_score: number | null;
  ai_decision_reason: string | null;
  ai_executed_action: string | null;
}

interface StatusDashboardProps {
  renderUrl: string;
}


interface GeminiStats { approval_rate: number; total_calls: number; model_switches: number; current_model: string; close_rate: number; }
interface MonthlyStats { total_profit_usd: number; closed_trades: number; win_rate: number; }

export default function StatusDashboard({ renderUrl }: StatusDashboardProps) {
  const [logs, setLogs] = useState<StatusLog[]>([]);
  const [latest, setLatest] = useState<StatusLog | null>(null);
  const [systemLogs, setSystemLogs] = useState<SystemLog[]>([]);
  const [geminiStats, setGeminiStats] = useState<GeminiStats | null>(null);
  const [monthlyStats, setMonthlyStats] = useState<MonthlyStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

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

        // システムログを取得
        const sysResp = await fetch(`${renderUrl}/api/system-logs`);
        if (sysResp.ok) {
          const sysData = await sysResp.json();
          setSystemLogs(sysData);
        }

        // Gemini統計を取得
        const gemResp = await fetch(`${renderUrl}/api/gemini-stats`);
        if (gemResp.ok) setGeminiStats(await gemResp.json());

        // 月次統計を取得
        const monResp = await fetch(`${renderUrl}/api/stats/monthly`);
        if (monResp.ok) setMonthlyStats(await monResp.json());

        setLastUpdated(new Date());
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
    <div style={{ padding: "15px", backgroundColor: "#0f172a", minHeight: "100vh" }}>
      <h2 style={{ marginTop: 0, color: "#f1f5f9" }}>📊 システムダッシュボード</h2>

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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>EA稼働</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold", color: "#000" }}>
            {latest.ea_alive ? "🟢 稼働中" : "🔴 停止"}
          </p>
          {latest.ea_last_heartbeat_ago_sec !== null && (
            <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#000" }}>
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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>Gemini</p>
          <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold", color: "#000" }}>
            {latest.gemini_approved ? "✅ 承認済み" : "⏳ 待機中"}
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#000" }}>
            {latest.gemini_last_direction || "なし"}
          </p>
          <p style={{ margin: "6px 0 0 0", fontSize: "10px", color: "#000", fontWeight: "bold" }}>
            🤖 {latest.gemini_model || "モデル取得中..."}
          </p>
          {latest.gemini_model_fallback_count > 0 && (
            <p style={{ margin: "2px 0 0 0", fontSize: "10px", color: "#c41e3a" }}>
              切り替え: {latest.gemini_model_fallback_count}回
            </p>
          )}
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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>最新スコア</p>
          <p style={{ margin: "0", fontSize: "14px", fontWeight: "bold", color: "#000" }}>
            買:{latest.ea_buy_score ?? "-"} / 売:{latest.ea_sell_score ?? "-"}
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#000" }}>
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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>FCM登録</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold", color: "#000" }}>
            {latest.fcm_token_count}台
          </p>
          <p style={{ margin: "4px 0 0 0", fontSize: "11px", color: "#000" }}>
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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>稼働時間</p>
          <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold", color: "#000" }}>
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
          <p style={{ margin: "0 0 4px 0", fontSize: "12px", color: "#000" }}>本日損益</p>
          <p style={{ margin: "0", fontSize: "18px", fontWeight: "bold", color: "#000" }}>
            {latest.today_total_pips >= 0 ? "+" : ""}{latest.today_total_pips.toFixed(2)} USD
          </p>
          <p style={{ margin: "2px 0 0 0", fontSize: "11px", color: "#555" }}>
            ≈ ¥{Math.round(latest.today_total_pips * 155).toLocaleString()}
          </p>
        </div>
      </div>

      {/* 月次サマリー + Gemini指標 */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px", marginBottom: "20px" }}>
        {monthlyStats && (
          <div style={{ backgroundColor: "#1e293b", padding: "12px", borderRadius: "8px", color: "#f1f5f9" }}>
            <p style={{ margin: "0 0 4px 0", fontSize: "11px", color: "#94a3b8" }}>今月累計</p>
            <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold",
              color: monthlyStats.total_profit_usd >= 0 ? "#22c55e" : "#ef4444" }}>
              {monthlyStats.total_profit_usd >= 0 ? "+" : ""}{monthlyStats.total_profit_usd.toFixed(2)}$
            </p>
            <p style={{ margin: "2px 0 0 0", fontSize: "11px", color: "#64748b" }}>
              {monthlyStats.closed_trades}件 / 勝率{monthlyStats.win_rate}%
            </p>
          </div>
        )}
        {geminiStats && (
          <div style={{ backgroundColor: "#1e293b", padding: "12px", borderRadius: "8px", color: "#f1f5f9" }}>
            <p style={{ margin: "0 0 4px 0", fontSize: "11px", color: "#94a3b8" }}>Gemini承認率</p>
            <p style={{ margin: "0", fontSize: "16px", fontWeight: "bold",
              color: geminiStats.approval_rate >= 50 ? "#22c55e" : "#f59e0b" }}>
              {geminiStats.approval_rate}%
            </p>
            <p style={{ margin: "2px 0 0 0", fontSize: "11px", color: "#64748b" }}>
              {geminiStats.total_calls}判定 / 切替{geminiStats.model_switches}回
            </p>
          </div>
        )}
      </div>

      {/* 最終更新時刻 */}
      {lastUpdated && (
        <div style={{ textAlign: "right", fontSize: "11px", color: "#475569", marginBottom: "8px" }}>
          最終更新: {lastUpdated.toLocaleTimeString("ja-JP")} （30秒自動更新）
        </div>
      )}

      {/* 詳細情報 */}
      <div style={{ backgroundColor: "#1e293b", padding: "15px", borderRadius: "8px", marginBottom: "20px", color: "#f1f5f9" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px", color: "#94a3b8" }}>⚙️ 設定</h3>
        <div style={{ fontSize: "13px", lineHeight: "1.8" }}>
          <p style={{ margin: "6px 0", color: "#e2e8f0" }}>
            <strong>時間足:</strong> {latest.settings_timeframe}分
          </p>
          <p style={{ margin: "6px 0", color: "#e2e8f0" }}>
            <strong>クロスオーバー:</strong> {latest.settings_crossover_mode}
          </p>
          <p style={{ margin: "6px 0", color: "#e2e8f0" }}>
            <strong>トレードモード:</strong> {latest.settings_trading_mode}
          </p>
          <p style={{ margin: "6px 0", color: "#e2e8f0" }}>
            <strong>モード:</strong> {latest.test_mode ? "🧪 TEST" : "🚀 PRODUCTION"}
          </p>
        </div>
      </div>

      {/* シグナルループモード */}
      <div style={{ backgroundColor: "#1e293b", padding: "15px", borderRadius: "8px", marginBottom: "20px", color: "#f1f5f9" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px", color: "#94a3b8" }}>📡 シグナル</h3>
        <p style={{ margin: "0", fontSize: "13px", color: "#e2e8f0" }}>
          <strong>ソース:</strong> {latest.ea_signal_loop_mode}
        </p>
        {latest.ea_last_signal_push_ago_sec !== null && (
          <p style={{ margin: "6px 0 0 0", fontSize: "13px", color: "#e2e8f0" }}>
            <strong>最終受信:</strong> {latest.ea_last_signal_push_ago_sec}秒前
          </p>
        )}
      </div>

      {/* AI 決定履歴 */}
      {latest.ai_decision_timestamp && (
        <div style={{ backgroundColor: "#1e293b", padding: "15px", borderRadius: "8px", marginBottom: "20px", color: "#f1f5f9" }}>
          <h3 style={{ marginTop: 0, marginBottom: "12px", color: "#94a3b8" }}>🤖 AI判定履歴</h3>
          <div style={{ fontSize: "12px", lineHeight: "2" }}>
            <p style={{ margin: "0", color: "#e2e8f0" }}>
              <strong>時刻:</strong> {new Date(latest.ai_decision_timestamp).toLocaleTimeString("ja-JP")}
            </p>
            <p style={{ margin: "0", color: "#e2e8f0" }}>
              <strong>種類:</strong> {latest.ai_decision_type || "なし"}
            </p>
            {latest.ai_confidence_score !== null && (
              <p style={{ margin: "0", color: "#e2e8f0" }}>
                <strong>信頼度:</strong> {latest.ai_confidence_score}%
              </p>
            )}
            {latest.ai_decision_reason && (
              <p style={{ margin: "0", color: "#cbd5e1", fontSize: "11px", whiteSpace: "pre-wrap" }}>
                <strong>理由:</strong> {latest.ai_decision_reason}
              </p>
            )}
            {latest.ai_executed_action && (
              <p style={{ margin: "0", color: "#a1e3a1" }}>
                <strong>実行:</strong> {latest.ai_executed_action}
              </p>
            )}
          </div>
        </div>
      )}

      {/* システムログ */}
      {systemLogs.length > 0 && (
        <div style={{ backgroundColor: "#1e293b", padding: "15px", borderRadius: "8px", marginBottom: "20px", color: "#f1f5f9" }}>
          <h3 style={{ marginTop: 0, marginBottom: "12px", color: "#94a3b8" }}>📜 システムログ</h3>
          <div style={{ fontSize: "11px", lineHeight: "1.6", maxHeight: "200px", overflowY: "auto" }}>
            {systemLogs.map((log, i) => (
              <p key={i} style={{ margin: "4px 0", color: log.level === "ERROR" ? "#f87171" : log.level === "WARNING" ? "#fbbf24" : "#cbd5e1" }}>
                <span style={{ fontWeight: "bold" }}>{new Date(log.timestamp).toLocaleTimeString("ja-JP")}</span> — {log.message}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* ログ履歴 */}
      <div style={{ backgroundColor: "#1e293b", padding: "15px", borderRadius: "8px", color: "#f1f5f9" }}>
        <h3 style={{ marginTop: 0, marginBottom: "12px", color: "#94a3b8" }}>📋 ログ履歴</h3>
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              fontSize: "12px",
              borderCollapse: "collapse",
            }}
          >
            <thead>
              <tr style={{ backgroundColor: "#334155", borderBottom: "1px solid #475569" }}>
                <th style={{ padding: "8px", textAlign: "left", color: "#cbd5e1" }}>時刻</th>
                <th style={{ padding: "8px", textAlign: "center", color: "#cbd5e1" }}>EA</th>
                <th style={{ padding: "8px", textAlign: "center", color: "#cbd5e1" }}>Gemini</th>
                <th style={{ padding: "8px", textAlign: "right", color: "#cbd5e1" }}>スコア</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #334155" }}>
                  <td style={{ padding: "8px", color: "#e2e8f0" }}>
                    {new Date(log.recorded_at).toLocaleTimeString("ja-JP", {
                      hour: "2-digit",
                      minute: "2-digit",
                      second: "2-digit",
                    })}
                  </td>
                  <td style={{ padding: "8px", textAlign: "center", color: "#e2e8f0" }}>
                    {log.ea_alive ? "🟢" : "🔴"}
                  </td>
                  <td style={{ padding: "8px", textAlign: "center", color: "#e2e8f0" }}>
                    {log.gemini_approved ? "✅" : "⏳"}
                  </td>
                  <td style={{ padding: "8px", textAlign: "right", color: "#e2e8f0" }}>
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
