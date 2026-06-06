-- ─────────────────────────────────────────────
-- Extensions
-- ─────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─────────────────────────────────────────────
-- ORGANIZATIONS (tenants)
-- ─────────────────────────────────────────────

CREATE TABLE organizations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        VARCHAR(255) NOT NULL,
  slug        VARCHAR(100) UNIQUE NOT NULL,
  plan        VARCHAR(20) DEFAULT 'trial',
  -- trial | starter | growth | enterprise
  plan_seats  INTEGER DEFAULT 5,
  is_active   BOOLEAN DEFAULT TRUE,
  deleted_at  TIMESTAMP,
  -- soft delete — never hard-delete orgs; 5 years of knowledge should not cascade on a misclick
  created_at  TIMESTAMP DEFAULT NOW(),
  updated_at  TIMESTAMP DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- USERS (synced from Supabase auth.users)
-- ─────────────────────────────────────────────

CREATE TABLE users (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_id     UUID UNIQUE NOT NULL,
  -- Supabase auth.users.id
  email       VARCHAR(255) NOT NULL,
  full_name   VARCHAR(255),
  avatar_url  TEXT,
  created_at  TIMESTAMP DEFAULT NOW(),
  updated_at  TIMESTAMP DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- ORGANIZATION MEMBERS + RBAC
-- ─────────────────────────────────────────────

CREATE TYPE member_role AS ENUM ('owner', 'admin', 'editor', 'viewer');

CREATE TABLE organization_members (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        member_role NOT NULL DEFAULT 'viewer',
  is_active   BOOLEAN DEFAULT TRUE,
  invited_by  UUID REFERENCES users(id),
  joined_at   TIMESTAMP DEFAULT NOW(),
  UNIQUE (org_id, user_id)
);

CREATE INDEX ON organization_members (org_id, user_id);
CREATE INDEX ON organization_members (user_id);

-- ─────────────────────────────────────────────
-- INVITATIONS (employee onboarding)
-- ─────────────────────────────────────────────

CREATE TABLE invitations (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email        VARCHAR(255) NOT NULL,
  role         member_role NOT NULL DEFAULT 'viewer',
  -- API generates raw token, emails it, stores only sha256 hash here — raw token never persisted
  token_hash   BYTEA UNIQUE NOT NULL,
  invited_by   UUID NOT NULL REFERENCES users(id),
  expires_at   TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '7 days',
  accepted_at  TIMESTAMP,
  created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON invitations (org_id, email);
-- No index needed: UNIQUE on token_hash already creates one

-- ─────────────────────────────────────────────
-- SOURCE CONNECTIONS (OAuth, per org, encrypted tokens)
-- ─────────────────────────────────────────────

CREATE TABLE source_connections (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id               UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source               VARCHAR(50) NOT NULL,
  -- slack | notion | github | jira | zendesk
  status               VARCHAR(20) DEFAULT 'connected',
  -- connected | disconnected | error | pending
  access_token_enc     BYTEA,
  -- pgcrypto encrypted; never store plaintext
  refresh_token_enc    BYTEA,
  token_expires_at     TIMESTAMP,
  scopes               TEXT[],
  external_account_id  VARCHAR(255),
  -- workspace/org ID from the source platform
  monitored_ids        JSONB DEFAULT '[]',
  -- channel/space/project IDs selected during onboarding
  lookback_days        INTEGER DEFAULT 180,
  connected_by         UUID REFERENCES users(id),
  connected_at         TIMESTAMP DEFAULT NOW(),
  updated_at           TIMESTAMP DEFAULT NOW(),
  -- external_account_id lets a company connect multiple workspaces of the same source
  UNIQUE (org_id, source, external_account_id)
);

CREATE INDEX ON source_connections (org_id, source, status);

-- ─────────────────────────────────────────────
-- WEBHOOK SUBSCRIPTIONS (per org)
-- ─────────────────────────────────────────────

CREATE TABLE webhook_subscriptions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source         VARCHAR(50) NOT NULL,
  source_ref_id  VARCHAR(255),
  -- subscription ID assigned by the source platform
  target_id      VARCHAR(255),
  -- channel/space/project being monitored
  secret_enc     BYTEA,
  -- encrypted signing secret used to verify incoming payloads
  status         VARCHAR(20) DEFAULT 'active',
  -- active | revoked | error
  created_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON webhook_subscriptions (org_id, source, status);

-- ─────────────────────────────────────────────
-- SKILLS (knowledge base, per org)
-- ─────────────────────────────────────────────

CREATE TABLE skills (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name              VARCHAR(255) NOT NULL,
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
  changed_by        UUID REFERENCES users(id),
  deleted_at        TIMESTAMP,
  -- soft delete — skills are enterprise knowledge; hard deletes need a deliberate restore window
  created_at        TIMESTAMP DEFAULT NOW(),
  updated_at        TIMESTAMP DEFAULT NOW(),
  UNIQUE (org_id, name)
);

-- HNSW outperforms IVFFlat on dynamic datasets (no VACUUM needed after inserts)
CREATE INDEX skills_embedding_hnsw ON skills USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON skills (org_id, status);
CREATE INDEX ON skills (org_id, source_authority);

-- ─────────────────────────────────────────────
-- SKILL VERSIONS (full history, per org)
-- ─────────────────────────────────────────────

CREATE TABLE skill_versions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  skill_id          UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  version           INTEGER NOT NULL,
  base_logic        TEXT,
  exceptions_block  JSONB,
  confidence        FLOAT,
  changed_by        UUID REFERENCES users(id),
  change_type       VARCHAR(30),
  -- update | exception_added | human_edit | sweep_sourced
  created_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON skill_versions (org_id, skill_id);

-- ─────────────────────────────────────────────
-- REVIEW QUEUE (human-in-the-loop gate, per org)
-- ─────────────────────────────────────────────

CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  skill_id        UUID REFERENCES skills(id) ON DELETE SET NULL,
  review_type     VARCHAR(30) NOT NULL,
  -- update | exception | contradiction | new | query_driven | sweep_sourced
  proposed_update JSONB,
  source_a        JSONB,
  -- {url, author, timestamp, excerpt, authority, tier}
  source_b        JSONB,
  -- populated for review_type = contradiction
  confidence      FLOAT,
  reason          TEXT,
  status          VARCHAR(20) DEFAULT 'pending',
  -- pending | approved | rejected | human_written
  resolved_by     UUID REFERENCES users(id),
  created_at      TIMESTAMP DEFAULT NOW(),
  resolved_at     TIMESTAMP
);

CREATE INDEX ON review_queue (org_id, status);
CREATE INDEX ON review_queue (org_id, review_type);

-- ─────────────────────────────────────────────
-- SOURCE EVENTS (webhook + sweep audit trail, per org)
-- ─────────────────────────────────────────────

CREATE TABLE source_events (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  source            VARCHAR(50) NOT NULL,
  -- slack | notion | github | jira | zendesk
  event_type        VARCHAR(50) NOT NULL,
  source_id         VARCHAR(255),
  external_event_id VARCHAR(255),
  -- provider-assigned event ID; used for deduplication (webhook providers retry aggressively)
  payload           JSONB,
  processed         BOOLEAN DEFAULT FALSE,
  skill_id          UUID REFERENCES skills(id) ON DELETE SET NULL,
  outcome           VARCHAR(30),
  -- published | queued | draft | discarded | duplicate | contradiction
  sweep_id          UUID,
  created_at        TIMESTAMP DEFAULT NOW(),
  UNIQUE (org_id, source, external_event_id)
);

CREATE INDEX ON source_events (org_id, source, processed);
CREATE INDEX ON source_events (org_id, sweep_id);

-- ─────────────────────────────────────────────
-- SWEEPS (onboarding + manual jobs, per org)
-- ─────────────────────────────────────────────

CREATE TABLE sweeps (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  status         VARCHAR(20) DEFAULT 'running',
  -- running | completed | failed | paused
  config         JSONB,
  -- source_authority config at time of sweep
  progress       JSONB DEFAULT '{}',
  -- {source: {total, processed, published, queued, discarded}}
  skills_created INTEGER DEFAULT 0,
  skills_queued  INTEGER DEFAULT 0,
  triggered_by   UUID REFERENCES users(id),
  started_at     TIMESTAMP DEFAULT NOW(),
  completed_at   TIMESTAMP
);

CREATE INDEX ON sweeps (org_id, status);

-- ─────────────────────────────────────────────
-- AGENT INTERACTIONS (episodic query log, per org)
-- ─────────────────────────────────────────────

CREATE TABLE agent_interactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id             UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id            UUID REFERENCES users(id),
  skill_id           UUID REFERENCES skills(id) ON DELETE SET NULL,
  query              TEXT,
  matched_confidence FLOAT,
  match_type         VARCHAR(20),
  -- semantic | query_driven | no_match
  agent_action       JSONB,
  human_override     BOOLEAN DEFAULT FALSE,
  created_at         TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON agent_interactions (org_id, user_id);
CREATE INDEX ON agent_interactions (org_id, skill_id);

-- ─────────────────────────────────────────────
-- AUDIT LOG (append-only, immutable record of all actions)
-- ─────────────────────────────────────────────

CREATE TABLE audit_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      UUID NOT NULL REFERENCES organizations(id),
  user_id     UUID REFERENCES users(id),
  action      VARCHAR(100) NOT NULL,
  -- e.g. skill.published | sweep.started | member.invited | connection.revoked
  resource    VARCHAR(50),
  resource_id UUID,
  old_value   JSONB,
  new_value   JSONB,
  ip_address  INET,
  user_agent  TEXT,
  created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON audit_log (org_id, created_at DESC);
CREATE INDEX ON audit_log (org_id, resource, resource_id);

-- ─────────────────────────────────────────────
-- ROW LEVEL SECURITY
-- ─────────────────────────────────────────────

ALTER TABLE organizations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_members  ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations           ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_connections    ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE skills                ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_versions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_queue          ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_events         ENABLE ROW LEVEL SECURITY;
ALTER TABLE sweeps                ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_interactions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log             ENABLE ROW LEVEL SECURITY;

-- Read org_id from the JWT claim set by Supabase auth
CREATE OR REPLACE FUNCTION current_org_id()
RETURNS UUID AS $$
  SELECT (current_setting('request.jwt.claims', true)::jsonb ->> 'org_id')::UUID;
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public;

-- Read the calling user's role within their org
-- Must join through users because organization_members.user_id is our internal UUID,
-- while auth.uid() returns the Supabase auth_id (users.auth_id)
CREATE OR REPLACE FUNCTION current_member_role()
RETURNS member_role AS $$
  SELECT om.role
  FROM organization_members om
  JOIN users u ON u.id = om.user_id
  WHERE om.org_id = current_org_id()
    AND u.auth_id = auth.uid()
    AND om.is_active = TRUE
  LIMIT 1;
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public;

-- Organizations: members see only their own org
CREATE POLICY org_select ON organizations FOR SELECT
  USING (id = current_org_id());

-- Members: all org members can see the roster
CREATE POLICY members_select ON organization_members FOR SELECT
  USING (org_id = current_org_id());

-- Members: only owners/admins can invite or change roles
CREATE POLICY members_insert ON organization_members FOR INSERT
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

CREATE POLICY members_update ON organization_members FOR UPDATE
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- Invitations: owners/admins only — regular members must not read token_hash (usable credential)
-- Acceptance flow uses a SECURITY DEFINER function that validates the raw token directly (no RLS)
CREATE POLICY invitations_select ON invitations FOR SELECT
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

CREATE POLICY invitations_write ON invitations FOR INSERT
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- Source connections: admins/owners only (contain encrypted secrets)
CREATE POLICY connections_select ON source_connections FOR SELECT
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

CREATE POLICY connections_write ON source_connections FOR ALL
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- Webhook subscriptions: admins/owners only
CREATE POLICY webhooks_policy ON webhook_subscriptions FOR ALL
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- Skills: all members read; editors and above write
CREATE POLICY skills_select ON skills FOR SELECT
  USING (org_id = current_org_id());

CREATE POLICY skills_insert ON skills FOR INSERT
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin', 'editor'));

CREATE POLICY skills_update ON skills FOR UPDATE
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin', 'editor'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin', 'editor'));

-- Skill versions: all members read (read-only; written by system)
CREATE POLICY skill_versions_select ON skill_versions FOR SELECT
  USING (org_id = current_org_id());

-- Review queue: all members read; editors and above resolve
CREATE POLICY review_select ON review_queue FOR SELECT
  USING (org_id = current_org_id());

CREATE POLICY review_update ON review_queue FOR UPDATE
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin', 'editor'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin', 'editor'));

-- Source events, sweeps, interactions: org isolation (fine-grained at API layer)
CREATE POLICY events_policy ON source_events FOR ALL
  USING (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

CREATE POLICY sweeps_policy ON sweeps FOR ALL
  USING (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

CREATE POLICY interactions_policy ON agent_interactions FOR ALL
  USING (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- Audit log: all members read; append-only (no UPDATE or DELETE policy = impossible)
CREATE POLICY audit_select ON audit_log FOR SELECT
  USING (org_id = current_org_id());

CREATE POLICY audit_insert ON audit_log FOR INSERT
  WITH CHECK (org_id = current_org_id());

-- ─────────────────────────────────────────────
-- ORGANIZATION SETTINGS (structured config, replaces metadata JSONB blob)
-- ─────────────────────────────────────────────

CREATE TABLE organization_settings (
  org_id            UUID PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
  -- Promoted columns: frequently queried / enforced at runtime — avoid JSON parsing in hot paths
  max_seats         INTEGER DEFAULT 5,
  max_skills        INTEGER DEFAULT 500,
  max_sweeps_per_day INTEGER DEFAULT 3,
  retention_days    INTEGER DEFAULT 365,
  sso_enabled       BOOLEAN DEFAULT FALSE,
  sso_provider      VARCHAR(20),
  -- saml | oidc
  -- Extensibility blobs: rarely queried, schema evolves without migrations
  branding          JSONB DEFAULT '{}',
  -- logo_url, primary_color, etc.
  sso_config        JSONB DEFAULT '{}',
  -- SAML/OIDC endpoints, certs (encrypted at app layer)
  features          JSONB DEFAULT '{}',
  -- per-plan feature flags
  updated_at        TIMESTAMP DEFAULT NOW()
);

ALTER TABLE organization_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY settings_select ON organization_settings FOR SELECT
  USING (org_id = current_org_id());

CREATE POLICY settings_write ON organization_settings FOR ALL
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- ─────────────────────────────────────────────
-- ORGANIZATION API KEYS (for MCP / LangGraph / n8n / agent access)
-- ─────────────────────────────────────────────

CREATE TABLE organization_api_keys (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name         VARCHAR(100) NOT NULL,
  -- human label, e.g. "n8n production", "LangGraph staging"
  key_hash     BYTEA NOT NULL,
  -- sha256 of the raw key — raw key shown once at creation, never stored
  key_prefix   VARCHAR(10) NOT NULL,
  -- first 8 chars of raw key for identification in UI (e.g. "sk-abc123")
  scopes       TEXT[] DEFAULT '{"read"}',
  -- read | write | admin
  last_used_at TIMESTAMP,
  expires_at   TIMESTAMP,
  -- NULL = no expiry
  revoked_at   TIMESTAMP,
  created_by   UUID NOT NULL REFERENCES users(id),
  created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON organization_api_keys (org_id);
CREATE INDEX ON organization_api_keys (key_hash);

ALTER TABLE organization_api_keys ENABLE ROW LEVEL SECURITY;

-- Only owners/admins can manage API keys
CREATE POLICY api_keys_select ON organization_api_keys FOR SELECT
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

CREATE POLICY api_keys_write ON organization_api_keys FOR ALL
  USING (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'))
  WITH CHECK (org_id = current_org_id()
    AND current_member_role() IN ('owner', 'admin'));

-- ─────────────────────────────────────────────
-- ORGANIZATION USAGE (metering for billing + plan limits)
-- ─────────────────────────────────────────────

CREATE TABLE organization_usage (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  period_start   DATE NOT NULL,
  period_end     DATE NOT NULL,
  skills_total   INTEGER DEFAULT 0,
  sweeps_run     INTEGER DEFAULT 0,
  queries_total  INTEGER DEFAULT 0,
  tokens_used    BIGINT DEFAULT 0,
  embeddings_run INTEGER DEFAULT 0,
  connector_syncs JSONB DEFAULT '{}',
  -- {slack: 12, github: 4, jira: 8}
  created_at     TIMESTAMP DEFAULT NOW(),
  updated_at     TIMESTAMP DEFAULT NOW(),
  UNIQUE (org_id, period_start)
);

CREATE INDEX ON organization_usage (org_id, period_start DESC);

ALTER TABLE organization_usage ENABLE ROW LEVEL SECURITY;

-- All org members can view their own usage (useful in billing page)
CREATE POLICY usage_select ON organization_usage FOR SELECT
  USING (org_id = current_org_id());

-- Written by system/backend only — no user-facing INSERT/UPDATE policies
-- (enforce at API layer; usage rows are upserted by the backend service role)
