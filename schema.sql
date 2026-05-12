CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE raw_content (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source         VARCHAR(50),
  source_id      VARCHAR(255),
  content        TEXT,
  metadata       JSONB,
  content_type   VARCHAR(20),
  graph_ingested BOOLEAN DEFAULT FALSE,
  ingested_at    TIMESTAMP DEFAULT NOW(),
  updated_at     TIMESTAMP DEFAULT NOW(),
  CONSTRAINT raw_content_source_id_unique UNIQUE (source, source_id)
);

CREATE INDEX raw_content_source_idx ON raw_content (source);
CREATE INDEX raw_content_metadata_entity_type_idx ON raw_content ((metadata ->> 'entity_type'));
CREATE INDEX raw_content_metadata_parent_idx ON raw_content ((metadata ->> 'parent_source_id'));

CREATE TABLE skills (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name           VARCHAR(255) UNIQUE NOT NULL,
  version        INTEGER DEFAULT 1,
  confidence     FLOAT,
  description    TEXT,
  decision_logic TEXT,
  tool_schemas   JSONB,
  source_ids     JSONB DEFAULT '[]',
  conflict_flags JSONB DEFAULT '[]',
  graph_node_ids JSONB DEFAULT '[]',
  status         VARCHAR(20) DEFAULT 'draft',
  embedding      VECTOR(1536),
  created_at     TIMESTAMP DEFAULT NOW(),
  updated_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON skills USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE skill_versions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id       UUID REFERENCES skills(id),
  version        INTEGER,
  decision_logic TEXT,
  confidence     FLOAT,
  changed_by     VARCHAR(50),
  created_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE review_queue (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id        UUID REFERENCES skills(id),
  proposed_update JSONB,
  confidence      FLOAT,
  reason          TEXT,
  status          VARCHAR(20) DEFAULT 'pending',
  created_at      TIMESTAMP DEFAULT NOW(),
  resolved_at     TIMESTAMP
);

CREATE TABLE agent_interactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  skill_id           UUID REFERENCES skills(id),
  query              TEXT,
  matched_confidence FLOAT,
  graph_path         JSONB,
  agent_action       JSONB,
  human_override     BOOLEAN DEFAULT FALSE,
  created_at         TIMESTAMP DEFAULT NOW()
);
