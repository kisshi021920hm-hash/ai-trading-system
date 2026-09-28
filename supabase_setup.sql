-- Supabase でこの SQL を実行してください（Table Editor → SQL Editor）

CREATE TABLE IF NOT EXISTS signals (
  id            BIGSERIAL PRIMARY KEY,
  symbol        TEXT NOT NULL DEFAULT 'GOLD',
  timeframe     TEXT NOT NULL DEFAULT 'M30',
  crossover     TEXT,           -- 'UP_CROSS' | 'DOWN_CROSS' | NULL
  rsi           NUMERIC(6,2),
  signal_line   NUMERIC(6,2),
  latest_close  NUMERIC(10,2),
  ai_valid      BOOLEAN,
  ai_confidence INTEGER,
  ai_reason     TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 直近シグナルを高速取得するためのインデックス
CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC);

-- Row Level Security（公開読み取り、サービスキーのみ書き込み）
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "公開読み取り" ON signals
  FOR SELECT USING (true);

CREATE POLICY "サービスキーのみ挿入" ON signals
  FOR INSERT WITH CHECK (true);
