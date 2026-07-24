"""
app/schemas/sessions.py
========================
Schemas de resposta para app/api/v1/endpoints/sessions.py.
Reutiliza RetrievedChunk de app.schemas.chat onde os campos coincidem.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RetrievedChunkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    score: float
    title: str
    source_file: str
    qdrant_chunk_id: int = Field(serialization_alias="chunk_id")
    ue_version: str
    summary_markdown: str
    raw_content: str
    rank_position: int
    project: "ProjectRef | None" = None
    doc_type: "DocTypeRef | None" = None


class ProjectRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str


class DocTypeRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    status: str
    content: str
    error_detail: str | None
    model_used: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    sequence_number: int
    created_at: datetime
    retrieved_chunks: list[RetrievedChunkResponse] = []


class SessionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    default_model: str
    project_source_filter: str | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class SessionDetailResponse(SessionSummaryResponse):
    messages: list[MessageResponse] = []