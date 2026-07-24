-- =============================================================================
-- 001_init_schema.sql
-- Schema inicial do banco do Assistente RAG UE5 — Postgres 15+
--
-- Este arquivo é lido automaticamente pela imagem oficial `postgres` na
-- primeira inicialização do volume (ver seção /docker-entrypoint-initdb.d/
-- no docker-compose.yml). Só roda UMA vez — se o volume já existir, o
-- Postgres ignora este script. Para reaplicar em dev, apague o volume.
--
-- Modelagem em 3FN (terceira forma normal) com algumas denormalizações pragmáticas
-- para otimizar consultas de leitura (ex.: v_chat_sessions_summary).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Extensões
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "pgcrypto";      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";       -- busca textual (ILIKE / similaridade) no histórico

-- -----------------------------------------------------------------------------
-- ENUMs — pequenos domínios fechados, não justificam tabela de lookup à parte
-- -----------------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE message_status AS ENUM ('pending', 'streaming', 'success', 'error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE health_dependency_status AS ENUM ('ok', 'degraded', 'error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- -----------------------------------------------------------------------------
-- Função utilitária: atualiza updated_at automaticamente
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- users  (preparado para multi-usuário futuro — hoje nullable em toda FK)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) UNIQUE,
    display_name    VARCHAR(120),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- projects  — normaliza `project_source` (ex.: "UE5_Engine", "Lyra", "Custom")
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id              SMALLSERIAL PRIMARY KEY,
    code            VARCHAR(64) NOT NULL UNIQUE,   -- valor salvo no payload do Qdrant
    display_name    VARCHAR(120) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO projects (code, display_name) VALUES
    ('UE5_Engine', 'UE5 Engine Core'),
    ('Lyra',       'Lyra Sample Project'),
    ('Custom',     'Projeto Personalizado')
ON CONFLICT (code) DO NOTHING;

-- -----------------------------------------------------------------------------
-- doc_types  — normaliza `doc_type` dos chunks recuperados
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS doc_types (
    id              SMALLSERIAL PRIMARY KEY,
    code            VARCHAR(64) NOT NULL UNIQUE,
    display_name    VARCHAR(120) NOT NULL
);

INSERT INTO doc_types (code, display_name) VALUES
    ('cpp_header_api',     'C++ Header API'),
    ('cpp_implementation',  'C++ Implementation'),
    ('blueprint_node',      'Blueprint Node'),
    ('udn_doc',             'UDN Documentation')
ON CONFLICT (code) DO NOTHING;

-- -----------------------------------------------------------------------------
-- chat_sessions  — uma conversa
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID REFERENCES users(id) ON DELETE SET NULL,
    title                   VARCHAR(160) NOT NULL DEFAULT 'Conversa Unreal Engine 5',
    default_model           VARCHAR(120) NOT NULL DEFAULT 'ollama/qwen2.5-coder:7b',
    default_top_k           SMALLINT NOT NULL DEFAULT 5 CHECK (default_top_k BETWEEN 1 AND 20),
    project_source_filter   VARCHAR(64),   -- espelha ChatRequest.project_source_filter (pode ser NULL = "todos")
    is_archived             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
    ON chat_sessions (user_id, updated_at DESC);

DROP TRIGGER IF EXISTS trg_chat_sessions_updated_at ON chat_sessions;
CREATE TRIGGER trg_chat_sessions_updated_at
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- -----------------------------------------------------------------------------
-- chat_messages  — uma mensagem dentro de uma sessão
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role                message_role NOT NULL,
    status              message_status NOT NULL DEFAULT 'success',
    content             TEXT NOT NULL DEFAULT '',
    error_detail        TEXT,
    model_used          VARCHAR(120),
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,

    -- Ordem determinística dentro da sessão (não confiar só em created_at,
    -- que pode colidir em inserts muito rápidos).
    sequence_number     INTEGER NOT NULL,

    -- Snapshot do ChatRequest que gerou esta mensagem — usado pelo botão
    -- "Tentar novamente" (retryMessage) e para debug/auditoria. Não é
    -- decomponível em colunas próprias porque é um payload opaco de request,
    -- não um atributo funcional da mensagem em si.
    request_payload     JSONB,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_chat_messages_session_sequence UNIQUE (session_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session_seq
    ON chat_messages (session_id, sequence_number);

-- Busca textual no histórico (ex.: "onde eu perguntei sobre UPROPERTY?")
CREATE INDEX IF NOT EXISTS idx_chat_messages_content_trgm
    ON chat_messages USING gin (content gin_trgm_ops);

-- -----------------------------------------------------------------------------
-- retrieved_chunks  — chunks de documentação citados numa resposta do assistente
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retrieved_chunks (
    id                  BIGSERIAL PRIMARY KEY,
    message_id          UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,

    -- chunk_id do Qdrant (não é PK aqui — é só uma referência externa ao ponto vetorial)
    qdrant_chunk_id      BIGINT NOT NULL,

    project_id          SMALLINT REFERENCES projects(id),
    doc_type_id         SMALLINT REFERENCES doc_types(id),

    score               REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    title               VARCHAR(255) NOT NULL DEFAULT '',
    source_file         TEXT NOT NULL DEFAULT '',
    ue_version          VARCHAR(16) NOT NULL DEFAULT '',
    summary_markdown    TEXT NOT NULL DEFAULT '',
    raw_content         TEXT NOT NULL DEFAULT '',

    -- Posição do chunk na lista de fontes daquela mensagem (#1, #2, ...)
    rank_position        SMALLINT NOT NULL,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_retrieved_chunks_message_rank UNIQUE (message_id, rank_position)
);

CREATE INDEX IF NOT EXISTS idx_retrieved_chunks_message
    ON retrieved_chunks (message_id);

-- -----------------------------------------------------------------------------
-- health_check_log  — histórico opcional do /health, útil pra observabilidade
-- (não obrigatório pro chat funcionar; incluído porque HealthResponse já
-- existe no contrato da API e vale a pena reter séries temporais)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_check_log (
    id              BIGSERIAL PRIMARY KEY,
    overall_status  health_dependency_status NOT NULL,
    qdrant_status   health_dependency_status NOT NULL,
    redis_status    health_dependency_status NOT NULL,
    litellm_status  health_dependency_status NOT NULL,
    details_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_check_log_checked_at
    ON health_check_log (checked_at DESC);

-- -----------------------------------------------------------------------------
-- View de conveniência: lista de sessões com metadados agregados
-- (equivalente ao que hoje é calculado à mão no frontend em `upsertSessionMeta`)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_chat_sessions_summary AS
SELECT
    s.id,
    s.user_id,
    s.title,
    s.default_model,
    s.project_source_filter,
    s.is_archived,
    s.created_at,
    s.updated_at,
    COUNT(m.id)                         AS message_count,
    MAX(m.created_at)                   AS last_message_at,
    (ARRAY_AGG(m.content ORDER BY m.sequence_number ASC)
        FILTER (WHERE m.role = 'user'))[1] AS first_user_message
FROM chat_sessions s
LEFT JOIN chat_messages m ON m.session_id = s.id
GROUP BY s.id;

COMMIT;