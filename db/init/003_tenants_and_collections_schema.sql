-- =============================================================================
-- 003_tenants_and_collections_schema.sql
-- Multi-tenant infrastructure — tenants, collections, configurations, API keys
--
-- This migration introduces full multi-tenancy support with:
-- - tenants: Isolated customer/workspace entities
-- - collections: Qdrant collection configurations per tenant
-- - tenant_configs: Normalized configuration storage (RAG, ingestion, embedding)
-- - api_keys: Secure API key storage for tenant authentication
--
-- Design principles:
-- - 3NF compliant: No redundant data, proper normalization
-- - Foreign key integrity with appropriate cascade rules
-- - Composite unique constraints for business logic
-- - Indexes for query optimization
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- tenants — top-level isolation unit
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            VARCHAR(64) NOT NULL,
    name            VARCHAR(128) NOT NULL,
    description     TEXT DEFAULT '',

    -- Status
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,

    -- Rate limiting (enforced at application layer)
    rate_limit_rpm  INTEGER CHECK (rate_limit_rpm IS NULL OR rate_limit_rpm > 0),
    rate_limit_tpm  INTEGER CHECK (rate_limit_tpm IS NULL OR rate_limit_tpm > 0),

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tenants_slug UNIQUE (slug)
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants (slug);
CREATE INDEX IF NOT EXISTS idx_tenants_is_active ON tenants (is_active);

DROP TRIGGER IF EXISTS trg_tenants_updated_at ON tenants;
CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- collections — Qdrant collection configuration per tenant
-- Each tenant can have multiple collections (e.g., ue5_docs, custom_docs)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Collection identity
    name                    VARCHAR(64) NOT NULL,  -- Logical name within tenant
    qdrant_collection_name  VARCHAR(128) NOT NULL, -- Actual Qdrant collection name

    -- Domain association (links to app.core.domains.registry)
    domain_id               VARCHAR(64) NOT NULL DEFAULT 'godot_4',

    -- Display metadata
    display_name            VARCHAR(128) NOT NULL,
    description             TEXT DEFAULT '',

    -- Default filter for this collection
    default_project_source_filter VARCHAR(64),

    -- Status
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,

    -- Metadata
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Composite unique: collection name must be unique within tenant
    CONSTRAINT uq_collections_tenant_name UNIQUE (tenant_id, name),
    -- Composite unique: Qdrant collection name must be globally unique
    CONSTRAINT uq_collections_qdrant_name UNIQUE (qdrant_collection_name)
);

CREATE INDEX IF NOT EXISTS idx_collections_tenant_id ON collections (tenant_id);
CREATE INDEX IF NOT EXISTS idx_collections_domain_id ON collections (domain_id);
CREATE INDEX IF NOT EXISTS idx_collections_is_active ON collections (is_active);

DROP TRIGGER IF EXISTS trg_collections_updated_at ON collections;
CREATE TRIGGER trg_collections_updated_at
    BEFORE UPDATE ON collections
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- embedding_configs — embedding model configuration (normalized)
-- Can be shared across collections via embedding_config_id FK
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embedding_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Configuration name for reference
    name            VARCHAR(64) NOT NULL,

    -- LiteLLM model identifier
    model           VARCHAR(128) NOT NULL DEFAULT 'ollama-embed',

    -- Vector dimension (must match model output)
    dimension       INTEGER NOT NULL DEFAULT 1024 CHECK (dimension > 0),

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_embedding_configs_tenant_name UNIQUE (tenant_id, name)
);

DROP TRIGGER IF EXISTS trg_embedding_configs_updated_at ON embedding_configs;
CREATE TRIGGER trg_embedding_configs_updated_at
    BEFORE UPDATE ON embedding_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- rag_configs — RAG pipeline configuration (normalized)
-- Query decomposition, retrieval, reranking settings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rag_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    name            VARCHAR(64) NOT NULL,

    -- Query Decomposition
    decomposition_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    decomposition_model         VARCHAR(128) NOT NULL DEFAULT 'gemini-3.5-flash-lite',
    decomposition_timeout       REAL NOT NULL DEFAULT 12.0 CHECK (decomposition_timeout >= 1.0 AND decomposition_timeout <= 30.0),

    -- Retrieval
    top_k                       INTEGER NOT NULL DEFAULT 5 CHECK (top_k >= 1 AND top_k <= 20),
    retrieval_limit_per_query   INTEGER NOT NULL DEFAULT 15 CHECK (retrieval_limit_per_query >= 5 AND retrieval_limit_per_query <= 50),
    score_threshold             REAL NOT NULL DEFAULT 0.6 CHECK (score_threshold >= 0.0 AND score_threshold <= 1.0),

    -- Hybrid Search
    hybrid_search_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    decomposition_max_keywords  INTEGER NOT NULL DEFAULT 6 CHECK (decomposition_max_keywords >= 0 AND decomposition_max_keywords <= 20),

    -- FlashRank Reranker
    reranker_enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    reranker_model              VARCHAR(128) NOT NULL DEFAULT 'ms-marco-MiniLM-L-12-v2',
    reranker_cache_dir          VARCHAR(256) NOT NULL DEFAULT '/tmp/flashrank_cache',
    rerank_min_score            REAL NOT NULL DEFAULT 0.2,  -- CORREÇÃO (Bug 2): Calibrado empiricamente a partir de ~12 testes. Ver rag_service.py para detalhes.

    -- LLM Generation Limits
    max_output_tokens           INTEGER NOT NULL DEFAULT 4096,  -- CORREÇÃO (Bug 1): Limite de tokens na resposta. Calibrado empiricamente (respostas de guia frequentemente ultrapassam 2048 mas raramente passam de ~3500-4000).

    -- Custom system prompt override (NULL = use domain default)
    system_prompt               TEXT,

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_rag_configs_tenant_name UNIQUE (tenant_id, name)
);

DROP TRIGGER IF EXISTS trg_rag_configs_updated_at ON rag_configs;
CREATE TRIGGER trg_rag_configs_updated_at
    BEFORE UPDATE ON rag_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- ingestion_configs — Ingestion pipeline configuration (normalized)
-- Code-to-Doc transformation, batching, concurrency settings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    name            VARCHAR(64) NOT NULL,

    -- Documenter LLM
    documenter_model            VARCHAR(128) NOT NULL DEFAULT 'qwen-local',

    -- Batching
    batch_size                  INTEGER NOT NULL DEFAULT 32 CHECK (batch_size >= 1 AND batch_size <= 256),
    embedding_concurrency       INTEGER NOT NULL DEFAULT 8 CHECK (embedding_concurrency >= 1 AND embedding_concurrency <= 32),

    -- Chunking limits
    max_chunk_chars             INTEGER NOT NULL DEFAULT 3000 CHECK (max_chunk_chars >= 500 AND max_chunk_chars <= 20000),
    max_header_chars            INTEGER NOT NULL DEFAULT 12000 CHECK (max_header_chars >= 1000),

    -- Failure handling
    max_consecutive_failures    INTEGER NOT NULL DEFAULT 3 CHECK (max_consecutive_failures >= 1 AND max_consecutive_failures <= 20),
    lock_timeout_seconds        INTEGER NOT NULL DEFAULT 3600 CHECK (lock_timeout_seconds >= 60),

    -- Default engine version
    default_ue_version          VARCHAR(16) NOT NULL DEFAULT '5.8',

    -- Source code extensions (stored as JSONB array for flexibility)
    source_code_extensions          JSONB NOT NULL DEFAULT '[".h", ".cpp"]'::jsonb,
    source_code_sample_extensions   JSONB NOT NULL DEFAULT '[".h", ".cpp", ".bp.json"]'::jsonb,
    documentation_extensions        JSONB NOT NULL DEFAULT '[".udn"]'::jsonb,

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_ingestion_configs_tenant_name UNIQUE (tenant_id, name)
);

DROP TRIGGER IF EXISTS trg_ingestion_configs_updated_at ON ingestion_configs;
CREATE TRIGGER trg_ingestion_configs_updated_at
    BEFORE UPDATE ON ingestion_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- litellm_configs — LiteLLM proxy configuration (normalized)
-- Optional per-tenant override of LiteLLM settings
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS litellm_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    name            VARCHAR(64) NOT NULL,

    base_url        VARCHAR(256) NOT NULL DEFAULT 'http://litellm-proxy:4000',
    master_key      VARCHAR(256),  -- Encrypted at application layer
    max_retries     INTEGER NOT NULL DEFAULT 4 CHECK (max_retries >= 1 AND max_retries <= 10),

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_litellm_configs_tenant_name UNIQUE (tenant_id, name)
);

DROP TRIGGER IF EXISTS trg_litellm_configs_updated_at ON litellm_configs;
CREATE TRIGGER trg_litellm_configs_updated_at
    BEFORE UPDATE ON litellm_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- collection_configs — Junction table linking collections to configurations
-- Allows collections to share configs or have custom overrides
-- NULL FK = use tenant default (resolved at application layer)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_configs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id           UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,

    -- Optional config overrides (NULL = inherit from tenant default)
    embedding_config_id     UUID REFERENCES embedding_configs(id) ON DELETE SET NULL,
    rag_config_id           UUID REFERENCES rag_configs(id) ON DELETE SET NULL,
    ingestion_config_id     UUID REFERENCES ingestion_configs(id) ON DELETE SET NULL,
    litellm_config_id       UUID REFERENCES litellm_configs(id) ON DELETE SET NULL,

    -- Custom system prompt override for this specific collection
    system_prompt_override  TEXT,

    -- Metadata
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_collection_configs_collection UNIQUE (collection_id)
);

DROP TRIGGER IF EXISTS trg_collection_configs_updated_at ON collection_configs;
CREATE TRIGGER trg_collection_configs_updated_at
    BEFORE UPDATE ON collection_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- tenant_default_configs — Default configurations for a tenant
-- Used when collection_configs FK is NULL
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant_default_configs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Default config references (NULL = use system default from settings)
    default_embedding_config_id     UUID REFERENCES embedding_configs(id) ON DELETE SET NULL,
    default_rag_config_id           UUID REFERENCES rag_configs(id) ON DELETE SET NULL,
    default_ingestion_config_id     UUID REFERENCES ingestion_configs(id) ON DELETE SET NULL,
    default_litellm_config_id       UUID REFERENCES litellm_configs(id) ON DELETE SET NULL,

    -- Default collection for this tenant (used when not specified in request)
    default_collection_id           UUID REFERENCES collections(id) ON DELETE SET NULL,

    -- Metadata
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tenant_default_configs_tenant UNIQUE (tenant_id)
);

DROP TRIGGER IF EXISTS trg_tenant_default_configs_updated_at ON tenant_default_configs;
CREATE TRIGGER trg_tenant_default_configs_updated_at
    BEFORE UPDATE ON tenant_default_configs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- api_keys — Secure API key storage for tenant authentication
-- Keys are stored as bcrypt hashes, never in plaintext
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- Key identifier (shown in UI, used for key rotation)
    name            VARCHAR(64) NOT NULL,

    -- Key prefix for identification (first 8 chars of original key)
    key_prefix      VARCHAR(8) NOT NULL,

    -- Bcrypt hash of the API key (cost factor 12)
    key_hash        VARCHAR(128) NOT NULL,

    -- Permissions (for future use)
    is_admin        BOOLEAN NOT NULL DEFAULT FALSE,
    is_read_only    BOOLEAN NOT NULL DEFAULT FALSE,

    -- Status
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at      TIMESTAMPTZ,  -- NULL = never expires

    -- Last usage tracking
    last_used_at    TIMESTAMPTZ,
    last_used_ip    INET,

    -- Metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_api_keys_tenant_name UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id ON api_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_prefix ON api_keys (key_prefix);
CREATE INDEX IF NOT EXISTS idx_api_keys_is_active ON api_keys (is_active);

DROP TRIGGER IF EXISTS trg_api_keys_updated_at ON api_keys;
CREATE TRIGGER trg_api_keys_updated_at
    BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- Update chat_sessions to reference tenant
-- -----------------------------------------------------------------------------
ALTER TABLE chat_sessions
    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant_id ON chat_sessions (tenant_id);

-- -----------------------------------------------------------------------------
-- Seed default tenant (for backward compatibility with existing data)
-- -----------------------------------------------------------------------------
INSERT INTO tenants (id, slug, name, description, is_active)
VALUES (
    '00000000-0000-0000-0000-000000000001'::uuid,
    'default',
    'Default Tenant',
    'Default tenant for backward compatibility with existing data',
    TRUE
)
ON CONFLICT (slug) DO NOTHING;

-- Update existing sessions to belong to default tenant
UPDATE chat_sessions
SET tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
WHERE tenant_id IS NULL;

-- Add NOT NULL constraint after backfill
ALTER TABLE chat_sessions
    ALTER COLUMN tenant_id SET NOT NULL;

COMMIT;

-- =============================================================================
-- Rollback script (for reference, do not execute automatically)
-- =============================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS tenant_default_configs;
-- DROP TABLE IF EXISTS collection_configs;
-- DROP TABLE IF EXISTS api_keys;
-- DROP TABLE IF EXISTS litellm_configs;
-- DROP TABLE IF EXISTS ingestion_configs;
-- DROP TABLE IF EXISTS rag_configs;
-- DROP TABLE IF EXISTS embedding_configs;
-- DROP TABLE IF EXISTS collections;
-- DROP TABLE IF EXISTS tenants;
-- ALTER TABLE chat_sessions DROP COLUMN IF EXISTS tenant_id;
-- COMMIT;
