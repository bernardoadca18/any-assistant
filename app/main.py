"""
app/main.py
===========
Ponto de entrada da aplicação FastAPI.

Responsabilidades:
- Configuração do lifespan (startup / shutdown)
- Inicialização ordenada das conexões (Qdrant → Redis)
- Registro dos routers
- Middleware (CORS, logging de requests)
- Documentação OpenAPI

Princípio: este arquivo é o "composition root" — orquestra dependências,
mas não contém lógica de negócio.
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

# ---------------------------------------------------------------------------
# Logging — configurado ANTES de qualquer outro import que possa logar
# ---------------------------------------------------------------------------
setup_logging()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — substitui @app.on_event (deprecated no FastAPI moderno)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Gerencia o ciclo de vida da aplicação.

    STARTUP (ordem importa — Qdrant antes do Redis para fail-fast):
        1. Conecta ao Qdrant e valida a coleção 'ue5_docs'
        2. Conecta ao Redis do agente

    SHUTDOWN (ordem inversa):
        1. Fecha Redis
        2. Fecha Qdrant
    """
    # --- STARTUP ---
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

    if startup_errors:
        log.warning(
            "Aplicação iniciada com %d erro(s) de dependência. "
            "Algumas funcionalidades podem estar indisponíveis: %s",
            len(startup_errors),
            "; ".join(startup_errors),
        )
    else:
        log.info("Todas as dependências conectadas. Aplicação pronta ✓")
        log.info("LiteLLM proxy: %s", settings.litellm_base_url)
        log.info("Qdrant:        %s (coleção: %s)", settings.qdrant_url, settings.qdrant_collection)
        log.info("Redis:         %s:%d", settings.redis_host, settings.redis_port)

    # Cede controle para a aplicação
    yield

    # --- SHUTDOWN ---
    log.info("Encerrando aplicação...")
    await close_redis()
    await close_qdrant()
    log.info("Shutdown concluído.")


# ---------------------------------------------------------------------------
# Instância da aplicação
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Factory function — facilita testes (cada teste pode criar sua própria app)."""

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Assistente de IA especializado em Unreal Engine 5. "
            "Usa RAG sobre a documentação oficial UE5 para responder perguntas "
            "com precisão técnica, acelerado por modelos locais via Ollama/LiteLLM."
        ),
        lifespan=lifespan,
        # Em produção, desabilita os endpoints de docs para reduzir surface de ataque
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    # --- CORS ----------------------------------------------------------------
    # Para uso pessoal/local, aceita qualquer origem.
    # Em produção pública, restrinja para o domínio do frontend.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.is_development else ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # --- Routers -------------------------------------------------------------
    app.include_router(api_router, prefix="/v1")

    # --- Exception Handlers --------------------------------------------------
    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Garante que exceções não tratadas retornem JSON válido (nunca HTML)."""
        log.exception("Exceção não tratada em %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Erro interno do servidor. Verifique os logs."},
        )

    return app


# Instância global usada pelo Uvicorn
app: FastAPI = create_app()