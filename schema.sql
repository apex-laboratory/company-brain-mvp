CREATE EXTENSION IF NOT EXISTS vector;

-- ─────────────────────────────────────────────
-- Core skills registry
-- ─────────────────────────────────────────────

CREATE TABLE skills (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name              VARCHAR(255) UNIQUE NOT NULL,
  version           INTEGER DEFAULT 1,
  trigger           TEXT,
  base_logic        TEXT,
  exceptions_block  JSONB DEFAULT '[]',
  -- [{condition, override, source_url, source_authority, author, date}]
  actions           JSONB DEFAULT '[]',
  -- [{name, params, description}]
  source_ids        JSONB DEFAULT '[]',
  source_authority  VARCHAR(10),
  -- high | medium | low
  conflict_flags    JSONB DEFAULT '[]',
  status            VARCHAR(20) DEFAULT 'draft',
  -- draft | pending_review | published | archived
  confidence        FLOAT,
  embedding         VECTOR(1536),
  changed_by        VARCHAR(50),
  -- system | human_authored | {reviewer_id}
  created_at        TIMESTAMP DEFAULT NOW(),
  updated_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON skills USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON skills (status);
CREATE INDEX ON skills (source_authority);

-- ─────────────────────────────────────────────
-- Full change history for published skills
-- ─────────────────────────────────────────────

CREATE TABLE skill_versions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id          UUID REFERENCES skills(id),
  version           INTEGER,
  base_logic        TEXT,
  exceptions_block  JSONB,
  confidence        FLOAT,
  changed_by        VARCHAR(50),
  change_type       VARCHAR(30),
  -- update | exception_added | human_edit | sweep_sourced
  created_at        TIMESTAMP DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Human-in-the-loop review gate
-- ─────────────────────────────────────────────

CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id        UUID REFERENCES skills(id),
  review_type     VARCHAR(30),
  -- update | exception | contradiction | new | query_driven | sweep_sourced
  proposed_update JSONB,
  source_a        JSONB,
  -- {url, author, timestamp, excerpt, authority, tier}
  source_b        JSONB,
  -- populated only for review_type = contradiction
  confidence      FLOAT,
  reason          TEXT,
  status          VARCHAR(20) DEFAULT 'pending',
  -- pending | approved | rejected | human_written
  resolved_by     VARCHAR(50),
  created_at      TIMESTAMP DEFAULT NOW(),
  resolved_at     TIMESTAMP
);

CREATE INDEX ON review_queue (status);
CREATE INDEX ON review_queue (review_type);

-- ─────────────────────────────────────────────
-- Webhook event log + sweep audit trail
-- ─────────────────────────────────────────────

CREATE TABLE source_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source       VARCHAR(50),
  -- slack | notion | github | jira | zendesk
  event_type   VARCHAR(50),
  -- message | page_update | pr_merged | issue_closed | ticket_resolved | etc
  source_id    VARCHAR(255),
  payload      JSONB,
  processed    BOOLEAN DEFAULT FALSE,
  skill_id     UUID,
  -- set after extraction if a skill was created or updated
  outcome      VARCHAR(30),
  -- published | queued | draft | discarded | duplicate | contradiction
  sweep_id     UUID,
  -- set if this event was processed during an onboarding sweep
  created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON source_events (source, processed);
CREATE INDEX ON source_events (sweep_id);

-- ─────────────────────────────────────────────
-- Onboarding + manual sweep job tracking
-- ─────────────────────────────────────────────

CREATE TABLE sweeps (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  status         VARCHAR(20) DEFAULT 'running',
  -- running | completed | failed | paused
  config         JSONB,
  -- source_authority.yaml contents at time of sweep
  progress       JSONB DEFAULT '{}',
  -- {source: {total, processed, published, queued, discarded}}
  skills_created INTEGER DEFAULT 0,
  skills_queued  INTEGER DEFAULT 0,
  started_at     TIMESTAMP DEFAULT NOW(),
  completed_at   TIMESTAMP
);

-- ─────────────────────────────────────────────
-- Agent query feedback (episodic log)
-- ─────────────────────────────────────────────

CREATE TABLE agent_interactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id           UUID REFERENCES skills(id),
  query              TEXT,
  matched_confidence FLOAT,
  match_type         VARCHAR(20),
  -- semantic | query_driven | no_match
  agent_action       JSONB,
  human_override     BOOLEAN DEFAULT FALSE,
  created_at         TIMESTAMP DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- OAuth token storage + monitored source config
-- ─────────────────────────────────────────────

CREATE TABLE source_connections (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source           VARCHAR(50) UNIQUE NOT NULL,
  -- slack | notion | github | jira | zendesk
  status           VARCHAR(20) DEFAULT 'connected',
  -- connected | disconnected | error
  access_token     TEXT,
  refresh_token    TEXT,
  token_expires_at TIMESTAMP,
  monitored_ids    JSONB DEFAULT '[]',
  -- channel/space/project IDs selected during onboarding
  lookback_days    INTEGER DEFAULT 180,
  connected_at     TIMESTAMP DEFAULT NOW(),
  updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON source_connections (source, status);

-- ─────────────────────────────────────────────
-- Active webhook subscription lifecycle
-- ─────────────────────────────────────────────

CREATE TABLE webhook_subscriptions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source          VARCHAR(50),
  source_ref_id   VARCHAR(255),
  -- ID the source system assigned to this subscription
  target_id       VARCHAR(255),
  -- channel/space/project being monitored
  status          VARCHAR(20) DEFAULT 'active',
  -- active | revoked
  created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON webhook_subscriptions (source, status);
