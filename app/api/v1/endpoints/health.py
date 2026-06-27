"""
app/api/v1/endpoints/health.py
==============================
Endpoint de health check com verificação ativa de todas as dependências.
Retorna HTTP 200 se tudo OK, 503 se alguma dependência falhar.

CORREÇÃO: importado asyncio no topo do módulo (não dentro da função).
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.core.config import settings
from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
from app.schemas.chat import DependencyStatus, HealthResponse

log = logging.getLogger(__name__)
router = APIRouter()


async def _check_qdrant(client: AsyncQdrantClient) -> DependencyStatus:
    try:
        start = time.perf_counter()
        await client.get_collections()
        latency = round((time.perf_counter() - start) * 1000, 2)
        return DependencyStatus(status="ok", latency_ms=latency)
    except Exception as exc:
        log.warning("Health check Qdrant falhou: %s", exc)
        return DependencyStatus(status="error", detail=str(exc))


async def _check_redis(client: Redis) -> DependencyStatus:
    try:
        start = time.perf_counter()
        await client.ping()
        latency = round((time.perf_counter() - start) * 1000, 2)
        return DependencyStatus(status="ok", latency_ms=latency)
    except Exception as exc:
        log.warning("Health check Redis falhou: %s", exc)
        return DependencyStatus(status="error", detail=str(exc))


async def _check_litellm() -> DependencyStatus:
    # str() necessário porque AnyHttpUrl retorna objeto Url, não str pura
    url = str(settings.litellm_base_url).rstrip("/") + "/health/readiness"
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(url, headers=settings.litellm_headers)
        latency = round((time.perf_counter() - start) * 1000, 2)

        if resp.status_code == 200:
            return DependencyStatus(status="ok", latency_ms=latency)
        else:
            return DependencyStatus(
                status="degraded",
                latency_ms=latency,
                detail=f"HTTP {resp.status_code}",
            )
    except Exception as exc:
        log.warning("Health check LiteLLM falhou: %s", exc)
        return DependencyStatus(status="error", detail=str(exc))


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check completo",
    tags=["infra"],
)
async def health_check() -> JSONResponse:
    """
    Verifica a saúde da aplicação e de todas as dependências.
    - **200**: todos os serviços operacionais
    - **503**: uma ou mais dependências com falha
    """
    qdrant_client = get_qdrant_client()
    redis_client = get_redis_client()

    qdrant_status, redis_status, litellm_status = await asyncio.gather(
        _check_qdrant(qdrant_client),
        _check_redis(redis_client),
        _check_litellm(),
    )

    dependencies = {
        "qdrant": qdrant_status,
        "redis": redis_status,
        "litellm": litellm_status,
    }

    has_error = any(d.status == "error" for d in dependencies.values())
    has_degraded = any(d.status == "degraded" for d in dependencies.values())

    if has_error:
        overall = "error"
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif has_degraded:
        overall = "degraded"
        http_status = status.HTTP_200_OK
    else:
        overall = "ok"
        http_status = status.HTTP_200_OK

    response = HealthResponse(
        status=overall,
        version=settings.app_version,
        environment=settings.app_env,
        dependencies=dependencies,
    )

    return JSONResponse(
        content=response.model_dump(mode="json"),
        status_code=http_status,
    )