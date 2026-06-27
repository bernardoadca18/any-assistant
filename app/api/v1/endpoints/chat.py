"""
app/api/v1/endpoints/chat.py
=============================
Endpoint POST /v1/chat — esqueleto preparado para a lógica RAG.

Na iteração atual (Fase 1 — Infraestrutura), este endpoint:
1. Valida o request via Pydantic
2. Retorna uma resposta stub confirmando que o pipeline está conectado
3. Documenta os pontos de extensão onde a lógica RAG será injetada

Na Fase 2, a lógica real de RAG será implementada em:
    app/services/ai/rag_service.py
    app/services/ai/embedding_service.py
    app/services/cache/session_service.py
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.core.config import settings
from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
from app.schemas.chat import ChatRequest, ChatResponse, RetrievedChunk

log = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Enviar mensagem ao Assistente UE5",
    description=(
        "Endpoint principal do agente RAG. Recebe uma pergunta sobre Unreal Engine 5, "
        "recupera chunks relevantes do Qdrant e retorna uma resposta gerada pelo LLM via LiteLLM."
    ),
    tags=["agent"],
)
async def chat(
    request: ChatRequest,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis_client),
) -> ChatResponse:
    """
    Pipeline RAG (implementação completa na Fase 2):

    1. [CACHE] Carregar histórico da sessão do Redis
    2. [EMBED] Gerar embedding da pergunta via Ollama (mxbai-embed-large)
    3. [RETRIEVE] Buscar top-k chunks no Qdrant (coleção 'ue5_docs')
    4. [AUGMENT] Montar prompt com contexto recuperado
    5. [GENERATE] Chamar LiteLLM → litellm-proxy:4000
    6. [CACHE] Salvar turno no histórico da sessão no Redis
    7. [RETURN] Retornar resposta com chunks citados
    """
    log.info(
        "Chat request | session=%s model=%s top_k=%d",
        request.session_id,
        request.model,
        request.top_k,
    )

    # -------------------------------------------------------------------------
    # STUB — substituir pelo RAGService na Fase 2
    # -------------------------------------------------------------------------
    stub_answer = (
        f"[STUB - Fase 1] Infraestrutura operacional ✓ | "
        f"Pergunta recebida: '{request.message}' | "
        f"Session: {request.session_id} | "
        f"Modelo solicitado: {request.model} | "
        f"Qdrant conectado: {qdrant is not None} | "
        f"Redis conectado: {redis is not None}"
    )

    return ChatResponse(
        session_id=request.session_id,
        answer=stub_answer,
        model_used=request.model,
        retrieved_chunks=[],      # Fase 2: populado pelo RAGService
        prompt_tokens=None,       # Fase 2: extraído da resposta do LiteLLM
        completion_tokens=None,
    )