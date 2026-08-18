[English](./README.md) | Português (BR)

---

# Any Assistant AI

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![React](https://img.shields.io/badge/React-19-61DAFB)
![Status](https://img.shields.io/badge/status-active--development-yellow)

> Uma plataforma de assistente IA multi-tenant e multi-domínio, alimentada por Retrieval-Augmented Generation (RAG) avançado. Atualmente suporta **Unreal Engine 5** e **Godot 4**, com arquitetura extensível para motores de jogos adicionais e domínios técnicos.

## Visão Geral

Any Assistant AI é um sistema pronto para produção, projetado para responder perguntas técnicas sobre arquitetura de motores de jogos, APIs e boas práticas, ancorando respostas de LLMs em uma base de conhecimento semanticamente curada. Diferente de sistemas RAG ingênuos que embedam código bruto diretamente, este projeto transforma código-fonte e documentação em documentação técnica gerada por LLM antes da indexação—garantindo densidade semântica para retrieval confiável enquanto preserva o conteúdo original como camada de referência autoritativa.

### Diferenciais-Chave

- **Transformação Code-to-Doc** — C++ bruto, Blueprints e GDScript nunca são embedados diretamente. Em vez disso, documentação gerada por LLM é usada para retrieval semântico, com fonte original preservada para referência de sintaxe exata.
- **Pipeline de Advanced RAG** — Decomposição de queries, busca híbrida (vetores densos + esparsos), reranking com cross-encoder e suporte multi-idioma entregam respostas de alta precisão.
- **Arquitetura Multi-Tenant** — Coleções isoladas por tenant com configuração hierárquica (coleção → tenant → global) e autenticação via API keys.
- **Suporte Multi-Domínio** — Perfis de domínio extensíveis permitem suporte a múltiplos motores de jogos. Atualmente inclui perfis para Godot 4 e Unreal Engine 5.
- **Infraestrutura Pronta para Produção** — Orquestração Docker Compose com health checks, limits de recursos e armazenamento persistente.

---

## Sumário

- [Funcionalidades](#funcionalidades)
- [Visão Geral da Arquitetura](#visão-geral-da-arquitetura)
- [Stack de Tecnologias](#stack-de-tecnologias)
- [Componentes Principais](#componentes-principais)
  - [Pipeline de Advanced RAG](#1-pipeline-de-advanced-rag)
  - [Motor de Ingestão Code-to-Doc](#2-motor-de-ingestão-code-to-doc)
  - [Sistema Multi-Tenant](#3-sistema-multi-tenant)
  - [Arquitetura Multi-Domínio](#4-arquitetura-multi-domínio)
  - [Interface Web](#5-interface-web)
- [Referência da API](#referência-da-api)
- [Schema do Banco de Dados](#schema-do-banco-de-dados)
- [Início Rápido](#início-rápido)
- [Configuração](#configuração)
- [Desenvolvimento](#desenvolvimento)
- [Estrutura do Projeto](#estrutura-do-projeto)
- [Roadmap](#roadmap)
- [Contribuindo](#contribuindo)
- [Licença](#licença)

---

## Funcionalidades

### Pipeline RAG
- **Decomposição de Queries** — Perguntas complexas são divididas em sub-queries focadas para retrieval abrangente
- **Suporte Multi-Idioma** — Queries em qualquer idioma são traduzidas para inglês para busca vetorial, com geração de resposta consciente do idioma
- **Busca Híbrida** — Combina similaridade de vetores densos com matching de palavras-chave sparse/BM25-like via Reciprocal Rank Fusion (RRF)
- **Reranking com Cross-Encoder** — Reranker FlashRank baseado em ONNX pontua chunks contra a intenção completa da query (apenas CPU, ~10-50ms)
- **Respostas em Streaming** — Server-Sent Events (SSE) para streaming de respostas em tempo real
- **Rastreamento de Tokens** — Contagens precisas de tokens prompt/completion com finish reasons

### Motor de Ingestão
- **Suporte Multi-Formato** — `.h`, `.cpp`, `.udn`, `.gd`, `.cs`, `.rst`, `.tscn`, `.tres`, `.xml` e exports JSON de Blueprint
- **Documentação Gerada por LLM** — Headers e implementações de código são transformados em guias técnicos orientados a gameplay
- **Re-Ingestão Idempotente** — Verificação de hash de conteúdo previne reprocessamento de arquivos inalterados
- **Quarentena de Falhas** — Arquivos com erros consecutivos de processamento são automaticamente quarantinados
- **Locking Distribuído** — Previne execuções de ingestão concorrentes de corromperem dados

### Multi-Tenancy
- **Coleções Isoladas** — Cada tenant possui coleções Qdrant isoladas
- **Configuração Hierárquica** — Settings cascateiam de coleção → tenant → global com fallback
- **Gerenciamento de API Keys** — API keys seguras com hash bcrypt e papéis admin/read-only
- **Rate Limiting** — Rate limits application-enforced (RPM/TPM) por tenant

### Interface Web
- **Stack Moderna** — React 19, TypeScript 5.8, Vite 6, Tailwind CSS v4
- **Streaming em Tempo Real** — Respostas streaming via SSE
- **Gerenciamento de Sessões** — Histórico de conversação persistente com rastreamento de citações
- **Tema Dark** — Otimizado para longas sessões de código
- **Design Responsivo** — Funciona em desktop e dispositivos móveis

---

## Visão Geral da Arquitetura

O sistema segue uma arquitetura de microserviços com separação clara entre frontend, API backend e serviços de infraestrutura. Todas as interações com LLMs são roteadas através de um único LiteLLM Proxy, abstraindo provedores (Ollama local ou serviços cloud como Gemini).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CAMADA FRONTEND                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  React 19 + Vite + TypeScript + Tailwind CSS (Porta 3001)           │   │
│  │  • ChatWorkspace com streaming    • Gerenciamento de sessões        │   │
│  │  • Troca de Tenant/Coleção        • Dashboard admin                 │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CAMADA API                                      │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI (Porta 8000)                                               │   │
│  │  • /v1/chat              • /v1/sessions                             │   │
│  │  • /v1/ingest/*          • /v1/admin/*                              │   │
│  │  • /v1/health            • /v1/chat/models                          │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
┌──────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Qdrant          │    │  Redis               │    │  PostgreSQL          │
│  Banco Vetorial  │    │  • Cache de sessão   │    │  • Sessões de chat   │
│  (Porta 6333)    │    │  • Locks de ingestão │    │  • Mensagens         │
│  • Vetores densos│    │  • Manifesto checkpoint│   │  • Chunks recuperados│
│  • Vetores sparse│    │  • Cache config tenant│   │  • Dados multi-tenant│
└──────────────────┘    └──────────────────────┘    └──────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ORQUESTRAÇÃO LLM                                    │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  LiteLLM Proxy (Porta 4000 - Externo)                               │   │
│  │  • API unificada para chat, embeddings, decomposições               │   │
│  │  • Roteia para Ollama (local) ou Gemini/OpenAI (cloud)              │   │
│  │  • Autenticação via master key                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Responsabilidades dos Componentes

| Componente | Função |
|------------|--------|
| **FastAPI** | Camada API async expondo endpoints REST. Executa ingestão como BackgroundTasks, usa `asyncio.to_thread()` para I/O de disco. Gerencia resolução de tenant, autenticação e validação de requests. |
| **React Frontend** | Single-page application com gerenciamento de estado Zustand, cliente HTTP Axios, e streaming SSE em tempo real. Inclui views para chat, gerenciamento de ingestão, admin de tenant/coleção e health do sistema. |
| **LiteLLM Proxy** | Ponto de entrada único para todas interações de modelo—chat completions, embeddings, decomposição de query e geração de documentação. Abstrai diferenças de provedores e centraliza gerenciamento de API keys. |
| **Qdrant** | Banco de dados vetorial armazenando chunks de documentação com payloads enriquecidos. Suporta vetores densos (1024-dim padrão), vetores sparse (BM25-like) e busca filtrada por payload. |
| **Redis** | Multiuso: cache de sessão (TTL), manifesto de checkpoint de ingestão (hashes SHA-256), tracker de falhas, locks distribuídos e cache L2 de configuração de tenant. |
| **PostgreSQL** | Persistência durável para dados multi-tenant: tenants, coleções, sessões de chat, mensagens, chunks recuperados e tabelas de configuração. Fonte primária de verdade para histórico de conversação. |
| **FlashRank** | Reranker cross-encoder baseado em ONNX (apenas CPU). Pontua chunks recuperados contra a intenção completa da query para ranking de relevância semântica. |

---

## Stack de Tecnologias

### Backend
- **Python 3.12** — Linguagem principal
- **FastAPI 0.115.5** — Framework web async
- **SQLAlchemy 2.0.36** — ORM async com driver asyncpg
- **Pydantic 2.10.3** — Validação de dados e gerenciamento de settings
- **Alembic 1.14.0** — Migrações de banco de dados
- **Uvicorn 0.32.1** — Servidor ASGI com uvloop
- **Qdrant Client 1.12.1** — Cliente do banco vetorial
- **Redis 5.2.1** — Cache e estado distribuído
- **FlashRank 0.2.9** — Reranking cross-encoder
- **HTTPX 0.28.1** — Cliente HTTP async
- **Tenacity 9.0.0** — Lógica de retry

### Frontend
- **React 19.0.1** — Framework UI
- **TypeScript 5.8** — Desenvolvimento type-safe
- **Vite 6.2.3** — Build tool com HMR
- **Tailwind CSS 4.1.14** — Styling utility-first
- **Zustand 5.0.14** — Gerenciamento de estado
- **Axios 1.19.0** — Cliente HTTP
- **Lucide React 0.546.0** — Biblioteca de ícones
- **Motion 12.23.24** — Biblioteca de animações

### Infraestrutura
- **PostgreSQL 16 Alpine** — Banco de dados primário
- **Redis 7 Alpine** — Cache e estado distribuído
- **Qdrant 1.18.0** — Banco de dados vetorial
- **Docker & Docker Compose** — Orquestração de containers
- **Nginx** — Serving de arquivos estáticos do frontend e reverse proxy

---

## Componentes Principais

### 1. Pipeline de Advanced RAG

Localização: `app/services/ai/rag_service.py`

O pipeline RAG opera em três fases para maximizar tanto o **recall** (encontrar toda documentação relevante) quanto a **precisão** (filtrar ruído):

#### Fase 1 — Decomposição e Tradução de Query

O corpus vetorial está indexado em **inglês técnico**. Queries de usuários em qualquer idioma são processadas por uma LLM rápida para:

1. **Traduzir** a query para inglês (`translated_query`) — esta é a âncora de busca
2. **Decompor** em até 3 sub-queries focadas para retrieval abrangente

Exemplo: "Como implemento pulo em C++ e configuro a animação de aterrissagem?" decomõe em:
- Query de input binding (C++)
- Query de Animation Blueprint (separada)

O parser de resposta usa estratégias de fallback em camadas (JSON → blocos markdown → JSON embutido → strings entre aspas → query original) para lidar graciosamente com respostas malformadas de LLM.

#### Fase 2 — Busca Híbrida e Reranking

1. Cada query (tradução + sub-queries) é embedada e buscada independentemente
2. Resultados são deduplicados por `(source_file, chunk_id)`
3. **Busca Híbrida** — Vetores densos combinados com vetores sparse/BM25-like via Reciprocal Rank Fusion
4. **Reranking Cross-Encoder** — FlashRank pontua cada chunk contra a query traduzida completa (não sub-queries)
5. Piso de qualidade opcional (`rag_rerank_min_score`) descarta chunks com score baixo, preservando top-1
6. Retrieval pode ser escopado para um `project_source` específico via filtros de payload Qdrant

#### Fase 3 — Geração com Contexto Aumentado

Cada chunk é formatado em um prompt de dupla camada:
- **Análise Semântica** — Resumo conceitual gerado por LLM (o que o código *faz* e *por quê*)
- **Referência Direta** — Fonte original não modificada (`raw_content`) — a fonte soberana de verdade para sintaxe exata

Histórico de conversação é carregado do Redis, a mensagem aumentada é enviada via LiteLLM, e o turno é persistido. Cada fase tem tratamento de erro isolado com fallback para RAG naive.

### 2. Motor de Ingestão Code-to-Doc

Localização: `app/services/ai/ingestion_service.py`

Disparado via `POST /v1/ingest/process-folder` e roda como BackgroundTask do FastAPI. Todo I/O de disco é despachado para threads de trabalho via `asyncio.to_thread()` para evitar bloquear requests de chat concorrentes.

**Princípio Central**: Código bruto nunca é embedado diretamente. Cada canal transforma fonte em Markdown semanticamente denso antes de chunking e embedding.

| Canal | Arquivos Fonte | Estratégia de Processamento |
|-------|----------------|------------------------------|
| **A** | `.h` headers C++ | Limpa boilerplate, filtra por relevância de gameplay, LLM gera documentação orientada a gameplay |
| **B** | `.udn` docs Epic | Sanitização regex pura (sem chamada LLM) — já é prosa escrita por humanos |
| **C** | `.cpp` implementações (sample mode) | Divisão em nível de função, agrupamento, LLM documenta lógica de implementação |
| **D** | `.bp.json` exports Blueprint (sample mode) | Consome JSON estruturado, documenta comportamento e fluxos de evento |
| **E** | `.gd`, `.cs`, `.tscn`, `.tres`, `.xml` | Processamento domain-specific para Godot e outros motores |

Todos os canais convergem para: chunking estrutural de Markdown → enriquecimento de metadados → embedding em lote → upsert idempotente no Qdrant.

**Features de Resiliência**:
- **Manifesto de Checkpoint** — Hash Redis mapeia paths de arquivos para hashes SHA-256 de conteúdo; arquivos inalterados são pulados
- **Quarentena de Falhas** — Arquivos com `max_consecutive_failures` erros são quarantinados
- **Locking Distribuído** — Apenas uma ingestão por coleção por vez, com renovação de heartbeat
- **Limpeza de Órfãos** — Chunks antigos deletados só após novos chunks confirmados como commitados

### 3. Sistema Multi-Tenant

Localização: `app/services/tenant_config_service.py`, `app/db/models/`

Suporta coleções isoladas por tenant com configuração hierárquica:

```
Config de Coleção → Default do Tenant → Settings Globais
```

**Schema do Banco**:
- `tenants` — Metadados do tenant, rate limits, status ativo
- `collections` — Config de coleção Qdrant por tenant (domínio, filtros de projeto)
- `api_keys` — API keys seguras com hash bcrypt e papéis admin/read-only
- `collection_configs` — Tabela de junção ligando coleções a configs de embedding/rag/ingestão

**Resolução de Tenant**:
- Cache L1 (nível de processo, TTL 60s)
- Cache L2 (Redis, TTL 300s)
- Fallback para settings globais em falha de resolução (nunca quebra)

### 4. Arquitetura Multi-Domínio

Localização: `app/core/domains/`

Perfis de domínio encapsulam conhecimento específico de engine:
- Extensões de arquivo e tipos MIME
- Estratégias e prompts de ingestão
- Padrões de tokenização (split de CamelCase/snake_case para vetores sparse)
- Lógica de sanitização
- Detecção de versão

**Domínios Disponíveis**:
- `godot_4` — Godot Engine 4.x (GDScript, arquivos de cena, C#)
- `ue5` — Unreal Engine 5 (headers C++, Blueprints, `.udn`)

**Adicionando um Novo Domínio**:
1. Crie perfil de domínio em `app/core/domains/<engine>.py`
2. Registre em `app/core/domains/registry.py`
3. Adicione estratégias de ingestão em `app/services/ai/ingestion/` se necessário

### 5. Interface Web

Localização: `frontend/src/`

SPA moderna construída com React 19, TypeScript e Tailwind CSS v4.

**Views Principais**:
- `ChatWorkspace.tsx` — Interface de chat principal com suporte a streaming
- `FolderIngestionView.tsx` — Submissão de jobs de ingestão
- `IngestionJobsView.tsx` — Monitoramento de status de jobs
- `TenantManagerView.tsx` — Gerenciamento multi-tenant
- `CollectionManagerView.tsx` — Gerenciamento de coleções Qdrant
- `AdminDashboardView.tsx` — Painel admin
- `DevHubView.tsx` — Dashboard de health do sistema

**Gerenciamento de Estado**:
- Store Zustand com persistência localStorage
- Gerencia: estado de auth, tenant/coleção/domínio ativos, health do sistema

---

## Referência da API

### URL Base: `/v1`

#### Chat

```http
POST /v1/chat
Content-Type: application/json

{
  "message": "Como implemento um double jump em C++?",
  "session_id": "uuid-opcional-da-sessao",
  "tenant_id": "uuid-opcional-do-tenant",
  "collection_id": "uuid-opcional-da-colecao",
  "model": "override-opcional-de-modelo",
  "top_k": 5,
  "project_source_filter": "filtro-opcional-de-projeto",
  "stream": true
}
```

**Resposta**: Streaming SSE ou JSON com chunks recuperados, contagens de tokens e citações.

#### Health

```http
GET /v1/health
```

Retorna status de health do Qdrant, Redis e LiteLLM com métricas de latência.

#### Ingestão

```http
POST /v1/ingest/process-folder
Content-Type: application/json

{
  "folder_path": "/data/Engine/Source",
  "content_type": "source_code",
  "project_source": "UE5_Engine",
  "sample_mode": false,
  "collection_id": "uuid-opcional-da-colecao"
}
```

**Resposta**: `202 Accepted` — roda como tarefa em background.

#### Sessões

```http
GET /v1/sessions?page=1&page_size=20
GET /v1/sessions/{session_id}
DELETE /v1/sessions/{session_id}
```

#### Admin

```http
GET /v1/admin/tenants
POST /v1/admin/tenants
GET /v1/admin/tenants/{id}/collections
POST /v1/admin/collections
```

#### Modelos

```http
GET /v1/chat/models
```

Lista modelos disponíveis no proxy LiteLLM.

---

## Schema do Banco de Dados

### Tabelas Principais do PostgreSQL

| Tabela | Propósito |
|--------|-----------|
| `tenants` | Isolamento multi-tenant (slug, nome, rate limits, is_active) |
| `collections` | Config de coleção Qdrant por tenant (domain_id, filtros de projeto) |
| `chat_sessions` | Sessões de conversação (title, default_model, project_filter) |
| `chat_messages` | Mensagens individuais (role, content, tokens, sequence_number) |
| `retrieved_chunks` | Citações (qdrant_chunk_id, score, title, source_file) |
| `api_keys` | Armazenamento seguro de API keys (key_hash bcrypt, is_admin, expires_at) |
| `embedding_configs` | Settings de modelo de embedding |
| `rag_configs` | Settings do pipeline RAG (decomposição, retrieval, reranking) |
| `ingestion_configs` | Settings do pipeline de ingestão |
| `litellm_configs` | Settings do proxy LiteLLM |

### Estrutura de Coleção Qdrant

- **Vetores densos**: 1024 dimensões (configurável)
- **Vetores sparse**: Vetores de palavras-chave BM25-like
- **Campos de payload**: `summary_markdown`, `raw_content`, `title`, `source_file`, `doc_type`, `project_source`, `ue_version`/`godot_version`, `keywords_exact`

---

## Início Rápido

### Pré-requisitos

- Docker e Docker Compose
- Git
- (Opcional) Ollama para modelos locais
- (Opcional) Chave API do Gemini/OpenAI para modelos cloud

### 1. Clonar e Configurar

```bash
git clone <repository-url>
cd any-assistant
cp .env.example .env
```

Edite o `.env`:

```bash
# Obrigatório
AGENT_REDIS_PASSWORD=sua-senha-segura
AGENT_POSTGRES_PASSWORD=sua-senha-segura
LITELLM_MASTER_KEY=sua-master-key

# Opcional (para modelos cloud)
GEMINI_API_KEY=sua-api-key-gemini
```

### 2. Iniciar Serviços

```bash
# Usando Makefile (recomendado)
make up

# Ou usando Docker Compose diretamente
docker compose up -d --build
```

Serviços iniciados:
- `frontend` — Interface web React (porta 3001)
- `ue5-agent` — Backend FastAPI (porta 8000)
- `qdrant` — Banco de dados vetorial (porta 6333)
- `agent-redis` — Cache de sessão e locks de ingestão
- `agent-postgres` — Armazenamento persistente de mensagens

### 3. Verificar Health

```bash
curl http://localhost:8000/v1/health
```

Ou abra http://localhost:3001 no navegador.

### 4. Ingerir Documentação

```bash
# Source da Engine UE5
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
    "folder_path": "/data/ue5_engine_root/Engine/Source",
    "content_type": "source_code",
    "project_source": "UE5_Engine",
    "sample_mode": false
  }'

# Source do Godot 4
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
    "folder_path": "/data/godot_source",
    "content_type": "source_code",
    "project_source": "Godot_4",
    "sample_mode": false
  }'
```

### 5. Conversar

Via Web UI:
- Abra http://localhost:3001
- Inicie uma nova conversa

Via API:

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Como implemento um double jump em C++ e conecto isso a um Animation Blueprint?",
    "session_id": "test-session-001"
  }'
```

---

## Configuração

A configuração é centralizada em `app/core/config.py` via Pydantic Settings, carregada de arquivo `.env`.

### Aplicação

```env
APP_ENV=production               # production | development | test
LOG_LEVEL=INFO                   # DEBUG | INFO | WARNING | ERROR
ACTIVE_DOMAIN=godot_4            # godot_4 | ue5
```

### Bancos de Dados

```env
# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=ue5_docs

# Redis
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=                  # obrigatório
REDIS_DB=0
REDIS_SESSION_TTL=3600

# PostgreSQL
POSTGRES_HOST=agent-postgres
POSTGRES_PORT=5432
POSTGRES_USER=ue5rag
POSTGRES_PASSWORD=               # obrigatório
POSTGRES_DB=ue5_rag_assistant
```

### Configuração LLM

```env
# LiteLLM Proxy (ponto de entrada único)
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=              # obrigatório
LITELLM_MAX_RETRIES=4

# Embeddings
EMBEDDING_MODEL=ollama-embed     # deve estar no config.yml do LiteLLM
EMBEDDING_DIM=1024               # 384 | 768 | 1024 | 1536 | 3072
```

### Pipeline RAG

```env
# Geral
RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.6

# Decomposição de Query
RAG_DECOMPOSITION_ENABLED=true
RAG_DECOMPOSITION_MODEL=gemini/gemini-2.0-flash-lite
RAG_DECOMPOSITION_TIMEOUT=12.0

# Busca Híbrida
RAG_HYBRID_SEARCH_ENABLED=true

# Reranker
RAG_RERANKER_ENABLED=true
RAG_RERANKER_MODEL=ms-marco-MiniLM-L-12-v2
RAG_RERANKER_CACHE_DIR=/tmp/flashrank_cache
RAG_RERANK_MIN_SCORE=0.0
```

### Ingestão

```env
INGESTION_DOCUMENTER_MODEL=gemini/gemini-2.0-flash-lite
INGESTION_BATCH_SIZE=32
INGESTION_EMBEDDING_CONCURRENCY=8
INGESTION_MAX_CHUNK_CHARS=3000
INGESTION_DEFAULT_UE_VERSION=5.8
INGESTION_LOCK_TIMEOUT_SECONDS=3600
INGESTION_MAX_HEADER_CHARS=12000
INGESTION_MAX_CONSECUTIVE_FAILURES=3
```

---

## Desenvolvimento

### Comandos do Makefile

```bash
make help              # Mostrar todos comandos disponíveis
make dev               # Iniciar ambiente de desenvolvimento com hot-reload
make up                # Iniciar ambiente de produção
make down              # Parar todos serviços
make rebuild           # Force rebuild de todos containers (sem cache)
make logs              # Mostrar logs de todos serviços
make logs-backend      # Mostrar apenas logs do backend
make health            # Verificar health dos serviços
make db-shell          # Abrir shell PostgreSQL
make db-backup         # Criar backup do banco
make redis-shell       # Abrir CLI Redis
make test              # Executar todos testes
make test-backend      # Executar testes do backend com coverage
make lint              # Executar linters (ruff + TypeScript)
make format            # Formatar código (ruff format)
make ingest-ue5        # Ingerir documentação da UE5 Engine
make ingest-status     # Verificar status de job de ingestão
make shell-backend     # Abrir shell do container backend
make env-setup         # Copiar .env.example para .env
make deps-install      # Instalar dependências localmente
```

### Desenvolvimento Frontend

```bash
cd frontend
npm run dev      # Servidor de desenvolvimento na porta 3000
npm run build    # Build de produção
npm run lint     # Type checking TypeScript
```

### Executando Testes

```bash
# Todos os testes
make test

# Testes do backend com coverage
make test-backend

# Testes do frontend
make test-frontend
```

### Migrações de Banco de Dados

```bash
# Gerar migração
alembic revision --autogenerate -m "descrição"

# Aplicar migrações
alembic upgrade head

# Rollback
alembic downgrade -1
```

---

## Estrutura do Projeto

```
any-assistant/
├── app/                          # Backend FastAPI
│   ├── api/v1/                   # Rotas da API
│   │   └── endpoints/            # Chat, health, ingestão, sessões, admin
│   ├── core/                     # Config, logging, auth
│   │   └── domains/              # Perfis de domínio (Godot 4, UE5)
│   ├── db/                       # Conexões de banco
│   ├── schemas/                  # Modelos Pydantic
│   └── services/                 # Lógica de negócio
│       ├── ai/                   # RAG, embeddings, ingestão
│       │   └── ingestion/        # Estratégias de ingestão domain-specific
│       └── db/                   # Serviços de banco
├── frontend/                     # SPA React
│   └── src/
│       ├── components/           # Componentes UI
│       ├── services/             # Clientes API
│       ├── store/                # Gerenciamento de estado Zustand
│       ├── types/                # Tipos TypeScript
│       └── views/                # Componentes de página
├── config/                       # Configurações de serviços (Qdrant, Redis)
├── db/                           # Scripts de inicialização do banco
│   └── init/                     # Scripts SQL de migração
├── scripts/                      # Scripts utilitários
├── docker-compose.yml            # Orquestração de serviços
├── Dockerfile                    # Container do backend
├── Makefile                      # Comandos de desenvolvimento
└── requirements.txt              # Dependências Python
```

---

## Roadmap

### v0.3.0 (Em Progresso)

- [x] Streaming de respostas de chat via SSE
- [ ] Highlighting de citações no frontend
- [ ] Suporte WebSocket para updates de ingestão em tempo real
- [ ] Mensagens de erro melhoradas e fluxos de recuperação

### v0.4.0 (Planejado)

- [ ] Busca multi-coleção
- [ ] UI de filtragem avançada
- [ ] Exportar conversas (Markdown, JSON)
- [ ] UI de gerenciamento de API keys
- [ ] Compartilhamento de conversas

### v0.5.0 (Planejado)

- [ ] Embeddings customizados fine-tuned
- [ ] Dashboard de performance
- [ ] Analytics e insights de queries
- [ ] API de ingestão em lote
- [ ] Perfis de domínio adicionais (Unity, Godot 3)

---

## Contribuindo

Contribuições são bem-vindas! Por favor siga estas diretrizes:

1. Faça fork do repositório
2. Crie uma branch de feature (`git checkout -b feature/feature-incrivel`)
3. Faça suas mudanças com testes
4. Execute a suíte de testes (`make test`)
5. Commit com conventional commits (`git commit -m 'feat: adiciona feature incrível'`)
6. Push para a branch (`git push origin feature/feature-incrivel`)
7. Abra um Pull Request

### Estilo de Código

- Backend: Siga PEP 8, use `ruff format`
- Frontend: Siga boas práticas TypeScript, use Prettier
- Escreva testes para nova funcionalidade
- Atualize documentação para mudanças de API

---

## Licença

Este projeto está licenciado sob a Licença MIT - veja o arquivo [LICENSE](LICENSE) para detalhes.

---

## Agradecimentos

- [Epic Games](https://www.epicgames.com/) pelo Unreal Engine 5 e o formato de documentação `.udn`
- [Godot Engine](https://godotengine.org/) pelo motor de jogos open-source
- [Qdrant](https://qdrant.tech/) pelo excelente banco de dados vetorial
- [LiteLLM](https://github.com/BerriAI/litellm) por simplificar a abstração de provedores LLM
- [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank) pelo reranking eficiente
