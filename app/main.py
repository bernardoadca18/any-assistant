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
from app.db.qdrant import close_qdrant, init_qdrant
from app.db.redis import close_redis, init_redis
from app.services.ai.embedding_service import close_embedding_client, init_embedding_client

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Gerencia o ciclo de vida da aplicação.

    STARTUP:
        1. Qdrant  — valida coleção 'ue5_docs'
        2. Redis   — pool de conexões para sessões
        3. LiteLLM — cliente HTTP para embeddings e LLM (mesmo proxy)

    SHUTDOWN (ordem inversa):
        1. LiteLLM client fechado
        2. Redis fechado
        3. Qdrant fechado
    """
    log.info("=" * 60)
    log.info("Iniciando %s v%s [%s]", settings.app_name, settings.app_version, settings.app_env)
    log.info("=" * 60)

    startup_errors: list[str] = []

    try:
        await init_qdrant()
    except ConnectionError as exc:
        log.error("FALHA ao conectar Qdrant: %s", exc)
        startup_errors.append(f"Qdrant: {exc}")

    try:
        await init_redis()
    except ConnectionError as exc:
        log.error("FALHA ao conectar Redis: %s", exc)
        startup_errors.append(f"Redis: {exc}")

    try:
        await init_embedding_client()
    except Exception as exc:
        log.error("FALHA ao inicializar EmbeddingService: %s", exc)
        startup_errors.append(f"EmbeddingService: {exc}")

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
    await close_embedding_client()
    await close_redis()
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