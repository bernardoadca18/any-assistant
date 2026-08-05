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

---------------------------------------------------------------------------
INTEGRAÇÃO MULTI-TENANT — LEIA ANTES DE MEXER (ver também o bloco
equivalente no topo de app/services/ai/rag_service.py, que documenta o
lado de CONSUMO desta config; aqui documentamos o lado de RESOLUÇÃO)
---------------------------------------------------------------------------
Este é o ÚNICO ponto do backend onde `ResolvedCollectionConfig` é
resolvido a partir do Postgres (via TenantConfigService) para uma
requisição de chat — `rag_service.py` deliberadamente NÃO toca
`AsyncSession`/Postgres, só recebe o objeto já resolvido.

FLUXO:
    1. Handler `chat()` resolve `resolved_config` uma única vez, logo no
       início (função `_resolve_tenant_config` abaixo), a partir de
       `request.tenant_id` (+ opcionalmente `request.collection_name`).
    2. `resolved_config` (pode ser None — ver abaixo) é propagado para
       `_sync_chat_response`/`_stream_chat_response` e destes para
       `run_rag_pipeline`/`run_rag_pipeline_stream`.

QUANDO `resolved_config` FICA None (e por quê isso é seguro): se
`request.tenant_id` não vier na requisição (schema ainda não migrado —
ver PENDÊNCIA abaixo) OU a resolução falhar por qualquer motivo (tenant
não encontrado, nenhuma coleção configurada para o domínio, Postgres
indisponível), `_resolve_tenant_config` NUNCA propaga essa falha para o
usuário — ela loga um warning e retorna None. `run_rag_pipeline` já foi
projetado para tratar `resolved_config=None` como um modo de operação
totalmente válido (settings global, sem isolamento de tenant — ver
docstring de `run_rag_pipeline`). Essa escolha é deliberada dado o
contexto atual: o app está em desenvolvimento, a prioridade imediata é
Godot 4 funcionando perfeitamente, e resolução de tenant não pode se
tornar um novo ponto único de falha que derruba o chat inteiro se o
Postgres/Redis do tenant service tiver um problema pontual.

PENDÊNCIA CONHECIDA (documentada, não bloqueante — mesma pendência
anotada em rag_service.py): `ChatRequest.tenant_id` não existe como
campo formal neste snapshot do schema (app/schemas/chat.py não foi
fornecido para esta auditoria). Usamos `getattr(request, "tenant_id",
None)` pelo mesmo motivo que `domain_id` já usa esse padrão em
rag_service.py — mantém o código funcional e correto tanto antes quanto
depois do schema ser migrado. Quando `ChatRequest` ganhar um campo
`tenant_id: uuid.UUID | None` real (idealmente também `collection_name:
str | None`, para permitir que um tenant com múltiplas coleções escolha
qual usar), o `getattr` abaixo pode virar acesso direto sem nenhuma
outra mudança necessária.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgres import get_db_session
from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.ai.rag_service import run_rag_pipeline, run_rag_pipeline_stream
from app.services.db import session_service
from app.services.tenant_config_service import ResolvedCollectionConfig, TenantConfigService

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency provider — TenantConfigService
# ---------------------------------------------------------------------------
# NOTA DE IMPLEMENTAÇÃO (verificar contra o padrão real do projeto): este
# provider segue o mesmo formato de `get_qdrant_client`/`get_redis_client`
# (ver app/db/qdrant.py, app/db/redis.py) por consistência, MAS
# `TenantConfigService` mantém um `_process_cache` (dict em memória, ver
# tenant_config_service.py `__init__`) que só cumpre sua função de cache
# L1 se a MESMA instância for reaproveitada entre requisições — criar uma
# instância nova a cada chamada (ex.: `TenantConfigService(redis)` direto
# dentro do handler) tornaria esse cache inútil (sempre vazio) e faria
# toda requisição pagar o custo de L2 (Redis) na melhor hipótese.
#
# Este singleton módulo-level espelha o padrão de `_qdrant_client` em
# app/db/qdrant.py (variável global, inicializada uma vez, obtida via
# Depends) — mas AQUI a inicialização é lazy (na primeira chamada) em vez
# de acontecer explicitamente no lifespan startup do FastAPI, porque
# `TenantConfigService` só precisa do client Redis (já garantidamente
# disponível via `get_redis_client`) e não tem nenhum health check próprio
# que justifique um hook de startup dedicado. Se o padrão real do projeto
# preferir inicialização explícita no lifespan (mesmo estilo de
# `init_qdrant`/`init_decomposition_client`), mover essa lógica para lá é
# uma troca mecânica — a função `get_tenant_config_service` abaixo é o
# único ponto que precisaria mudar.
_tenant_config_service: TenantConfigService | None = None


def get_tenant_config_service(redis: Redis = Depends(get_redis_client)) -> TenantConfigService:
    """Dependency FastAPI — retorna o TenantConfigService singleton do processo."""
    global _tenant_config_service
    if _tenant_config_service is None:
        _tenant_config_service = TenantConfigService(redis)
    return _tenant_config_service


async def _resolve_tenant_config(
    request: ChatRequest,
    db: AsyncSession,
    tenant_config_service: TenantConfigService,
) -> ResolvedCollectionConfig | None:
    """
    Resolve `ResolvedCollectionConfig` para a requisição, ou None se
    nenhum tenant puder ser resolvido — NUNCA levanta exceção (ver
    justificativa completa no bloco de comentário no topo do módulo).

    Prioridade de resolução:
        1. `request.tenant_id` + `request.collection_name` (se ambos
           presentes) → `resolve_collection_config` (coleção específica).
        2. `request.tenant_id` sozinho (sem collection_name) →
           `resolve_collection_config` sem collection_name, que internamente
           cai para a coleção default do tenant, e se essa também não
           existir, tenta casar por `domain_id` — ver
           `TenantConfigService.resolve_collection_config`.
        3. Nenhum `tenant_id` na requisição → retorna None imediatamente,
           SEM logar warning (esperado: nem toda requisição precisa ter
           tenant, especialmente durante o desenvolvimento single-tenant
           atual, focado em Godot 4).

    Qualquer falha na resolução (tenant inexistente, nenhuma coleção
    configurada, erro de Postgres/Redis) é logada como warning e resulta
    em None — run_rag_pipeline trata None como modo de operação válido
    (settings global, ver docstring lá).
    """
    tenant_id_raw = getattr(request, "tenant_id", None)
    if not tenant_id_raw:
        return None

    try:
        tenant_id = tenant_id_raw if isinstance(tenant_id_raw, uuid.UUID) else uuid.UUID(str(tenant_id_raw))
    except (ValueError, TypeError):
        log.warning(
            "request.tenant_id='%s' não é um UUID válido — ignorando resolução "
            "de tenant, requisição seguirá com settings global.",
            tenant_id_raw,
        )
        return None

    collection_name = getattr(request, "collection_name", None)

    try:
        return await tenant_config_service.resolve_collection_config(
            db, tenant_id, collection_name
        )
    except ValueError as exc:
        # Tenant/coleção não encontrados — condição esperada durante
        # desenvolvimento (nem todo tenant_id enviado pelo client
        # necessariamente já tem uma Collection cadastrada no Postgres).
        # Loga e segue com resolved_config=None (settings global).
        log.warning(
            "Não foi possível resolver config de tenant/coleção "
            "(tenant_id=%s, collection_name=%s): %s. Requisição seguirá "
            "com settings global (comportamento legado, sem isolamento "
            "de tenant).",
            tenant_id,
            collection_name,
            exc,
        )
        return None
    except Exception as exc:
        # Qualquer outra falha (Postgres/Redis indisponível, etc.) — mesma
        # política: nunca derruba o chat por causa da camada de tenant.
        log.error(
            "Erro inesperado ao resolver config de tenant (tenant_id=%s): %s. "
            "Requisição seguirá com settings global.",
            tenant_id,
            exc,
            exc_info=True,
        )
        return None


@router.post(
    "",  # router.py inclui este router com prefix="/chat" → path final /v1/chat
    response_model=None,  # pode retornar ChatResponse (JSON) OU StreamingResponse (SSE)
    status_code=status.HTTP_200_OK,
    summary="Enviar mensagem ao Assistente UE5",
    tags=["agent"],
)
async def chat(
    request: ChatRequest,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis_client),
    db: AsyncSession = Depends(get_db_session),
    tenant_config_service: TenantConfigService = Depends(get_tenant_config_service),
):
    # Garante a sessão no Postgres (cria na primeira mensagem) e persiste a
    # mensagem do usuário ANTES de gerar a resposta — assim, mesmo que a
    # geração falhe, a pergunta do usuário não se perde.
    session = await session_service.get_or_create_session(
        db,
        request.session_id,
        default_model=request.model,
        default_top_k=request.top_k,
        project_source_filter=request.project_source_filter,
    )
    await session_service.touch_session_title_if_needed(db, session, request.message)
    await session_service.append_user_message(db, session.id, request.message, request.model_dump())
    await db.commit()

    # MULTI-TENANT: resolvido UMA ÚNICA VEZ aqui, antes de decidir entre
    # streaming/síncrono, e propagado já resolvido para os dois caminhos —
    # ver bloco de comentário no topo do módulo para o fluxo completo e a
    # justificativa de por que isso nunca bloqueia/derruba a requisição.
    resolved_config = await _resolve_tenant_config(request, db, tenant_config_service)

    if request.stream:
        return StreamingResponse(
            _stream_chat_response(request, session.id, qdrant, redis, db, resolved_config),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # desativa buffering de proxies (nginx) para SSE
            },
        )

    return await _sync_chat_response(request, session.id, qdrant, redis, db, resolved_config)


async def _sync_chat_response(
    request: ChatRequest,
    session_id: uuid.UUID,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    db: AsyncSession,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> ChatResponse:
    try:
        result: ChatResponse = await run_rag_pipeline(
            request=request, qdrant=qdrant, redis=redis, resolved_config=resolved_config
        )
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
    resolved_config: ResolvedCollectionConfig | None = None,
):
    """
    Consome run_rag_pipeline_stream() (app/services/ai/rag_service.py) e
    repassa os eventos como SSE, acumulando o texto completo para persistir
    no Postgres ao final. O histórico do Redis (build_messages_for_llm/
    save_turn) já é gerenciado inteiramente DENTRO de run_rag_pipeline_stream
    — este endpoint só cuida do Postgres e do protocolo SSE.

    `resolved_config`: já resolvido pelo handler `chat()` (ver
    `_resolve_tenant_config`) — repassado direto para
    `run_rag_pipeline_stream`, nenhuma resolução acontece aqui.
    """
    full_answer_parts: list[str] = []
    final_chunks = []
    final_model_used = request.model
    final_prompt_tokens: int | None = None
    final_completion_tokens: int | None = None

    try:
        async for event in run_rag_pipeline_stream(
            request=request, qdrant=qdrant, redis=redis, resolved_config=resolved_config
        ):
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
        )
        await db.commit()


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()