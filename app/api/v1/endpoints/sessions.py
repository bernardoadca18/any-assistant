"""
app/api/v1/endpoints/sessions.py
=================================
Endpoints de histórico — é o que o frontend chama ao montar a página
para popular a sidebar de conversas e restaurar mensagens, em vez de
depender só do localStorage.

    GET    /v1/sessions              -> lista de sessões (resumo)
    GET    /v1/sessions/{id}         -> sessão + todas as mensagens + chunks
    DELETE /v1/sessions/{id}         -> apaga sessão (cascade cuida do resto)

Não mexe no histórico Redis de contexto do LLM (isso é responsabilidade de
app/services/ai/session_service.py, usado internamente por
run_rag_pipeline/run_rag_pipeline_stream) — apagar uma sessão aqui remove
só a persistência durável no Postgres. O cache Redis daquela sessão, se
existir, expira sozinho pelo TTL normal (settings.redis_session_ttl).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentTenant
from app.db.postgres import get_db_session
from app.schemas.sessions import SessionDetailResponse, SessionSummaryResponse
from app.services import session_service

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sessions", response_model=list[SessionSummaryResponse], tags=["sessions"])
async def list_sessions(
    auth: CurrentTenant, db: AsyncSession = Depends(get_db_session)
) -> list[SessionSummaryResponse]:
    """Lista sessões do tenant autenticado."""
    sessions = await session_service.list_sessions(db, tenant_id=auth.tenant_id)
    return [SessionSummaryResponse.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse, tags=["sessions"])
async def get_session(
    auth: CurrentTenant,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> SessionDetailResponse:
    """Obtém detalhes de uma sessão do tenant autenticado."""
    session = await session_service.get_session_with_messages(db, session_id, tenant_id=auth.tenant_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sessão não encontrada.")
    return SessionDetailResponse.model_validate(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sessions"])
async def remove_session(
    auth: CurrentTenant,
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
):
    """Remove sessão do tenant autenticado."""
    deleted = await session_service.delete_session(db, session_id, tenant_id=auth.tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sessão não encontrada.")