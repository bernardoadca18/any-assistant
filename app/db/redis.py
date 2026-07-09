"""
app/db/redis.py
===============
Gerenciamento do ciclo de vida do cliente Redis para memória do agente.

Responsabilidades:
- Armazenar histórico de conversas por session_id (TTL configurável)
- Armazenar contextos e metadados de sessão
- Armazenar o manifesto de checkpoint/idempotência da ingestão (ver
  app/services/ai/ingestion_service.py)
- NÃO é o Redis do LiteLLM — este é exclusivo do agente UE5

Estrutura de chaves:
    session:{session_id}:history  → lista JSON de mensagens
    session:{session_id}:meta     → hash com metadados (created_at, model usado etc.)
    manifest:{collection_name}    → hash (caminho_relativo → sha256 do arquivo),
                                     o checkpoint de ingestão por coleção Qdrant
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_redis_client: Redis | None = None


async def init_redis() -> Redis:
    """
    Inicializa o pool de conexões Redis e valida a conectividade.
    Deve ser chamado no lifespan startup do FastAPI.

    Raises:
        ConnectionError: se o Redis não responder ao PING
    """
    global _redis_client

    log.info(
        "Conectando ao Redis do agente em %s:%d...",
        settings.redis_host,
        settings.redis_port,
    )

    client: Redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,       # retorna str em vez de bytes
        socket_connect_timeout=5.0,
        socket_timeout=5.0,
        retry_on_timeout=True,
        max_connections=20,
    )

    try:
        await client.ping()
        log.info("Redis do agente conectado com sucesso.")
    except RedisConnectionError as exc:
        raise ConnectionError(
            f"Não foi possível conectar ao Redis em "
            f"{settings.redis_host}:{settings.redis_port}: {exc}"
        ) from exc

    _redis_client = client
    return client


async def close_redis() -> None:
    """Fecha o pool de conexões Redis. Chamado no lifespan shutdown."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
        log.info("Conexão com Redis encerrada.")


def get_redis_client() -> Redis:
    """
    FastAPI Dependency — retorna o cliente Redis inicializado.

    Uso:
        async def my_endpoint(redis: Redis = Depends(get_redis_client)):
            ...
    """
    if _redis_client is None:
        raise RuntimeError(
            "Redis client não inicializado. "
            "Verifique se o lifespan do FastAPI está configurado corretamente."
        )
    return _redis_client


# ---------------------------------------------------------------------------
# Helpers de chave — centraliza a nomenclatura para evitar typos
# ---------------------------------------------------------------------------

def session_history_key(session_id: str) -> str:
    """Chave Redis para o histórico de mensagens de uma sessão."""
    return f"session:{session_id}:history"


def session_meta_key(session_id: str) -> str:
    """Chave Redis para metadados de uma sessão."""
    return f"session:{session_id}:meta"


def ingestion_manifest_key(collection_name: str) -> str:
    """
    Chave Redis (Hash) do manifesto de checkpoint/idempotência de ingestão
    de uma coleção Qdrant específica — isola os checkpoints entre coleções
    diferentes (ex: `manifest:ue5_docs`, `manifest:ue5_docs_v2`).

    Campos da hash: caminho relativo do arquivo (à raiz da pasta ingerida)
    → SHA-256 hexdigest do conteúdo bruto do arquivo na última ingestão
    bem-sucedida. Ver app/services/ai/ingestion_service.py para o fluxo
    completo de leitura/escrita.
    """
    return f"manifest:{collection_name}"