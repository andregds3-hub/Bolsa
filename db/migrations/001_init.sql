-- Schema alvo (PostgreSQL 17 + TimescaleDB). O MVP roda em memória; esta é a persistência da fase 2.
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TYPE account_status AS ENUM ('ACTIVE','DAILY_LOCKED','BREACHED','PASSED','ARCHIVED');
CREATE TYPE order_side     AS ENUM ('BUY','SELL');
CREATE TYPE order_type     AS ENUM ('MARKET','LIMIT','STOP','STOP_LIMIT');
CREATE TYPE order_status   AS ENUM ('PENDING_NEW','WORKING','PARTIALLY_FILLED','FILLED','CANCELED','REJECTED','EXPIRED');
CREATE TYPE order_role     AS ENUM ('ENTRY','STOP_LOSS','BREAKEVEN','TRAILING','TAKE_PROFIT','MANUAL','LIQUIDATION');
CREATE TYPE asset_class    AS ENUM ('STOCK','ETF','FUTURE','OPTION','FOREX','CFD_INDEX','CFD_COMMODITY');

-- ───────────── Identidade & LGPD ─────────────
CREATE TABLE users (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  idp_subject       text UNIQUE NOT NULL,          -- "sub" do Keycloak (hash Argon2id vive no IdP)
  email             citext UNIQUE NOT NULL,
  display_name      text NOT NULL,
  public_alias      text UNIQUE NOT NULL,          -- nome exibido no ranking (nunca o e-mail)
  email_verified_at timestamptz,
  locale            text NOT NULL DEFAULT 'pt-BR',
  created_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz                    -- exclusão: anonimiza já, expurga após prazo legal
);

CREATE TABLE user_consents (                       -- prova de aceite, versionada
  id         bigserial PRIMARY KEY,
  user_id    uuid NOT NULL REFERENCES users(id),
  document   text NOT NULL CHECK (document IN ('TERMS','PRIVACY','SIM_DISCLAIMER','MARKETING')),
  version    text NOT NULL,
  accepted   boolean NOT NULL,
  ip         inet,
  user_agent text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE data_subject_requests (               -- LGPD art. 18: exportação / eliminação
  id           bigserial PRIMARY KEY,
  user_id      uuid NOT NULL REFERENCES users(id),
  kind         text NOT NULL CHECK (kind IN ('EXPORT','DELETE')),
  status       text NOT NULL DEFAULT 'PENDING',
  requested_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  artifact_url text                                -- link assinado e temporário (S3)
);

-- ───────────── Instrumentos & câmbio ─────────────
CREATE TABLE instruments (
  id            serial PRIMARY KEY,
  symbol        text UNIQUE NOT NULL,              -- WINZ26, WDOF27, PETR4, EURUSD, XAUUSD, US100
  venue         text NOT NULL,                     -- B3, FX, CME, NASDAQ, CFD
  asset_class   asset_class NOT NULL,
  quote_ccy     char(3) NOT NULL,
  tick_size     numeric(20,10) NOT NULL,
  tick_value    numeric(20,10) NOT NULL,           -- valor de 1 tick por 1 unidade de qty
  contract_size numeric(20,4)  NOT NULL DEFAULT 1, -- FX: 100000
  lot_step      numeric(20,4)  NOT NULL DEFAULT 1, -- B3: 100 (padrão) / 1 (fracionário); FX: 0.01
  margin_model  jsonb NOT NULL,                    -- {"type":"fixed","day":150} | {"type":"leverage","ratio":100}
  fee_model     jsonb NOT NULL,                    -- corretagem, emolumentos, ISS, swap long/short
  session       jsonb NOT NULL,                    -- pregão, leilões, rollover, feriados
  underlying_id int REFERENCES instruments(id),    -- opções / futuros
  option_type   char(1) CHECK (option_type IN ('C','P')),
  option_style  char(1) CHECK (option_style IN ('A','E')),
  strike        numeric(20,8),
  expiry        date,
  is_active     boolean NOT NULL DEFAULT true
);

CREATE TABLE fx_rates (
  ts    timestamptz NOT NULL,
  base  char(3) NOT NULL,
  quote char(3) NOT NULL,
  bid   numeric(20,10) NOT NULL,
  ask   numeric(20,10) NOT NULL
);
SELECT create_hypertable('fx_rates', 'ts');

-- ───────────── Ranking (temporadas antes das contas) ─────────────
CREATE TABLE leaderboard_seasons (
  id        serial PRIMARY KEY,
  name      text NOT NULL,                         -- "Temporada Nov/2026"
  starts_at timestamptz NOT NULL,
  ends_at   timestamptz NOT NULL,
  rules     jsonb NOT NULL                         -- {"min_trades":20,"min_days":10,"weights":{...}}
);

-- ───────────── Contas & regras de risco ─────────────
CREATE TABLE accounts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES users(id),
  name            text NOT NULL,
  plan            text NOT NULL,                   -- PRACTICE | CHALLENGE_50K | CHALLENGE_50K_TRAILING
  base_ccy        char(3) NOT NULL,
  initial_balance numeric(20,2) NOT NULL,
  balance         numeric(20,2) NOT NULL,          -- projeção do ledger (fonte da verdade: ledger_entries)
  high_water_mark numeric(20,2) NOT NULL,
  position_mode   text NOT NULL DEFAULT 'NETTING' CHECK (position_mode IN ('NETTING','HEDGING')),
  status          account_status NOT NULL DEFAULT 'ACTIVE',
  season_id       int REFERENCES leaderboard_seasons(id),
  ranked          boolean NOT NULL DEFAULT false,  -- no máx. 1 conta ranqueada por usuário/temporada
  reset_count     int NOT NULL DEFAULT 0,
  reset_of        uuid REFERENCES accounts(id),    -- reset = arquiva a antiga e cria uma nova
  created_at      timestamptz NOT NULL DEFAULT now(),
  closed_at       timestamptz
);
CREATE UNIQUE INDEX one_ranked_account_per_season
  ON accounts (user_id, season_id) WHERE ranked AND status <> 'ARCHIVED';

CREATE TABLE risk_limits (
  account_id                  uuid PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  daily_loss_limit            numeric(20,2) NOT NULL,
  daily_loss_basis            text NOT NULL DEFAULT 'MAX_BALANCE_EQUITY'
                              CHECK (daily_loss_basis IN ('BALANCE','MAX_BALANCE_EQUITY')),
  max_drawdown                numeric(20,2) NOT NULL,
  drawdown_mode               text NOT NULL DEFAULT 'STATIC'
                              CHECK (drawdown_mode IN ('STATIC','TRAILING_INTRADAY','TRAILING_EOD')),
  drawdown_lock_at            numeric(20,2),       -- trailing para de subir aqui (ex.: saldo inicial)
  profit_target               numeric(20,2),
  min_trading_days            int NOT NULL DEFAULT 0,
  consistency_max_day_pct     numeric(5,2),        -- melhor dia ≤ X% do lucro total
  max_position_size           jsonb,               -- {"WIN":20,"WDO":5,"EURUSD":10}
  stop_out_level              numeric(5,4) NOT NULL DEFAULT 0.50,
  block_orders_risking_breach boolean NOT NULL DEFAULT true,
  news_blackout_seconds       int NOT NULL DEFAULT 0,   -- 120 = proíbe abrir ±2 min de notícia HIGH
  reset_tz                    text NOT NULL DEFAULT 'America/Sao_Paulo',
  reset_time                  time NOT NULL DEFAULT '18:00'
);

CREATE TABLE risk_daily_state (
  account_id    uuid NOT NULL REFERENCES accounts(id),
  trading_day   date NOT NULL,
  day_anchor    numeric(20,2) NOT NULL,            -- max(saldo, equity) no reset
  min_equity    numeric(20,2) NOT NULL,
  max_equity    numeric(20,2) NOT NULL,
  realized_pnl  numeric(20,2) NOT NULL DEFAULT 0,
  breached_at   timestamptz,
  breach_reason text,                              -- DAILY_LOSS | MAX_DRAWDOWN | STOP_OUT
  PRIMARY KEY (account_id, trading_day)
);

-- ───────────── Ordens, execuções, posições ─────────────
CREATE TABLE orders (
  id              uuid PRIMARY KEY,
  account_id      uuid NOT NULL REFERENCES accounts(id),
  client_order_id text NOT NULL,                   -- idempotência (reenvio não duplica)
  instrument_id   int  NOT NULL REFERENCES instruments(id),
  side            order_side NOT NULL,
  type            order_type NOT NULL,
  role            order_role NOT NULL DEFAULT 'ENTRY',
  qty             numeric(20,4) NOT NULL CHECK (qty > 0),
  filled_qty      numeric(20,4) NOT NULL DEFAULT 0,
  limit_price     numeric(20,8),
  stop_price      numeric(20,8),
  time_in_force   text NOT NULL DEFAULT 'DAY' CHECK (time_in_force IN ('DAY','GTC','GTD','IOC','FOK')),
  expire_at       timestamptz,
  reduce_only     boolean NOT NULL DEFAULT false,
  parent_order_id uuid REFERENCES orders(id),      -- entrada que originou o bracket
  oco_group_id    uuid,                            -- pernas que se cancelam mutuamente
  position_id     uuid,
  bracket         jsonb,  -- {"sl_ticks":60,"tp_ticks":120,"trail_ticks":40,"trail_activation":50,"be_trigger":30,"be_offset":2}
  avg_fill_price  numeric(20,8),
  status          order_status NOT NULL,
  reject_code     text,
  source          text NOT NULL DEFAULT 'MANUAL',  -- MANUAL | EA:<id> | RISK_ENGINE
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, client_order_id)
);
CREATE INDEX orders_working ON orders (account_id) WHERE status IN ('WORKING','PARTIALLY_FILLED');
CREATE INDEX orders_oco     ON orders (oco_group_id) WHERE oco_group_id IS NOT NULL;

CREATE TABLE executions (
  id             bigserial PRIMARY KEY,
  order_id       uuid NOT NULL REFERENCES orders(id),
  account_id     uuid NOT NULL REFERENCES accounts(id),
  instrument_id  int  NOT NULL REFERENCES instruments(id),
  side           order_side NOT NULL,
  qty            numeric(20,4) NOT NULL,
  price          numeric(20,8) NOT NULL,
  slippage_ticks numeric(10,2) NOT NULL DEFAULT 0,
  fee            numeric(20,4) NOT NULL DEFAULT 0,
  fee_ccy        char(3) NOT NULL,
  fx_to_base     numeric(20,10) NOT NULL,
  liquidity      text NOT NULL CHECK (liquidity IN ('MAKER','TAKER')),
  executed_at    timestamptz NOT NULL
);

CREATE TABLE positions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id    uuid NOT NULL REFERENCES accounts(id),
  instrument_id int  NOT NULL REFERENCES instruments(id),
  side          order_side NOT NULL,
  qty           numeric(20,4) NOT NULL,
  avg_price     numeric(20,8) NOT NULL,
  status        text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
  opened_at     timestamptz NOT NULL,
  closed_at     timestamptz
);
CREATE UNIQUE INDEX one_open_position_netting
  ON positions (account_id, instrument_id) WHERE status = 'OPEN';  -- remover para contas HEDGING

-- ───────────── Histórico de trades (base do Journal) ─────────────
CREATE TABLE trades (
  id                bigserial PRIMARY KEY,
  account_id        uuid NOT NULL REFERENCES accounts(id),
  position_id       uuid NOT NULL REFERENCES positions(id),
  instrument_id     int  NOT NULL REFERENCES instruments(id),
  side              order_side NOT NULL,
  qty               numeric(20,4) NOT NULL,
  entry_price       numeric(20,8) NOT NULL,
  exit_price        numeric(20,8) NOT NULL,
  opened_at         timestamptz NOT NULL,
  closed_at         timestamptz NOT NULL,
  gross_pnl_quote   numeric(20,4) NOT NULL,
  fees_base         numeric(20,4) NOT NULL,
  net_pnl_base      numeric(20,4) NOT NULL,
  fx_to_base        numeric(20,10) NOT NULL,
  initial_risk_base numeric(20,4),                 -- distância do SL inicial → R-múltiplo
  r_multiple        numeric(10,4),
  mae_ticks         numeric(12,2),                 -- Maximum Adverse Excursion
  mfe_ticks         numeric(12,2),                 -- Maximum Favorable Excursion
  exit_reason       text NOT NULL CHECK (exit_reason IN
                    ('TAKE_PROFIT','STOP_LOSS','TRAILING','BREAKEVEN','MANUAL','LIQUIDATION','EXPIRY')),
  setup_tags        text[] NOT NULL DEFAULT '{}',
  notes             text
);
CREATE INDEX trades_account_time ON trades (account_id, closed_at);

-- Toda mudança de saldo é um lançamento (auditável, reproduzível)
CREATE TABLE ledger_entries (
  id          bigserial PRIMARY KEY,
  account_id  uuid NOT NULL REFERENCES accounts(id),
  kind        text NOT NULL CHECK (kind IN
              ('INITIAL','REALIZED_PNL','FEE','SWAP','DIVIDEND','JCP','TAX_WITHHELD','RESET','ADJUSTMENT')),
  amount      numeric(20,4) NOT NULL,
  ccy         char(3) NOT NULL,
  fx_to_base  numeric(20,10) NOT NULL,
  amount_base numeric(20,4) NOT NULL,
  ref_type    text,
  ref_id      text,
  created_at  timestamptz NOT NULL
);

CREATE TABLE equity_snapshots (
  account_id  uuid NOT NULL,
  ts          timestamptz NOT NULL,
  balance     numeric(20,2) NOT NULL,
  equity      numeric(20,2) NOT NULL,
  used_margin numeric(20,2) NOT NULL,
  PRIMARY KEY (account_id, ts)
);
SELECT create_hypertable('equity_snapshots', 'ts');   -- curva de capital

-- ───────────── Calendário macro ─────────────
CREATE TABLE economic_events (
  id               bigserial PRIMARY KEY,
  scheduled_at     timestamptz NOT NULL,
  country          char(2) NOT NULL,
  title            text NOT NULL,                  -- Payroll, IPCA, FOMC, Copom
  impact           text NOT NULL CHECK (impact IN ('LOW','MEDIUM','HIGH')),
  forecast         numeric,
  previous         numeric,
  actual           numeric,
  surprise_std     numeric,                        -- desvio histórico da surpresa → z-score
  affected_symbols jsonb NOT NULL,                 -- {"EURUSD":-1,"WDOZ26":0.8}
  shock_profile    jsonb NOT NULL                  -- {"spread_mult":4,"depth_mult":0.2,"vol_mult":4,"half_life_s":90}
);

-- ───────────── Ranking ─────────────
CREATE TABLE leaderboard_entries (
  season_id        int  NOT NULL REFERENCES leaderboard_seasons(id),
  account_id       uuid NOT NULL REFERENCES accounts(id),
  public_alias     text NOT NULL,                  -- desnormalizado; anonimizado na exclusão
  score            numeric(10,4) NOT NULL,
  rank             int,
  return_pct       numeric(10,4),
  max_drawdown_pct numeric(10,4),
  sortino          numeric(10,4),
  profit_factor    numeric(10,4),
  win_rate         numeric(6,4),
  consistency      numeric(6,4),
  trading_days     int,
  trades           int,
  resets           int,
  eligible         boolean NOT NULL DEFAULT false,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (season_id, account_id)
);
CREATE INDEX leaderboard_rank ON leaderboard_entries (season_id, score DESC) WHERE eligible;
