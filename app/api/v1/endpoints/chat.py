"""
app/api/v1/endpoints/chat.py
=============================
Endpoint POST /v1/chat — implementação completa do pipeline RAG.

Fase 2: stub substituído pelo RAGService real.
O endpoint recebe a requisição, delega ao run_rag_pipeline() e retorna
a resposta estruturada com chunks citados e tokens consumidos.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.ai.rag_service import run_rag_pipeline

log = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Enviar mensagem ao Assistente UE5",
    description=(
        "Pipeline RAG completo: gera embedding da pergunta, recupera chunks "
        "relevantes da documentação UE5 no Qdrant, monta prompt aumentado e "
        "gera resposta via LLM através do LiteLLM proxy. "
        "O histórico de conversa é mantido por sessão no Redis."
    ),
    tags=["agent"],
)
async def chat(
    request: ChatRequest,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis_client),
) -> ChatResponse:
    """
    Executa o pipeline RAG e retorna a resposta do assistente UE5.

    - **message**: Pergunta do usuário sobre Unreal Engine 5
    - **session_id**: UUID v4 para manter histórico de conversa (gere um e reutilize)
    - **model**: Modelo LiteLLM (ex: `ollama/qwen2.5-coder:7b`)
    - **top_k**: Número de chunks da documentação a recuperar (1-20)
    - **stream**: Não implementado nesta versão (reservado para Fase 3)
    """
    try:
        return await run_rag_pipeline(
            request=request,
            qdrant=qdrant,
            redis=redis,
        )
    except RuntimeError as exc:
        # Erros operacionais conhecidos (Ollama down, LiteLLM timeout, etc.)
        log.error("Erro no pipeline RAG | session=%s: %s", request.session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        # Erros inesperados — não vazamos stack trace para o cliente
        log.exception("Erro inesperado no pipeline RAG | session=%s", request.session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno ao processar a requisição. Verifique os logs do servidor.",
        ) from exc