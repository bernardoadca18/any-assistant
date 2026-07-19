English | [Português (BR)](./README.pt-BR.md)
 
---
 
# UE5 AI Assistant

![Version](https://img.shields.io/badge/version-0.2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Status](https://img.shields.io/badge/status-active--development-yellow)

> An AI-powered technical assistant for **Unreal Engine 5**, built on an Advanced Retrieval-Augmented Generation (RAG) pipeline. It answers gameplay and engine-architecture questions by grounding LLM responses in a semantically curated knowledge base derived directly from UE5 C++ source, official Epic documentation, and exported Blueprint logic.

Unlike a naive "embed the raw source code" RAG system, this project's core design principle is that **raw C++ and Blueprint graphs are not semantically dense enough for reliable retrieval**. A dedicated ingestion engine transforms code and visual scripting into LLM-generated, human-readable technical documentation *before* it ever reaches the vector store — while preserving the original raw content as a secondary, authoritative reference layer for exact syntax.

---

## Table of Contents

- [Architecture & Tech Stack](#architecture--tech-stack)
- [Core Features](#core-features)
  - [Advanced RAG Pipeline](#1-advanced-rag-pipeline)
  - [Code-to-Doc Ingestion Engine](#2-code-to-doc-ingestion-engine-multi-channel-etl)
  - [Idempotency & Resilience](#3-idempotency--resilience)
- [API Endpoints](#api-endpoints)
- [Environment Configuration](#environment-configuration)
- [Usage](#usage)

---

## Architecture & Tech Stack

The system is composed of a stateless FastAPI service surrounded by specialized infrastructure. **No component ever talks to an LLM provider directly** — every model call (chat, embeddings, query decomposition, documentation generation) is routed through a single **LiteLLM Proxy**, which abstracts the underlying provider (local Ollama models or cloud models like Gemini).

| Component | Role |
|---|---|
| **FastAPI** | Async API layer exposing `/v1/chat`, `/v1/ingest/process-folder`, and `/v1/health`. Runs ingestion as `BackgroundTasks` on the same event loop, using `asyncio.to_thread()` to offload blocking disk I/O. |
| **LiteLLM Proxy** | Single entry point for *all* model traffic — chat completions, embeddings, query decomposition, and ingestion-time documentation generation. Routes internally to Ollama or Gemini depending on model alias. Authenticated via a master key. |
| **Qdrant** | Vector database storing documentation chunks with enriched payloads (`project_source`, `doc_type`, `ue_version`, `raw_content`). Supports payload-filtered search (e.g., restricting retrieval to a specific project). |
| **Redis** | Multi-purpose backing store: conversation session history (with TTL), the ingestion **checkpoint manifest** (SHA-256 content hashes per file), a **failure/quarantine tracker**, and **distributed locks** that serialize ingestion runs per collection. |
| **FlashRank** | Lightweight, CPU-only Cross-Encoder reranker (ONNX runtime, no GPU required) used to reorder retrieved chunks by true semantic relevance before they reach the final prompt. |
| **Gemini (via LiteLLM)** | Used for two lightweight, cheap-and-fast tasks: query decomposition/translation (`gemini-3.1-flash-lite`) and ingestion-time documentation generation (`gemini-2.0-flash-lite`). |
| **Ollama** | Local model backend (e.g., `qwen2.5-coder`), reached exclusively through the LiteLLM proxy — used for the main chat/completion model and for embeddings. |

---

## Core Features

### 1. Advanced RAG Pipeline

The pipeline (`app/services/ai/rag_service.py`) runs in three phases designed to maximize both **recall** (finding all relevant documentation for multi-topic questions) and **precision** (filtering out noise before it reaches the final prompt):

**Phase 1 — Query Decomposition & Translation**
The vector corpus is indexed entirely in **technical English** (both the LLM-generated documentation and Epic's official `.udn` docs). Because a Portuguese query would suffer a cross-lingual embedding mismatch, `decompose_query()` calls a fast LLM (`gemini-3.1-flash-lite`, with a distinct fallback model on failure) to:
1. Produce a literal English **translation** of the user's question (`translated_query`) — this is always used as the search anchor, never the original-language query.
2. Decompose the question into up to **3 additional technical sub-queries**, each focused on a single UE5 concept (e.g., splitting "how do I code jumping in C++ and set up the landing animation?" into a C++ input-binding query and a separate Animation Blueprint query).

The response parser is deliberately defensive, with layered fallback strategies (direct JSON → JSON in markdown fences → embedded JSON → quoted-string extraction → original-question fallback) to guarantee the pipeline never crashes on a malformed LLM response.

**Phase 2 — Expanded Retrieval + Cross-Encoder Reranking**
1. Every query (translation + sub-queries) is embedded and searched against Qdrant independently, each pulling up to `rag_retrieval_limit_per_query` candidates (default 15) — favoring recall over precision at this stage.
2. Results across all queries are **deduplicated** in-memory by `(source_file, chunk_id)`.
3. The deduplicated pool is reranked by `rerank_with_flashrank()`, a Cross-Encoder (`ms-marco-MiniLM-L-12-v2` by default) that scores each chunk directly against the translated English query — never the sub-queries individually — to judge relevance against the full intent of the question. Reranking runs on CPU via ONNX and is dispatched to a thread to avoid blocking the event loop.
4. An optional quality floor (`rag_rerank_min_score`) can discard low-scoring chunks post-rerank, while always preserving at least the top-1 result so the prompt is never empty.
5. Retrieval can optionally be scoped to a single `project_source` (e.g., only Lyra, or only base Engine code) via a Qdrant payload filter.

**Phase 3 — Generation**
The final context is formatted into a dual-layer prompt per chunk:
- **Semantic Analysis** — the LLM-generated conceptual summary (what the code *does* and *why*).
- **Direct Reference** — the original, unmodified raw source (`raw_content`: the cleaned C++ header, sanitized `.udn`, or formatted Blueprint JSON), which the system prompt explicitly instructs the model to treat as the **sovereign source of truth** for exact signatures, types, and property names.

Conversation history is loaded from Redis, the augmented message is sent to the chat model via `call_litellm()`, and the turn is persisted back to Redis. Every phase has isolated error handling with fallback to a simpler Naive RAG path (`retrieve_chunks()`), so a failure in decomposition or reranking never surfaces as an HTTP 500.

### 2. Code-to-Doc Ingestion Engine (Multi-Channel ETL)

The ingestion engine (`app/services/ai/ingestion_service.py`) is triggered via `POST /v1/ingest/process-folder` and runs entirely as a **FastAPI `BackgroundTask`**, inside the same container and event loop as the chat API. Because a recursive scan over an engine-scale source tree can involve tens of thousands of files, all disk-bound work (`os.walk`, file reads) is dispatched to worker threads via `asyncio.to_thread()` so it never blocks concurrent `/v1/chat` requests.

Its core architectural principle: **raw code is never embedded directly**. Each channel transforms its source material into semantically dense Markdown before it is chunked and embedded — this is what allows a small local model to answer accurately instead of getting lost in raw syntax.

| Channel | Source | Processing |
|---|---|---|
| **A — `.h` headers** | UE5 C++ headers | Cleans includes/boilerplate, filters for gameplay relevance (strict keyword allowlist for Engine code; disabled entirely in `sample_mode` or for strategic modern-gameplay modules like Mover/Network Prediction), then sends the cleaned header to an LLM acting as a "Senior Technical Documenter" to produce a gameplay-oriented Markdown guide. |
| **B — `.udn`** | Epic's official documentation format | Pure regex-based sanitization (strips proprietary macros/metadata) — **no LLM call**. Lower cost, since the source is already human-written prose. |
| **C — `.cpp` (Sample Mode only)** | Implementation logic in sample projects (Lyra, City Sample, Vehicle Sample) | Splits the file into function-level blocks via signature detection + brace-balancing, groups small functions together to avoid wasteful LLM calls, and documents the grouped implementation logic — capturing *how* a mechanic works, not just its public API. |
| **D — `.bp.json` (Sample Mode only)** | Blueprint graphs, exported via a companion Unreal Editor Python script (`BP_GraphExporter.py`) | Consumes an already-structured JSON representation (`ParentClass`, `Variables`, linearized `ExecutionFlow` with branch/loop resolution) — no regex parsing of raw text — and documents the Blueprint's gameplay behavior, key variables, main event flows, and integration points with other systems. |

All four channels converge on the same downstream pipeline: **structural Markdown chunking** (split by header hierarchy, with a hard character ceiling as a safety net), **metadata enrichment** (`project_source`, `doc_type`, `ue_version`, `raw_content`), **batched embedding generation**, and **idempotent upsert** into Qdrant.

### 3. Idempotency & Resilience

Redis underpins every safety mechanism in the ingestion pipeline:

- **Checkpoint Manifest** — a Redis hash (`manifest:{collection_name}`) maps each source file's relative path to a SHA-256 content hash. On re-ingestion, unchanged files are skipped entirely (no re-reading, no LLM call, no re-embedding), and modified files are automatically detected and reprocessed. A file's entry is only committed to the manifest once *every* chunk it produced has been **confirmed persisted** in Qdrant — never based on in-memory generation counts alone — so an interrupted run never leaves a false "success" checkpoint.
- **Failure Tracking & Quarantine** — files that fail processing (read errors, LLM failures, embedding failures) are tracked in a dedicated Redis failures hash. After `ingestion_max_consecutive_failures` consecutive failures, a file is quarantined and skipped on future runs until manually cleared, preventing wasted LLM/embedding calls on a permanently broken file.
- **Distributed Locking** — a Redis-based distributed lock (`ingestion_lock_key`) ensures only one ingestion run per collection can execute at a time, preventing two concurrent runs from corrupting the checkpoint manifest. The lock is periodically renewed (heartbeat) for the duration of long-running ingestions so it never expires mid-run due to TTL, while still self-expiring safely if the process dies unexpectedly.
- **Orphan Cleanup** — when a previously-indexed file changes, its old chunks are deleted from Qdrant only *after* the new chunks are confirmed committed, avoiding any window where valid data is missing from the index.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/v1/chat` | Executes the full Advanced RAG pipeline: embeds the question, retrieves and reranks documentation chunks, builds the augmented prompt, and generates a response via the LiteLLM proxy. Conversation history is maintained per `session_id` in Redis. Returns `503` on known operational failures (e.g., LiteLLM/Ollama unavailable) and `500` on unexpected errors. |
| `POST` | `/v1/ingest/process-folder` | Triggers the Code-to-Doc ETL engine as a background task over a folder path **inside the container**. Accepts `content_type` (`source_code` or `documentation`), `project_source` (provenance label, e.g. `UE5_Engine`, `Lyra`), `sample_mode` (enables Channels C & D), and an optional `ue_version` override. Returns immediately with `202 Accepted` — the task runs asynchronously and progress is tracked via application logs and the Redis manifest. |
| `GET` | `/v1/health` | Active health check against all dependencies (Qdrant, Redis, LiteLLM), run concurrently. Returns `200` if all dependencies are healthy or degraded, `503` if any dependency is in an error state — including per-dependency latency and status detail. |

---

## Environment Configuration

Configuration is centralized in `app/core/config.py` via Pydantic Settings, loaded from a `.env` file. Key variables:

```env
# --- Application ---
APP_ENV=production               # production | development | test
LOG_LEVEL=INFO

# --- Qdrant ---
QDRANT_HOST=qdrant
QDRANT_PORT=6333
QDRANT_COLLECTION=ue5_docs

# --- Redis (sessions, manifest, locks, quarantine) ---
REDIS_HOST=agent-redis
REDIS_PORT=6379
REDIS_PASSWORD=                  # required
REDIS_DB=0
REDIS_SESSION_TTL=3600           # conversation session TTL, in seconds

# --- LiteLLM Proxy (single entry point for all model calls) ---
LITELLM_BASE_URL=http://litellm-proxy:4000
LITELLM_MASTER_KEY=              # required
LITELLM_MAX_RETRIES=4            # retries on 429/500/502/503/504 or timeout

# --- Embeddings ---
EMBEDDING_MODEL=ollama-embed     # must be configured as an embedding model in LiteLLM's config.yml
EMBEDDING_DIM=1024               # one of: 384, 768, 1024, 1536, 3072

# --- RAG (general) ---
RAG_TOP_K=5
RAG_SCORE_THRESHOLD=0.6          # Naive RAG only — superseded by the reranker in Advanced RAG

# --- Advanced RAG — Query Decomposition ---
RAG_DECOMPOSITION_ENABLED=true
RAG_DECOMPOSITION_MODEL=gemini/gemini-3.1-flash-lite
RAG_DECOMPOSITION_TIMEOUT=12.0

# --- Advanced RAG — Retrieval Expansion ---
RAG_RETRIEVAL_LIMIT_PER_QUERY=15

# --- Advanced RAG — FlashRank Reranker ---
RAG_RERANKER_ENABLED=true
RAG_RERANKER_MODEL=ms-marco-MiniLM-L-12-v2   # or ms-marco-MultiBERT-L-12, rank-T5-flan
RAG_RERANKER_CACHE_DIR=/tmp/flashrank_cache
RAG_RERANK_MIN_SCORE=0.0

# --- Ingestion — Code-to-Doc ETL ---
INGESTION_DOCUMENTER_MODEL=gemini/gemini-2.0-flash-lite
INGESTION_BATCH_SIZE=32
INGESTION_EMBEDDING_CONCURRENCY=8
INGESTION_MAX_CHUNK_CHARS=3000
INGESTION_DEFAULT_UE_VERSION=5.8
INGESTION_LOCK_TIMEOUT_SECONDS=3600
INGESTION_MAX_HEADER_CHARS=12000
INGESTION_MAX_CONSECUTIVE_FAILURES=3
```

> **Note:** `ollama_base_url` is intentionally absent — the FastAPI service never calls Ollama directly. All model traffic, including embeddings, is routed through the LiteLLM proxy, which has Ollama pre-configured as one of its backends.

---

## Usage

> The project is designed to run as part of a multi-container stack (FastAPI service + Qdrant + Redis + LiteLLM proxy), coordinated via Docker Compose.

### 1. Clone and configure

```bash
git clone <repository-url>
cd ue5-ai-assistant
cp .env.example .env
# Fill in REDIS_PASSWORD, LITELLM_MASTER_KEY, and any provider API keys
# required by your LiteLLM config.yml (e.g., Gemini API key).
```

### 2. Start the stack

```bash
docker compose up -d --build
```

This should bring up, at minimum: the `ue5-agent` FastAPI service, `qdrant`, `agent-redis`, and the `litellm-proxy` (with Ollama reachable from the proxy's network).

### 3. Verify health

```bash
curl http://localhost:8000/v1/health
```

### 4. Ingest documentation and source code

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/UnrealEngine/Engine/Source",
        "content_type": "source_code",
        "project_source": "UE5_Engine",
        "sample_mode": false
      }'
```

For a gameplay sample project (enables Channels C and D):

```bash
curl -X POST http://localhost:8000/v1/ingest/process-folder \
  -H "Content-Type: application/json" \
  -d '{
        "folder_path": "/data/Lyra",
        "content_type": "source_code",
        "project_source": "Lyra",
        "sample_mode": true
      }'
```

Ingestion runs in the background; monitor progress via the application logs (`INFO` level) and Redis's checkpoint manifest.

### 5. Ask a question

```bash
curl -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
        "message": "How do I implement a double jump in C++ and hook it up to an Animation Blueprint?",
        "session_id": "5c1f2e3a-...-uuid-v4",
        "top_k": 5
      }'
```
