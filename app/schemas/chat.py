"""
app/schemas/chat.py
====================
ATENÇÃO: este arquivo NÃO foi enviado por você nas rodadas anteriores — o
conteúdo abaixo é uma RECONSTRUÇÃO best-effort, inferida apenas pelos campos
efetivamente usados em rag_service.py e chat.py (request.session_id,
request.model, request.top_k, request.stream, request.message,
request.project_source_filter, result.answer, result.model_used,
result.prompt_tokens, result.completion_tokens, result.retrieved_chunks,
RetrievedChunk.{score,summary_markdown,title,description,source_file,
chunk_id,ue_version,doc_type,project_source,raw_content}).

NÃO substitua seu arquivo real por este sem antes comparar campo a campo —
validadores customizados, valores default específicos, docstrings e
qualquer campo que meu conjunto de arquivos não expôs (ex: campos extras já
usados no frontend TypeScript) foram perdidos nesta reconstrução. O único
acréscimo intencional é `domain_id`.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str
    message: str
    model: str = "ollama/qwen2.5-coder:7b"
    top_k: int = Field(default=5, ge=1, le=20)
    stream: bool = False
    project_source_filter: str | None = None

    # NOVO — multi-domain. Quando None, o endpoint resolve via
    # domain_registry.resolve(explicit_domain_id=None, header_domain_id=...,
    # default_domain_id=settings.active_domain) — ver chat.py::chat().
    domain_id: str | None = Field(
        default=None,
        description=(
            "domain_id explícito (ex: 'godot_4', 'ue5'). Se omitido, o "
            "servidor resolve via header X-Domain-ID ou settings.active_domain."
        ),
    )


class RetrievedChunk(BaseModel):
    score: float
    summary_markdown: str = ""
    title: str = "Sem título"
    description: str = ""
    source_file: str = ""
    chunk_id: int = 0
    ue_version: str = ""  # NOTA: nome de campo legado — ver PATCH_NOTES.md
    doc_type: str = ""
    project_source: str = ""
    raw_content: str = ""


class ChatResponse(BaseModel):
    answer: str
    model_used: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieved_chunks: list[RetrievedChunk] = Field(default_factory=list)


class DependencyStatus(BaseModel):
    """Status of a single dependency in health check."""
    status: str  # "ok", "degraded", "error"
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """Health check response with dependency statuses."""
    status: str  # "ok", "degraded", "error"
    version: str
    environment: str
    dependencies: dict[str, DependencyStatus]
