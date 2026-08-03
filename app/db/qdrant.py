"""
app/db/qdrant.py
================
Gerenciamento do ciclo de vida do cliente Qdrant.
- Singleton thread-safe via módulo-level instance
- Health check explícito no startup
- Tipagem alinhada com o payload gerado pelo ue5_rag_ingestor.py
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton — criado no lifespan, usado via Depends()
# ---------------------------------------------------------------------------
_qdrant_client: AsyncQdrantClient | None = None


async def init_qdrant() -> AsyncQdrantClient:
    """
    Inicializa o cliente Qdrant e valida a coleção 'ue5_docs'.
    Deve ser chamado no lifespan startup do FastAPI.

    Raises:
        ConnectionError: se o Qdrant não estiver acessível
        RuntimeError: se a coleção não existir (o ingestor ainda não rodou)
    """
    global _qdrant_client

    log.info("Conectando ao Qdrant em %s...", settings.qdrant_url)

    client = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        timeout=10.0,
    )

    # Verifica conectividade
    try:
        await client.get_collections()
        log.info("Qdrant conectado com sucesso.")
    except Exception as exc:
        raise ConnectionError(
            f"Não foi possível conectar ao Qdrant em {settings.qdrant_url}: {exc}"
        ) from exc

    # Valida que a coleção existe e tem a dimensão correta
    await _validate_collection(client)

    _qdrant_client = client
    return client


async def _validate_collection(client: AsyncQdrantClient) -> None:
    """
    Verifica se a coleção do domínio ativo existe e tem a dimensão de embedding
    esperada (1024 para mxbai-embed-large).

    Não lança erro se a coleção não existir — apenas registra um warning,
    pois o ingestor pode ainda não ter sido executado.
    """
    collection_name = settings.qdrant_collection_for(settings.active_domain)

    try:
        collection_info = await client.get_collection(collection_name)
        vectors_config = collection_info.config.params.vectors

        # vectors pode ser VectorsConfig (named) ou VectorParams (unnamed)
        if hasattr(vectors_config, "size"):
            actual_dim = vectors_config.size
        else:
            actual_dim = None

        if actual_dim and actual_dim != settings.embedding_dim:
            log.warning(
                "ATENÇÃO: Coleção '%s' tem dimensão %d mas embedding_dim configurado é %d. "
                "Verifique se o modelo de embedding está alinhado com o ingestor.",
                collection_name,
                actual_dim,
                settings.embedding_dim,
            )
        else:
            log.info(
                "Coleção '%s' validada ✓ (dim=%d)",
                collection_name,
                settings.embedding_dim,
            )

    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            log.warning(
                "Coleção '%s' não encontrada no Qdrant. "
                "Execute o ue5_rag_ingestor.py antes de fazer queries RAG.",
                collection_name,
            )
        else:
            raise


async def close_qdrant() -> None:
    """Fecha a conexão com o Qdrant. Chamado no lifespan shutdown."""
    global _qdrant_client
    if _qdrant_client:
        await _qdrant_client.close()
        _qdrant_client = None
        log.info("Conexão com Qdrant encerrada.")


def get_qdrant_client() -> AsyncQdrantClient:
    """
    FastAPI Dependency — retorna o cliente Qdrant inicializado.

    Uso:
        async def my_endpoint(qdrant: AsyncQdrantClient = Depends(get_qdrant_client)):
            ...
    """
    if _qdrant_client is None:
        raise RuntimeError(
            "Qdrant client não inicializado. "
            "Verifique se o lifespan do FastAPI está configurado corretamente."
        )
    return _qdrant_client