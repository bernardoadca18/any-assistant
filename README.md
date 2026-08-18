English | [Português (BR)](./README.pt-BR.md)

---

# Any Assistant AI

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![React](https://img.shields.io/badge/React-19-61DAFB)
![Status](https://img.shields.io/badge/status-active--development-yellow)

> A multi-tenant, multi-domain AI assistant platform powered by Advanced Retrieval-Augmented Generation (RAG). Currently supports **Unreal Engine 5** and **Godot 4** with extensible architecture for additional game engines and technical domains.

## Overview

Any Assistant AI is a production-ready system designed to answer technical questions about game engine architecture, APIs, and best practices by grounding LLM responses in a semantically curated knowledge base. Unlike naive RAG systems that embed raw code directly, this project transforms source code and documentation into LLM-generated technical documentation before indexing—ensuring semantic density for reliable retrieval while preserving original content as an authoritative reference layer.

### Key Differentiators

- **Code-to-Doc Transformation** — Raw C++, Blueprints, and GDScript are never embedded directly. Instead, LLM-generated documentation is used for semantic retrieval, with original source preserved for exact syntax reference.
- **Advanced RAG Pipeline** — Query decomposition, hybrid search (dense + sparse vectors), cross-encoder reranking, and multi-language support deliver high-precision responses.
- **Multi-Tenant Architecture** — Isolated collections per tenant with hierarchical configuration (collection → tenant → global) and API key authentication.
- **Multi-Domain Support** — Extensible domain profiles enable support for multiple game engines. Currently ships with Godot 4 and Unreal Engine 5 profiles.
- **Production-Ready Infrastructure** — Docker Compose orchestration with health checks, resource limits, and persistent storage.

---

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Technology Stack](#technology-stack)
- [Core Components](#core-components)
  - [Advanced RAG Pipeline](#1-advanced-rag-pipeline)
  - [Code-to-Doc Ingestion Engine](#2-code-to-doc-ingestion-engine)
  - [Multi-Tenancy System](#3-multi-tenancy-system)
  - [Multi-Domain Architecture](#4-multi-domain-architecture)
  - [Web Interface](#5-web-interface)
- [API Reference](#api-reference)
- [Database Schema](#database-schema)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Development](#development)
- [Project Structure](#project-structure)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Features

### RAG Pipeline
- **Query Decomposition** — Complex questions are broken into focused sub-queries for comprehensive retrieval
- **Multi-Language Support** — Queries in any language are translated to English for vector search, with language-aware response generation
- **Hybrid Search** — Combines dense vector similarity with sparse/BM25-like keyword matching via Reciprocal Rank Fusion (RRF)
- **Cross-Encoder Reranking** — FlashRank ONNX-based reranker scores chunks against full query intent (CPU-only, ~10-50ms)
- **Streaming Responses** — Server-Sent Events (SSE) for real-time response streaming
- **Token Tracking** — Accurate prompt/completion token counts with finish reasons

### Ingestion Engine
- **Multi-Format Support** — `.h`, `.cpp`, `.udn`, `.gd`, `.cs`, `.rst`, `.tscn`, `.tres`, `.xml`, and Blueprint JSON exports
- **LLM-Powered Documentation** — Code headers and implementations are transformed into gameplay-oriented technical guides
- **Idempotent Re-Ingestion** — Content hash checking prevents reprocessing unchanged files
- **Failure Quarantine** — Files with consecutive processing errors are automatically quarantined
- **Distributed Locking** — Prevents concurrent ingestion runs from corrupting data

### Multi-Tenancy
- **Isolated Collections** — Each tenant has isolated Qdrant collections
- **Hierarchical Configuration** — Settings cascade from collection → tenant → global with fallback
- **API Key Management** — Secure bcrypt-hashed API keys with admin and read-only roles
- **Rate Limiting** — Application-enforced rate limits (RPM/TPM) per tenant

### Web Interface
- **Modern Stack** — React 19, TypeScript 5.8, Vite 6, Tailwind CSS v4
- **Real-Time Streaming** — SSE-powered streaming responses
- **Session Management** — Persistent conversation history with citation tracking
- **Dark Theme** — Optimized for long coding sessions
- **Responsive Design** — Works on desktop and mobile devices

---

## Architecture Overview

The system follows a microservices architecture with clear separation between frontend, backend API, and infrastructure services. All LLM interactions are routed through a single LiteLLM Proxy, abstracting providers (local Ollama or cloud services like Gemini).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FRONTEND LAYER                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  React 19 + Vite + TypeScript + Tailwind CSS (Port 3001)            │   │
│  │  • ChatWorkspace with streaming    • Session management             │   │
│  │  • Tenant/Collection switching    • Admin dashboard                 │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              API LAYER                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastAPI (Port 8000)                                                │   │
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
│  Vector DB       │    │  • Session cache     │    │  • Chat sessions     │
│  (Port 6333)     │    │  • Ingestion locks   │    │  • Messages          │
│  • Dense vectors │    │  • Checkpoint manifest│   │  • Retrieved chunks  │
│  • Sparse vectors│    │  • Tenant config cache│   │  • Multi-tenant data │
└──────────────────┘    └──────────────────────┘    └──────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          LLM ORCHESTRATION                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  LiteLLM Proxy (Port 4000 - External)                               │   │
│  │  • Unified API for chat, embeddings, decompositions                 │   │
│  │  • Routes to Ollama (local) or Gemini/OpenAI (cloud)                │   │
│  │  • Master key authentication                                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Role |
|-----------|------|
| **FastAPI** | Async API layer exposing REST endpoints. Runs ingestion as BackgroundTasks, uses `asyncio.to_thread()` for disk I/O. Handles tenant resolution, authentication, and request validation. |
| **React Frontend** | Single-page application with Zustand state management, Axios HTTP client, and real-time SSE streaming. Includes views for chat, ingestion management, tenant/collection admin, and system health. |
| **LiteLLM Proxy** | Single entry point for all model interactions—chat completions, embeddings, query decomposition, and documentation generation. Abstracts provider differences and centralizes API key management. |
| **Qdrant** | Vector database storing documentation chunks with enriched payloads. Supports dense vectors (1024-dim default), sparse vectors (BM25-like), and payload-filtered search. |
| **Redis** | Multi-purpose: session cache (TTL), ingestion checkpoint manifest (SHA-256 hashes), failure tracker, distributed locks, and L2 tenant configuration cache. |
| **PostgreSQL** | Durable persistence for multi-tenant data: tenants, collections, chat sessions, messages, retrieved chunks, and configuration tables. Primary source of truth for conversation history. |
| **FlashRank** | ONNX-based cross-encoder reranker (CPU-only). Scores retrieved chunks against full query intent for semantic relevance ranking. |

---

## Technology Stack

### Backend
- **Python 3.12** — Core language
- **FastAPI 0.115.5** — Async web framework
- **SQLAlchemy 2.0.36** — Async ORM with asyncpg driver
- **Pydantic 2.10.3** — Data validation and settings management
- **Alembic 1.14.0** — Database migrations
- **Uvicorn 0.32.1** — ASGI server with uvloop
- **Qdrant Client 1.12.1** — Vector database client
- **Redis 5.2.1** — Cache and distributed state
- **FlashRank 0.2.9** — Cross-encoder reranking
- **HTTPX 0.28.1** — Async HTTP client
- **Tenacity 9.0.0** — Retry logic

### Frontend
- **React 19.0.1** — UI framework
- **TypeScript 5.8** — Type-safe development
- **Vite 6.2.3** — Build tool with HMR
- **Tailwind CSS 4.1.14** — Utility-first styling
- **Zustand 5.0.14** — State management
- **Axios 1.19.0** — HTTP client
- **Lucide React 0.546.0** — Icon library
- **Motion 12.23.24** — Animation library

### Infrastructure
- **PostgreSQL 16 Alpine** — Primary database
- **Redis 7 Alpine** — Cache and distributed state
- **Qdrant 1.18.0** — Vector database
- **Docker & Docker Compose** — Container orchestration
- **Nginx** — Frontend static file serving and reverse proxy

---

## Core Components

### 1. Advanced RAG Pipeline

Location: `app/services/ai/rag_service.py`

The RAG pipeline operates in three phases to maximize both **recall** (finding all relevant documentation) and **precision** (filtering noise):

#### Phase 1 — Query Decomposition & Translation

The vector corpus is indexed in **technical English**. User queries in any language are processed by a fast LLM to:

1. **Translate** the query to English (`translated_query`) — this is the search anchor
2. **Decompose** into up to 3 focused sub-queries for comprehensive retrieval

Example: "How do I implement jumping in C++ and set up the landing animation?" decomposes into:
- Input binding query (C++)
- Animation Blueprint query (separate)

The response parser uses layered fallback strategies (JSON → markdown fences → embedded JSON → quoted strings → original query) to handle malformed LLM responses gracefully.

#### Phase 2 — Hybrid Search & Reranking

1. Each query (translation + sub-queries) is embedded and searched independently
2. Results are deduplicated by `(source_file, chunk_id)`
3. **Hybrid Search** — Dense vectors combined with sparse/BM25-like vectors via Reciprocal Rank Fusion
4. **Cross-Encoder Reranking** — FlashRank scores each chunk against the full translated query (not sub-queries)
5. Optional quality floor (`rag_rerank_min_score`) discards low-scoring chunks while preserving top-1
6. Retrieval can be scoped to a specific `project_source` via Qdrant payload filters

#### Phase 3 — Context-Augmented Generation

Each chunk is formatted into a dual-layer prompt:
- **Semantic Analysis** — LLM-generated conceptual summary (what the code *does* and *why*)
- **Direct Reference** — Original unmodified source (`raw_content`) — the sovereign source of truth for exact syntax

Conversation history is loaded from Redis, the augmented message is sent via LiteLLM, and the turn is persisted. Each phase has isolated error handling with fallback to naive RAG.

### 2. Code-to-Doc Ingestion Engine

Location: `app/services/ai/ingestion_service.py`

Triggered via `POST /v1/ingest/process-folder` and runs as a FastAPI BackgroundTask. All disk I/O is dispatched to worker threads via `asyncio.to_thread()` to avoid blocking concurrent chat requests.

**Core Principle**: Raw code is never embedded directly. Each channel transforms source into semantically dense Markdown before chunking and embedding.

| Channel | Source Files | Processing Strategy |
|---------|--------------|---------------------|
| **A** | `.h` C++ headers | Cleans boilerplate, filters for gameplay relevance, LLM generates gameplay-oriented documentation |
| **B** | `.udn` Epic docs | Pure regex sanitization (no LLM call) — already human-written prose |
| **C** | `.cpp` implementations (sample mode) | Function-level splitting, grouping, LLM documents implementation logic |
| **D** | `.bp.json` Blueprint exports (sample mode) | Consumes structured JSON, documents behavior and event flows |
| **E** | `.gd`, `.cs`, `.tscn`, `.tres`, `.xml` | Domain-specific processing for Godot and other engines |

All channels converge on: structural Markdown chunking → metadata enrichment → batched embedding → idempotent Qdrant upsert.

**Resilience Features**:
- **Checkpoint Manifest** — Redis hash maps file paths to SHA-256 content hashes; unchanged files skipped
- **Failure Quarantine** — Files with `max_consecutive_failures` errors are quarantined
- **Distributed Locking** — Only one ingestion per collection at a time, with heartbeat renewal
- **Orphan Cleanup** — Old chunks deleted only after new chunks confirmed committed

### 3. Multi-Tenancy System

Location: `app/services/tenant_config_service.py`, `app/db/models/`

Supports isolated collections per tenant with hierarchical configuration:

```
Collection Config → Tenant Default → Global Settings
```

**Database Schema**:
- `tenants` — Tenant metadata, rate limits, active status
- `collections` — Qdrant collection config per tenant (domain, project filters)
- `api_keys` — Secure bcrypt-hashed API keys with admin/read-only roles
- `collection_configs` — Junction table linking collections to embedding/rag/ingestion configs

**Tenant Resolution**:
- L1 cache (process-level, 60s TTL)
- L2 cache (Redis, 300s TTL)
- Fallback to global settings on resolution failure (never crashes)

### 4. Multi-Domain Architecture

Location: `app/core/domains/`

Domain profiles encapsulate engine-specific knowledge:
- File extensions and MIME types
- Ingestion strategies and prompts
- Tokenization patterns (CamelCase/snake_case splitting for sparse vectors)
- Sanitization logic
- Version detection

**Available Domains**:
- `godot_4` — Godot Engine 4.x (GDScript, scene files, C#)
- `ue5` — Unreal Engine 5 (C++ headers, Blueprints, `.udn`)

**Adding a New Domain**:
1. Create domain profile in `app/core/domains/<engine>.py`
2. Register in `app/core/domains/registry.py`
3. Add ingestion strategies in `app/services/ai/ingestion/` if needed

### 5. Web Interface

Location: `frontend/src/`

Modern SPA built with React 19, TypeScript, and Tailwind CSS v4.

**Key Views**:
- `ChatWorkspace.tsx` — Main chat interface with streaming support
- `FolderIngestionView.tsx` — Submit ingestion jobs
- `IngestionJobsView.tsx` — Monitor job status
- `TenantManagerView.tsx` — Multi-tenant management
- `CollectionManagerView.tsx` — Qdrant collection management
- `AdminDashboardView.tsx` — Admin panel
- `DevHubView.tsx` — System health dashboard

**State Management**:
- Zustand store with localStorage persistence
- Manages: auth state, active tenant/collection/domain, system health

---

## API Reference

### Base URL: `/v1`

#### Chat

```http
POST /v1/chat
Content-Type: application/json

{
  "message": "How do I implement a double jump in C++?",
  "session_id": "optional-session-uuid",
  "tenant_id": "optional-tenant-uuid",
  "collection_id": "optional-collection-uuid",
  "model": "optional-model-override",
  "top_k": 5,
  "project_source_filter": "optional-project-filter",
  "stream": true
}
```

**Response**: Streaming SSE or JSON with retrieved chunks, token counts, and citations.

#### Health

```http
GET /v1/health
```

Returns health status of Qdrant, Redis, and LiteLLM with latency metrics.

#### Ingestion

```http
POST /v1/ingest/process-folder
Content-Type: application/json

{
  "folder_path": "/data/Engine/Source",
  "content_type": "source_code",
  "project_source": "UE5_Engine",
  "sample_mode": false,
  "collection_id": "optional-collection-uuid"
}
```

**Response**: `202 Accepted` — runs as background task.

#### Sessions

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

#### Models

```http
GET /v1/chat/models
```

Lists available models from LiteLLM proxy.

---

## Database Schema

### PostgreSQL Core Tables

| Table | Purpose |
|-------|---------|
| `tenants` | Multi-tenant isolation (slug, name, rate limits, is_active) |
| `collections` | Qdrant collection config per tenant (domain_id, project filters) |
| `chat_sessions` | Conversation sessions (title, default_model, project_filter) |
| `chat_messages` | Individual messages (role, content, tokens, sequence_number) |
| `retrieved_chunks` | Citations (qdrant_chunk_id, score, title, source_file) |
| `api_keys` | Secure API key storage (key_hash bcrypt, is_admin, expires_at) |
| `embedding_configs` | Embedding model settings |
| `rag_configs` | RAG pipeline settings (decomposition, retrieval, reranking) |
| `ingestion_configs` | Ingestion pipeline settings |
| `litellm_configs` | LiteLLM proxy settings |

### Qdrant Collection Structure

- **Dense vectors**: 1024 dimensions (configurable)
- **Sparse vectors**: BM25-like keyword vectors
- **Payload fields**: `summary_markdown`, `raw_content`, `title`, `source_file`, `doc_type`, `project_source`, `ue_version`/`godot_version`, `keywords_exact`

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Git
- (Optional) Ollama for local models
- (Optional) Gemini/OpenAI API key for cloud models

### 1. Clone and Configure

```bash
git clone <repository-url>
cd any-assistant
cp .env.example .env
```

Edit `.env`:

```bash
# Required
AGENT_REDIS_PASSWORD=your-secure-password
AGENT_POSTGRES_PASSWORD=your-secure-password
LITELLM_MASTER_KEY=your-master-key

# Optional (for cloud models)
GEMINI_API_KEY=your-gemini-api-key
```

### 2. Start Services

```bash
# Using Makefile (recommended)
make up

# Or using Docker Compose directly
docker compose up -d --build
```

Services started:
- `frontend` — React web interface (port 3001)
- `ue5-agent` — FastAPI backend (port 8000)
- `qdrant` — Vector database (port 6333)
- `agent-redis` — Session cache and ingestion locks
- `agent-postgres` — Persistent message storage

### 3. Verify Health

```bash
curl http://localhost:8000/v1/health
```

Or open http://localhost:3001 in your browser.

### 4. Ingest Documentation

```bash
# UE5 Engine source
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
    "folder_path": "/data/ue5_engine_root/Engine/Source",
    "content_type": "source_code",
    "project_source": "UE5_Engine",
    "sample_mode": false
  }'

# Godot 4 source
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
    "folder_path": "/data/godot_source",
    "content_type": "source_code",
    "project_source": "Godot_4",
    "sample_mode": false
  }'
```

### 5. Chat

Via Web UI:
- Open http://localhost:3001
- Start a new conversation

Via API:

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "How do I implement a double jump in C++ and connect it to an Animation Blueprint?",
    "session_id": "test-session-001"
  }'
```

---

## Configuration

Configuration is centralized in `app/core/config.py` via Pydantic Settings, loaded from `.env` file.

### Application

```env
APP_ENV=production               # production | development | test
LOG_LEVEL=INFO                   # DEBUG | INFO | WARNING | ERROR
ACTIVE_DOMAIN=godot_4            # godot_4 | ue5
```

### Databases

```env
# Qdrant
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=ue5_docs

# Redis
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=                  # required
REDIS_DB=0
REDIS_SESSION_TTL=3600

# PostgreSQL
POSTGRES_HOST=agent-postgres
POSTGRES_PORT=5432
POSTGRES_USER=ue5rag
POSTGRES_PASSWORD=               # required
POSTGRES_DB=ue5_rag_assistant
```

### LLM Configuration

```env
# LiteLLM Proxy (single entry point)
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=              # required
LITELLM_MAX_RETRIES=4

# Embeddings
EMBEDDING_MODEL=ollama-embed     # must be in LiteLLM config.yml
EMBEDDING_DIM=1024               # 384 | 768 | 1024 | 1536 | 3072
```

### RAG Pipeline

```env
# General
RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.6

# Query Decomposition
RAG_DECOMPOSITION_ENABLED=true
RAG_DECOMPOSITION_MODEL=gemini/gemini-2.0-flash-lite
RAG_DECOMPOSITION_TIMEOUT=12.0

# Hybrid Search
RAG_HYBRID_SEARCH_ENABLED=true

# Reranker
RAG_RERANKER_ENABLED=true
RAG_RERANKER_MODEL=ms-marco-MiniLM-L-12-v2
RAG_RERANKER_CACHE_DIR=/tmp/flashrank_cache
RAG_RERANK_MIN_SCORE=0.0
```

### Ingestion

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

## Development

### Makefile Commands

```bash
make help              # Show all available commands
make dev               # Start development environment with hot-reload
make up                # Start production environment
make down              # Stop all services
make rebuild           # Force rebuild all containers (no cache)
make logs              # Show logs for all services
make logs-backend      # Show backend logs only
make health            # Check service health
make db-shell          # Open PostgreSQL shell
make db-backup         # Create database backup
make redis-shell       # Open Redis CLI
make test              # Run all tests
make test-backend      # Run backend tests with coverage
make lint              # Run linters (ruff + TypeScript)
make format            # Format code (ruff format)
make ingest-ue5        # Ingest UE5 Engine documentation
make ingest-status     # Check ingestion job status
make shell-backend     # Open backend container shell
make env-setup         # Copy .env.example to .env
make deps-install      # Install dependencies locally
```

### Frontend Development

```bash
cd frontend
npm run dev      # Development server on port 3000
npm run build    # Production build
npm run lint     # TypeScript type checking
```

### Running Tests

```bash
# All tests
make test

# Backend tests with coverage
make test-backend

# Frontend tests
make test-frontend
```

### Database Migrations

```bash
# Generate migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

---

## Project Structure

```
any-assistant/
├── app/                          # FastAPI backend
│   ├── api/v1/                   # API routes
│   │   └── endpoints/            # Chat, health, ingestion, sessions, admin
│   ├── core/                     # Config, logging, auth
│   │   └── domains/              # Domain profiles (Godot 4, UE5)
│   ├── db/                       # Database connections
│   ├── schemas/                  # Pydantic models
│   └── services/                 # Business logic
│       ├── ai/                   # RAG, embeddings, ingestion
│       │   └── ingestion/        # Domain-specific ingestion strategies
│       └── db/                   # Database services
├── frontend/                     # React SPA
│   └── src/
│       ├── components/           # UI components
│       ├── services/             # API clients
│       ├── store/                # Zustand state management
│       ├── types/                # TypeScript types
│       └── views/                # Page components
├── config/                       # Service configurations (Qdrant, Redis)
├── db/                           # Database initialization scripts
│   └── init/                     # SQL migration scripts
├── scripts/                      # Utility scripts
├── docker-compose.yml            # Service orchestration
├── Dockerfile                    # Backend container
├── Makefile                      # Development commands
└── requirements.txt              # Python dependencies
```

---

## Roadmap

### v0.3.0 (In Progress)

- [x] Streaming chat responses via SSE
- [ ] Citation highlighting in frontend
- [ ] WebSocket support for real-time ingestion updates
- [ ] Improved error messages and recovery flows

### v0.4.0 (Planned)

- [ ] Multi-collection search
- [ ] Advanced filtering UI
- [ ] Export conversations (Markdown, JSON)
- [ ] API key management UI
- [ ] Conversation sharing

### v0.5.0 (Planned)

- [ ] Custom fine-tuned embeddings
- [ ] Performance dashboard
- [ ] Query analytics and insights
- [ ] Batch ingestion API
- [ ] Additional domain profiles (Unity, Godot 3)

---

## Contributing

Contributions are welcome! Please follow these guidelines:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes with tests
4. Run the test suite (`make test`)
5. Commit with conventional commits (`git commit -m 'feat: add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

### Code Style

- Backend: Follow PEP 8, use `ruff format`
- Frontend: Follow TypeScript best practices, use Prettier
- Write tests for new functionality
- Update documentation for API changes

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Acknowledgments

- [Epic Games](https://www.epicgames.com/) for Unreal Engine 5 and the `.udn` documentation format
- [Godot Engine](https://godotengine.org/) for the open-source game engine
- [Qdrant](https://qdrant.tech/) for the excellent vector database
- [LiteLLM](https://github.com/BerriAI/litellm) for simplifying LLM provider abstraction
- [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank) for efficient reranking
