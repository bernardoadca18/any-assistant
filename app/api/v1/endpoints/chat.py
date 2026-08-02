"""
app/api/v1/endpoints/chat.py
=============================
Endpoint POST /v1/chat — pipeline RAG completo (run_rag_pipeline /
run_rag_pipeline_stream, em app/services/ai/rag_service.py) + persistência
durável no Postgres (app/services/db/session_service.py).

O histórico usado para montar o PROMPT do LLM continua 100% a cargo de
app/services/ai/session_service.py (build_messages_for_llm/save_turn,
chamado DE DENTRO de run_rag_pipeline/run_rag_pipeline_stream) — este
endpoint não mexe nisso. Ele só garante que cada mensagem também seja
gravada no Postgres, para o frontend conseguir recarregar o histórico
completo ao abrir a aplicação (o Redis tem TTL curto e não serve pra isso).

`stream: true` retorna text/event-stream no formato consumido por
lib/api.ts::sendChatMessageStream no frontend (eventos "token", "sources",
"done", "error").
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentTenant
from app.core.config import settings
from app.core.domains.registry import domain_registry
from app.db.postgres import get_db_session
from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
from app.schemas.chat import ChatRequest, ChatResponse, ModelInfo
from app.services.ai.rag_service import run_rag_pipeline, run_rag_pipeline_stream
from app.services import session_service

log = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/models",
    response_model=list[ModelInfo],
    summary="List available models from LiteLLM",
    tags=["agent"],
)
async def list_models(
    auth: CurrentTenant,
):
    """
    Returns a list of available models from the LiteLLM proxy.
    Proxies to LiteLLM's /models endpoint.
    """
    import httpx

    client = httpx.AsyncClient(
        base_url=settings.litellm_base_url,
        headers=settings.litellm_headers,
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
    )

    try:
        resp = await client.get("/models")
        resp.raise_for_status()
        data = resp.json()
        # LiteLLM returns OpenAI-compatible format: {"object": "list", "data": [{"id": "model-name", ...}]}
        models = [ModelInfo(**m) for m in data.get("data", []) if m.get("id")]
        return models
    except Exception as exc:
        log.error("Failed to fetch models from LiteLLM: %s", exc)
        # Return fallback list on error
        return [
            ModelInfo(id="ollama/qwen2.5-coder:7b"),
            ModelInfo(id="ollama/qwen2.5-coder:14b"),
            ModelInfo(id="ollama/deepseek-coder-v2:16b"),
            ModelInfo(id="gemini/gemini-2.0-flash"),
        ]
    finally:
        await client.aclose()


@router.post(
    "",  # router.py inclui este router com prefix="/chat" → path final /v1/chat
    response_model=None,  # pode retornar ChatResponse (JSON) OU StreamingResponse (SSE)
    status_code=status.HTTP_200_OK,
    summary="Enviar mensagem ao Assistente (multi-domain: Godot 4.x / UE5)",
    tags=["agent"],
)
async def chat(
    auth: CurrentTenant,
    request: ChatRequest,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis_client),
    db: AsyncSession = Depends(get_db_session),
    x_domain_id: str | None = Header(default=None, alias="X-Domain-ID"),
):
    # CORREÇÃO (ALTO — domain_id nunca era resolvido no endpoint): antes,
    # run_rag_pipeline/run_rag_pipeline_stream sempre operavam com o domínio
    # default hardcoded (UE5). Resolução por prioridade: domain_id explícito
    # no payload → header X-Domain-ID → settings.active_domain (ver
    # app.core.domains.registry.DomainRegistry.resolve).
    domain_id = domain_registry.resolve(
        explicit_domain_id=request.domain_id,
        header_domain_id=x_domain_id,
        default_domain_id=settings.active_domain,
    ).domain_id
    # Reescreve no próprio request: run_rag_pipeline/_stream leem
    # `getattr(request, "domain_id", None)` — sem isso, uma resolução vinda
    # do header X-Domain-ID (sem domain_id no payload) nunca chegaria ao
    # pipeline.
    request.domain_id = domain_id

    # Garante a sessão no Postgres (cria na primeira mensagem) e persiste a
    # mensagem do usuário ANTES de gerar a resposta — assim, mesmo que a
    # geração falhe, a pergunta do usuário não se perde.
    session = await session_service.get_or_create_session(
        db,
        request.session_id,
        tenant_id=auth.tenant_id,
        default_model=request.model,
        default_top_k=request.top_k,
        project_source_filter=request.project_source_filter,
        domain_id=domain_id,
    )
    await session_service.touch_session_title_if_needed(db, session, request.message)
    await session_service.append_user_message(
        db, session.id, request.message, request.model_dump(), domain_id=domain_id
    )
    await db.commit()

    if request.stream:
        return StreamingResponse(
            _stream_chat_response(request, session.id, qdrant, redis, db),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # desativa buffering de proxies (nginx) para SSE
            },
        )

    return await _sync_chat_response(request, session.id, qdrant, redis, db)


async def _sync_chat_response(
    request: ChatRequest,
    session_id: uuid.UUID,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    db: AsyncSession,
) -> ChatResponse:
    try:
        result: ChatResponse = await run_rag_pipeline(request=request, qdrant=qdrant, redis=redis)
    except RuntimeError as exc:
        log.error("Erro no pipeline RAG | session=%s: %s", session_id, exc)
        await session_service.append_assistant_message(
            db,
            session_id,
            content="O assistente está temporariamente indisponível.",
            status="error",
            error_detail=str(exc),
            model_used=None,
            prompt_tokens=None,
            completion_tokens=None,
            retrieved_chunks=[],
            request_payload=request.model_dump(),
            domain_id=request.domain_id,
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Erro inesperado no pipeline RAG | session=%s", session_id)
        await session_service.append_assistant_message(
            db,
            session_id,
            content="Erro interno ao processar a busca de documentação.",
            status="error",
            error_detail=str(exc),
            model_used=None,
            prompt_tokens=None,
            completion_tokens=None,
            retrieved_chunks=[],
            request_payload=request.model_dump(),
            domain_id=request.domain_id,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro interno ao processar a requisição. Verifique os logs do servidor.",
        ) from exc

    await session_service.append_assistant_message(
        db,
        session_id,
        content=result.answer,
        status="success",
        error_detail=None,
        model_used=result.model_used,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        retrieved_chunks=result.retrieved_chunks,
        request_payload=request.model_dump(),
        domain_id=request.domain_id,
    )
    await db.commit()

    return result


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _stream_chat_response(
    request: ChatRequest,
    session_id: uuid.UUID,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    db: AsyncSession,
):
    """
    Consome run_rag_pipeline_stream() (app/services/ai/rag_service.py) e
    repassa os eventos como SSE, acumulando o texto completo para persistir
    no Postgres ao final. O histórico do Redis (build_messages_for_llm/
    save_turn) já é gerenciado inteiramente DENTRO de run_rag_pipeline_stream
    — este endpoint só cuida do Postgres e do protocolo SSE.
    """
    full_answer_parts: list[str] = []
    final_chunks = []
    final_model_used = request.model
    final_prompt_tokens: int | None = None
    final_completion_tokens: int | None = None

    try:
        async for event in run_rag_pipeline_stream(request=request, qdrant=qdrant, redis=redis):
            if event["type"] == "sources":
                # RetrievedChunk é um objeto Pydantic — serializa antes de mandar pro SSE.
                final_chunks = event["retrieved_chunks"]
                yield _sse(
                    {
                        "type": "sources",
                        "retrieved_chunks": [c.model_dump() for c in final_chunks],
                    }
                )
            elif event["type"] == "token":
                full_answer_parts.append(event["content"])
                yield _sse(event)
            elif event["type"] == "done":
                final_model_used = event.get("model_used", final_model_used)
                final_prompt_tokens = event.get("prompt_tokens")
                final_completion_tokens = event.get("completion_tokens")
                yield _sse(
                    {
                        "type": "done",
                        "session_id": str(session_id),
                        "model_used": final_model_used,
                        "prompt_tokens": final_prompt_tokens,
                        "completion_tokens": final_completion_tokens,
                        "created_at": _utcnow_iso(),
                    }
                )

        full_answer = "".join(full_answer_parts)

        await session_service.append_assistant_message(
            db,
            session_id,
            content=full_answer,
            status="success",
            error_detail=None,
            model_used=final_model_used,
            prompt_tokens=final_prompt_tokens,
            completion_tokens=final_completion_tokens,
            retrieved_chunks=final_chunks,
            request_payload=request.model_dump(),
            domain_id=request.domain_id,
        )
        await db.commit()

    except Exception as exc:
        log.exception("Erro no streaming do pipeline RAG | session=%s", session_id)
        detail = str(exc) if isinstance(exc, RuntimeError) else "Erro interno durante o streaming."
        yield _sse({"type": "error", "detail": detail})

        await session_service.append_assistant_message(
            db,
            session_id,
            content="".join(full_answer_parts) or "O assistente está temporariamente indisponível.",
            status="error",
            error_detail=str(exc),
            model_used=final_model_used,
            prompt_tokens=None,
            completion_tokens=None,
            retrieved_chunks=final_chunks,
            request_payload=request.model_dump(),
            domain_id=request.domain_id,
        )
        await db.commit()


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()