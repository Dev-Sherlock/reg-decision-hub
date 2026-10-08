-- ---------------------------------------------------------------------------
-- Database initialization
-- Runs once, automatically, on first PostgreSQL container startup.
--
-- This file mirrors what src/api/database.py:init_db() does. init_db() is the
-- authority (it runs on every API start and applies migrations), but seeding
-- here means a fresh `docker compose up` has a usable schema even before the
-- API is healthy.
--
-- NOTE: the image must be pgvector/pgvector:pg16 — the stock postgres image
-- has no `vector` extension. See docker-compose.yml.
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Symbolic facts — the universal memory store.
--
-- A fact is (subject, predicate) -> value. Values are JSONB so any JSON type
-- works: string, number, boolean, null, array, object.
-- `embedding` holds the sentence-transformers vector used for semantic search
-- and is nullable so a fact can be stored before the embedding model is
-- available (or when the model fails to download — see AGENTS.md).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbolic_facts (
    subject        TEXT NOT NULL,
    predicate      TEXT NOT NULL,
    value          JSONB,
    embedding      VECTOR(384),
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (subject, predicate)
);

CREATE INDEX IF NOT EXISTS idx_symbolic_facts_subject ON symbolic_facts(subject);
CREATE INDEX IF NOT EXISTS idx_symbolic_facts_predicate ON symbolic_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_symbolic_facts_updated ON symbolic_facts(updated_at);
-- Cosine-distance index for ORDER BY embedding <=> query. Rows with NULL
-- embeddings are simply not indexed, so keyword fallbacks still work.
CREATE INDEX IF NOT EXISTS idx_symbolic_facts_embedding
    ON symbolic_facts USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- User-defined inference rules, stored as Prolog source text.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbolic_rules (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    head        TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_symbolic_rules_name ON symbolic_rules(name);

-- ---------------------------------------------------------------------------
-- Audit trail for every fact, rule and node-override change.
--
-- entity_type: 'fact' | 'rule' | 'node'
-- operation:   'INSERT' | 'UPDATE' | 'DELETE'
--
-- subject/predicate locate a fact; rule_name locates a rule; for a node
-- override, subject holds the node id and old/new value hold the actions.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id           SERIAL PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    subject      TEXT,
    predicate    TEXT,
    rule_name    TEXT,
    operation    TEXT NOT NULL,
    old_value    JSONB,
    new_value    JSONB,
    owner        TEXT,
    timestamp    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    diff_hash    TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_subject ON audit_log(subject);
CREATE INDEX IF NOT EXISTS idx_audit_log_rule_name ON audit_log(rule_name);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity_type ON audit_log(entity_type);

-- Convenience view: how many times each subject was touched, by whom.
CREATE OR REPLACE VIEW audit_summary AS
SELECT
    entity_type,
    COALESCE(subject, rule_name) AS subject,
    COALESCE(owner, 'unknown')   AS owner,
    COUNT(*)                     AS change_count,
    MIN(timestamp)               AS first_change,
    MAX(timestamp)               AS last_change
FROM audit_log
GROUP BY entity_type, COALESCE(subject, rule_name), COALESCE(owner, 'unknown');
