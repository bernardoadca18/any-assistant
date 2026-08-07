"""
app/services/session_service.py
================================
Repositório de sessões e mensagens — Postgres como fonte de verdade,
Redis como cache de curto prazo do histórico recente (reaproveita as
chaves já definidas em app/db/redis.py: session_history_key/session_meta_key).

Fluxo:
- Toda leitura de "lista de sessões" e "mensagens de uma sessão" (usada
  pelo frontend ao abrir a aplicação) vai direto no Postgres.
- O histórico usado para montar o PROMPT do LLM (contexto multi-turn)
  tenta o Redis primeiro (rápido); se der cache miss, reconstrói a
  partir do Postgres e regrava no Redis (warm_redis_from_postgres).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.domains.registry import domain_registry
from app.db.models import ChatMessage, ChatSession, DocType, Project, RetrievedChunk
from app.db.redis import session_history_key, session_meta_key
from app.schemas.chat import ChatRequest, RetrievedChunk as RetrievedChunkSchema

log = logging.getLogger(__name__)

REDIS_HISTORY_TTL_SECONDS = 60 * 60 * 6  # 6h — histórico recente para contexto do LLM
REDIS_HISTORY_MAX_MESSAGES = 20          # janela de contexto multi-turn


# ---------------------------------------------------------------------------
# Lookups (project/doc_type) com cache local em processo — tabelas pequenas
# e praticamente estáticas.
#
# CORREÇÃO (ALTO — cache/lookup ignorava domain_id): as chaves eram apenas
# `code` (str). Com `projects`/`doc_types` migradas para UNIQUE composto
# (code, domain_id) — ver db/init/002_domain_multi_tenant_schema.sql — dois
# domínios diferentes podem ter o MESMO code (ex: um futuro doc_type
# "api_reference" em ue5 E em godot_4) apontando para linhas distintas. O
# cache agora é indexado por (domain_id, code).
# ---------------------------------------------------------------------------
_project_id_cache: dict[tuple[str, str], int] = {}
_doc_type_id_cache: dict[tuple[str, str], int] = {}


async def _resolve_project_id(db: AsyncSession, code: str | None, domain_id: str) -> int | None:
    if not code:
        return None
    cache_key = (domain_id, code)
    if cache_key in _project_id_cache:
        return _project_id_cache[cache_key]

    result = await db.execute(
        select(Project).where(Project.code == code, Project.domain_id == domain_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        # Procedência nova, não cadastrada ainda (ex: novo projeto de amostra
        # ingerido) — cria on-the-fly em vez de falhar a gravação da mensagem.
        project = Project(code=code, display_name=code, domain_id=domain_id)
        db.add(project)
        await db.flush()
        log.info("Novo project_source registrado dinamicamente: %s (domain=%s)", code, domain_id)

    _project_id_cache[cache_key] = project.id
    return project.id


async def _resolve_doc_type_id(db: AsyncSession, code: str | None, domain_id: str) -> int | None:
    if not code:
        return None
    cache_key = (domain_id, code)
    if cache_key in _doc_type_id_cache:
        return _doc_type_id_cache[cache_key]

    result = await db.execute(
        select(DocType).where(DocType.code == code, DocType.domain_id == domain_id)
    )
    doc_type = result.scalar_one_or_none()
    if doc_type is None:
        doc_type = DocType(code=code, display_name=code, domain_id=domain_id)
        db.add(doc_type)
        await db.flush()
        log.info("Novo doc_type registrado dinamicamente: %s (domain=%s)", code, domain_id)

    _doc_type_id_cache[cache_key] = doc_type.id
    return doc_type.id


# ---------------------------------------------------------------------------
# Sessões
# ---------------------------------------------------------------------------

async def get_or_create_session(
    db: AsyncSession,
    session_id: str,
    *,
    tenant_id: uuid.UUID,
    default_model: str,
    default_top_k: int,
    project_source_filter: str | None,
    domain_id: str,
) -> ChatSession:
    """
    Busca a sessão pelo UUID vindo do frontend; cria se ainda não existir
    (primeira mensagem de uma conversa nova — o frontend já gera o UUID
    antes de enviar a primeira mensagem).

    CORREÇÃO (MÉDIO — título de sessão hardcoded para UE5): `domain_id` é
    obrigatório e usado para (a) gravar `ChatSession.domain_id` e (b)
    resolver o título default via `BaseDomainProfile.default_session_title`
    do domínio ativo (ex: "Conversa Godot Engine 4" em vez de "Conversa
    Unreal Engine 5" fixo) — ver também touch_session_title_if_needed.
    """
    try:
        sid = uuid.UUID(session_id)
    except ValueError as exc:
        raise ValueError(f"session_id inválido, esperado UUID: {session_id}") from exc

    result = await db.execute(select(ChatSession).where(ChatSession.id == sid))
    session = result.scalar_one_or_none()
    if session is not None:
        return session

    profile = domain_registry.get(domain_id)
    session = ChatSession(
        id=sid,
        tenant_id=tenant_id,
        title=profile.default_session_title,
        domain_id=domain_id,
        default_model=default_model,
        default_top_k=default_top_k,
        project_source_filter=project_source_filter,
    )
    db.add(session)
    await db.flush()
    return session


async def list_sessions(
    db: AsyncSession, *, tenant_id: uuid.UUID, include_archived: bool = False
) -> list[ChatSession]:
    """
    Lista sessões de um tenant específico.

    IMPORTANTE: O isolamento multi-tenant exige que tenant_id seja sempre
    fornecido para garantir que um tenant não acesse sessões de outro.
    """
    stmt = select(ChatSession).where(ChatSession.tenant_id == tenant_id).order_by(ChatSession.updated_at.desc())
    if not include_archived:
        stmt = stmt.where(ChatSession.is_archived.is_(False))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_session_with_messages(
    db: AsyncSession, session_id: uuid.UUID, *, tenant_id: uuid.UUID
) -> ChatSession | None:
    """
    Obtém sessão com mensagens, garantindo isolamento multi-tenant.

    IMPORTANTE: O tenant_id é obrigatório para garantir que um tenant não
    acesse sessões de outro.
    """
    stmt = (
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.tenant_id == tenant_id)
        .options(
            selectinload(ChatSession.messages)
            .selectinload(ChatMessage.retrieved_chunks)
            .selectinload(RetrievedChunk.project),
            selectinload(ChatSession.messages)
            .selectinload(ChatMessage.retrieved_chunks)
            .selectinload(RetrievedChunk.doc_type),
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def delete_session(db: AsyncSession, session_id: uuid.UUID, *, tenant_id: uuid.UUID) -> bool:
    """
    Remove sessão, garantindo isolamento multi-tenant.

    IMPORTANTE: O tenant_id é obrigatório para garantir que um tenant não
    remova sessões de outro.
    """
    session = await db.get(ChatSession, session_id)
    if session is None or session.tenant_id != tenant_id:
        return False
    await db.delete(session)  # cascade cuida de messages/retrieved_chunks
    return True


async def touch_session_title_if_needed(db: AsyncSession, session: ChatSession, first_user_content: str) -> None:
    """
    Define o título da sessão a partir da primeira mensagem do usuário, uma
    única vez.

    CORREÇÃO (MÉDIO — comparação hardcoded para UE5): a condição de "ainda é
    o título default, pode sobrescrever" comparava contra a string fixa
    "Conversa Unreal Engine 5". Isso fazia com que sessões Godot 4 (cujo
    título default é "Conversa Godot Engine 4" — ver get_or_create_session)
    nunca tivessem o título atualizado a partir da primeira mensagem, já que
    a comparação sempre dava falso. Agora compara contra o
    `default_session_title` do PRÓPRIO domain_id da sessão.
    """
    profile = domain_registry.get(session.domain_id)
    if session.title != profile.default_session_title:
        return
    trimmed = first_user_content.strip()
    session.title = trimmed[:40] + ("..." if len(trimmed) > 40 else "")


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

async def _next_sequence_number(db: AsyncSession, session_id: uuid.UUID) -> int:
    result = await db.execute(
        select(ChatMessage.sequence_number)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.sequence_number.desc())
        .limit(1)
    )
    last = result.scalar_one_or_none()
    return (last or 0) + 1


async def append_user_message(
    db: AsyncSession, session_id: uuid.UUID, content: str, request_payload: dict, domain_id: str
) -> ChatMessage:
    seq = await _next_sequence_number(db, session_id)
    msg = ChatMessage(
        session_id=session_id,
        domain_id=domain_id,
        role="user",
        status="success",
        content=content,
        sequence_number=seq,
        request_payload=request_payload,
    )
    db.add(msg)
    await db.flush()
    return msg


async def append_assistant_message(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    content: str,
    status: str,
    error_detail: str | None,
    model_used: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    retrieved_chunks: list[RetrievedChunkSchema],
    request_payload: dict,
    domain_id: str,
) -> ChatMessage:
    seq = await _next_sequence_number(db, session_id)
    msg = ChatMessage(
        session_id=session_id,
        domain_id=domain_id,
        role="assistant",
        status=status,
        content=content,
        error_detail=error_detail,
        model_used=model_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        sequence_number=seq,
        request_payload=request_payload,
    )
    db.add(msg)
    await db.flush()

    for idx, chunk in enumerate(retrieved_chunks, start=1):
        project_id = await _resolve_project_id(db, chunk.project_source, domain_id=domain_id)
        doc_type_id = await _resolve_doc_type_id(db, chunk.doc_type, domain_id=domain_id)
        db.add(
            RetrievedChunk(
                message_id=msg.id,
                domain_id=domain_id,
                qdrant_chunk_id=chunk.chunk_id,
                project_id=project_id,
                doc_type_id=doc_type_id,
                score=chunk.score,
                title=chunk.title,
                source_file=chunk.source_file,
                ue_version=chunk.ue_version,
                summary_markdown=chunk.summary_markdown,
                raw_content=chunk.raw_content,
                rank_position=idx,
            )
        )

    await db.flush()
    return msg


# ---------------------------------------------------------------------------
# Cache Redis do histórico recente (contexto multi-turn para o LLM)
# ---------------------------------------------------------------------------

async def get_history_for_prompt(
    db: AsyncSession, redis: Redis, session_id: uuid.UUID
) -> list[dict[str, str]]:
    """
    Retorna as últimas N mensagens (role/content) para montar o contexto
    do prompt. Tenta o Redis primeiro; em cache miss, reconstrói do
    Postgres e regrava no Redis.
    """
    key = session_history_key(str(session_id))
    cached = await redis.get(key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            log.warning("Histórico corrompido no Redis para %s, reconstruindo do Postgres.", session_id)

    return await warm_redis_from_postgres(db, redis, session_id)


async def warm_redis_from_postgres(
    db: AsyncSession, redis: Redis, session_id: uuid.UUID
) -> list[dict[str, str]]:
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id, ChatMessage.status == "success")
        .order_by(ChatMessage.sequence_number.desc())
        .limit(REDIS_HISTORY_MAX_MESSAGES)
    )
    messages = list(reversed(result.scalars().all()))
    history = [{"role": m.role, "content": m.content} for m in messages]

    key = session_history_key(str(session_id))
    await redis.set(key, json.dumps(history), ex=REDIS_HISTORY_TTL_SECONDS)

    meta_key = session_meta_key(str(session_id))
    await redis.hset(
        meta_key,
        mapping={"last_synced_at": datetime.now(timezone.utc).isoformat()},
    )
    await redis.expire(meta_key, REDIS_HISTORY_TTL_SECONDS)

    return history


async def append_to_redis_history(
    redis: Redis, session_id: uuid.UUID, role: str, content: str
) -> None:
    """Atualização incremental e barata do cache — evita reconsultar o Postgres a cada turno."""
    key = session_history_key(str(session_id))
    cached = await redis.get(key)
    history: list[dict[str, str]] = json.loads(cached) if cached else []
    history.append({"role": role, "content": content})
    history = history[-REDIS_HISTORY_MAX_MESSAGES:]
    await redis.set(key, json.dumps(history), ex=REDIS_HISTORY_TTL_SECONDS)