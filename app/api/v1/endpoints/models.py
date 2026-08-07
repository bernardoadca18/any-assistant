"""
app/api/v1/endpoints/models.py
================================
Endpoint GET /v1/chat/models — proxy para LiteLLM /v1/models.

Retorna lista de modelos disponíveis para o frontend selecionar
no popover de parâmetros do chat.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/models", tags=["agent"])
async def list_models() -> JSONResponse:
    """
    Lista modelos disponíveis no LiteLLM proxy.

    Faz proxy para GET {litellm_base_url}/v1/models e retorna
    a lista formatada para o frontend.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.litellm_base_url}/v1/models",
                headers=settings.litellm_headers,
            )
            response.raise_for_status()

            data = response.json()

            # LiteLLM retorna no formato OpenAI: {"data": [{"id": "...", "object": "model", ...}]}
            # O frontend espera ModelInfo[] com campos {id, name?, object?}
            # Passamos direto o array "data" do LiteLLM
            models = data.get("data", [])

            return JSONResponse(content=models)

    except httpx.HTTPStatusError as exc:
        log.error(
            "Erro ao buscar modelos do LiteLLM: status=%s, body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        return JSONResponse(
            content=[],
            status_code=502,
        )
    except Exception as exc:
        log.exception("Erro inesperado ao buscar modelos do LiteLLM")
        return JSONResponse(
            content=[],
            status_code=503,
        )
