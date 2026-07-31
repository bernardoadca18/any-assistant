-- 002_domain_multi_tenant_schema.sql
-- ====================================
-- Adiciona isolamento por domain_id (multi-domain: ue5, godot_4, ...) às
-- tabelas que hoje assumem implicitamente um único domínio (UE5).
--
-- Godot 4 passa a ser o domínio default ('godot_4') via DEFAULT — sessões
-- e chunks já existentes (100% UE5) são retroativamente marcados como
-- 'ue5' num UPDATE explícito logo abaixo, ANTES do DEFAULT mudar de alvo
-- para o restante do tráfego novo. Rodar dentro de uma transação.

BEGIN;

-- ---------------------------------------------------------------------
-- 1. chat_sessions
-- ---------------------------------------------------------------------
ALTER TABLE chat_sessions
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NOT NULL DEFAULT 'godot_4';

-- Dados pré-existentes: toda sessão criada antes desta migração era 100%
-- UE5 (não havia outro domínio) — corrige o backfill antes de deixar o
-- DEFAULT valer para linhas novas.
UPDATE chat_sessions SET domain_id = 'ue5' WHERE created_at < now();

CREATE INDEX IF NOT EXISTS ix_chat_sessions_domain_id ON chat_sessions (domain_id);

-- ---------------------------------------------------------------------
-- 2. chat_messages — domain_id herdado da sessão no momento da escrita,
--    denormalizado aqui para permitir filtrar mensagens por domínio sem
--    join, e para preservar o domínio original mesmo se a sessão for
--    reatribuída no futuro (multi-domain por sessão não é suportado hoje,
--    mas a coluna já existe para essa evolução).
-- ---------------------------------------------------------------------
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NOT NULL DEFAULT 'godot_4';

UPDATE chat_messages cm
SET domain_id = cs.domain_id
FROM chat_sessions cs
WHERE cm.session_id = cs.id;

CREATE INDEX IF NOT EXISTS ix_chat_messages_domain_id ON chat_messages (domain_id);

-- ---------------------------------------------------------------------
-- 3. retrieved_chunks
-- ---------------------------------------------------------------------
ALTER TABLE retrieved_chunks
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NOT NULL DEFAULT 'godot_4';

UPDATE retrieved_chunks rc
SET domain_id = cm.domain_id
FROM chat_messages cm
WHERE rc.message_id = cm.id;

CREATE INDEX IF NOT EXISTS ix_retrieved_chunks_domain_id ON retrieved_chunks (domain_id);

-- ---------------------------------------------------------------------
-- 4. projects / doc_types — permitir o MESMO code em domínios diferentes
--    (ex: doc_type "api_reference" poderia existir tanto em ue5 quanto em
--    godot_4 com significados distintos). Troca o UNIQUE simples por um
--    composto (code, domain_id).
-- ---------------------------------------------------------------------
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NOT NULL DEFAULT 'godot_4';
UPDATE projects SET domain_id = 'ue5' WHERE created_at < now() AND domain_id = 'godot_4'
    AND code IN ('UE5_Engine', 'Lyra', 'CitySample', 'VehicleSample');

ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_code_key;
ALTER TABLE projects ADD CONSTRAINT uq_projects_code_domain UNIQUE (code, domain_id);

ALTER TABLE doc_types
    ADD COLUMN IF NOT EXISTS domain_id VARCHAR(64) NOT NULL DEFAULT 'godot_4';
UPDATE doc_types SET domain_id = 'ue5' WHERE domain_id = 'godot_4'
    AND code IN ('cpp_header_api', 'cpp_implementation', 'udn_doc', 'blueprint_asset');

ALTER TABLE doc_types DROP CONSTRAINT IF EXISTS doc_types_code_key;
ALTER TABLE doc_types ADD CONSTRAINT uq_doc_types_code_domain UNIQUE (code, domain_id);

-- ---------------------------------------------------------------------
-- 5. Seed dos doc_types padrão do domínio Godot 4 (idempotente)
-- ---------------------------------------------------------------------
INSERT INTO doc_types (code, display_name, domain_id) VALUES
    ('gdscript_api',    'GDScript Source (.gd)',              'godot_4'),
    ('csharp_api',      'Godot C# Source (.cs)',               'godot_4'),
    ('godot_docs_rst',  'Official Godot Manual (.rst)',        'godot_4'),
    ('scene_structure', 'Scene/Resource Structure (.tscn/.tres)', 'godot_4')
ON CONFLICT (code, domain_id) DO NOTHING;

COMMIT;

-- NOTA (rollback manual, caso necessário):
--   ALTER TABLE retrieved_chunks DROP COLUMN domain_id;
--   ALTER TABLE chat_messages DROP COLUMN domain_id;
--   ALTER TABLE chat_sessions DROP COLUMN domain_id;
--   ALTER TABLE projects DROP CONSTRAINT uq_projects_code_domain, ADD CONSTRAINT projects_code_key UNIQUE (code);
--   ALTER TABLE projects DROP COLUMN domain_id;
--   ALTER TABLE doc_types DROP CONSTRAINT uq_doc_types_code_domain, ADD CONSTRAINT doc_types_code_key UNIQUE (code);
--   ALTER TABLE doc_types DROP COLUMN domain_id;
