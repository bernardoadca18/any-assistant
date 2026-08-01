"""
app/main.py
===========
Ponto de entrada da aplicação FastAPI.

Arquitetura simplificada: todo acesso a modelos (LLM + embeddings)
passa pelo LiteLLM proxy. Não há mais conexão direta ao Ollama.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.postgres import close_postgres, init_postgres
from app.db.qdrant import close_qdrant, init_qdrant
from app.db.redis import close_redis, init_redis
from app.services.ai.embedding_service import close_embedding_client, init_embedding_client
from app.services.ai.ingestion_service import ensure_sparse_vectors_enabled
from app.services.ai.rag_service import (
    close_decomposition_client,
    close_litellm_client,
    init_decomposition_client,
    init_litellm_client,
)

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Gerencia o ciclo de vida da aplicação.

    STARTUP:
        1. Qdrant  — valida coleção 'ue5_docs'
        1.1 Qdrant — migração idempotente/aditiva do espaço de vetor
            esparso 'keywords_sparse' (busca híbrida — ver
            ingestion_service.ensure_sparse_vectors_enabled). Não bloqueante:
            se a coleção ainda não existir (nenhuma ingestão rodou ainda),
            fica registrado um warning e a migração é reaplicada
            automaticamente na próxima ingestão.
        2. Redis   — pool de conexões para sessões
        3. LiteLLM — cliente HTTP para embeddings (mesmo proxy)
        4. LiteLLM — cliente HTTP para chat/completions (mesmo proxy)
        5. LiteLLM — cliente HTTP para decomposição/tradução de query
           (mesmo proxy; client dedicado — ver rag_service.decompose_query)

    SHUTDOWN (ordem inversa):
        1. LiteLLM client de decomposição de query fechado
        2. LiteLLM client de chat/completions fechado
        3. LiteLLM client de embeddings fechado
        4. Redis fechado
        5. Qdrant fechado
    """
    log.info("=" * 60)
    log.info("Iniciando %s v%s [%s]", settings.app_name, settings.app_version, settings.app_env)
    log.info("=" * 60)

    startup_errors: list[str] = []

    qdrant_client = None
    try:
        qdrant_client = await init_qdrant()
    except ConnectionError as exc:
        log.error("FALHA ao conectar Qdrant: %s", exc)
        startup_errors.append(f"Qdrant: {exc}")

    if qdrant_client is not None:
        # BUSCA HÍBRIDA: migração idempotente/aditiva do espaço esparso.
        # Não bloqueante de propósito — se falhar (ex: coleção ainda não
        # existe, nenhuma ingestão rodou ainda), a aplicação sobe normalmente
        # e a migração é reaplicada automaticamente no início da próxima
        # ingestão (ver ingestion_service._run_ingestion).
        try:
            await ensure_sparse_vectors_enabled(qdrant_client)
        except Exception as exc:
            log.warning(
                "Migração de sparse vectors (busca híbrida) não aplicada no "
                "startup: %s. Será retentada na próxima ingestão.",
                exc,
            )

    try:
        await init_redis()
    except ConnectionError as exc:
        log.error("FALHA ao conectar Redis: %s", exc)
        startup_errors.append(f"Redis: {exc}")

    try:
        await init_postgres()
    except ConnectionError as exc:
        log.error("FALHA ao conectar Postgres: %s", exc)
        startup_errors.append(f"Postgres: {exc}")

    try:
        await init_embedding_client()
    except Exception as exc:
        log.error("FALHA ao inicializar EmbeddingService: %s", exc)
        startup_errors.append(f"EmbeddingService: {exc}")

    try:
        await init_litellm_client()
    except Exception as exc:
        log.error("FALHA ao inicializar client de chat/completions do LiteLLM: %s", exc)
        startup_errors.append(f"LiteLLM (chat): {exc}")

    try:
        await init_decomposition_client()
    except Exception as exc:
        log.error("FALHA ao inicializar client de decomposição de query do LiteLLM: %s", exc)
        startup_errors.append(f"LiteLLM (decomposição): {exc}")

    if startup_errors:
        log.warning(
            "Aplicação iniciada com %d erro(s) de dependência: %s",
            len(startup_errors),
            "; ".join(startup_errors),
        )
    else:
        log.info("Todas as dependências conectadas. Aplicação pronta ✓")
        log.info("LiteLLM: %s (embeddings + LLM)", settings.litellm_base_url)
        log.info("Qdrant:  %s (coleção: %s)", settings.qdrant_url, settings.qdrant_collection)
        log.info("Redis:   %s:%d", settings.redis_host, settings.redis_port)
        log.info("Embedding model: %s (dim=%d)", settings.embedding_model, settings.embedding_dim)

    yield

    log.info("Encerrando aplicação...")
    await close_decomposition_client()
    await close_litellm_client()
    await close_embedding_client()
    await close_redis()
    await close_postgres()
    await close_qdrant()
    log.info("Shutdown concluído.")


def create_app() -> FastAPI:
    """Factory function — facilita testes unitários."""

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Assistente de IA especializado em Unreal Engine 5. "
            "Usa RAG sobre a documentação oficial UE5 para responder perguntas "
            "com precisão técnica. Todos os modelos são servidos via LiteLLM proxy."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/v1")

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Exceção não tratada em %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Erro interno do servidor. Verifique os logs."},
        )

    return app


app: FastAPI = create_app()