"""
app/schemas/chat.py
===================
Schemas Pydantic para os endpoints de chat e RAG.

CORREÇÃO: substituído datetime.utcnow() (deprecated Python 3.12) por
datetime.now(timezone.utc) com aware datetime.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Retorna o datetime atual em UTC com timezone-aware (Python 3.12 safe)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Payloads de entrada (Request)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """Mensagem individual no histórico de conversa."""
    role: Literal["user", "assistant", "system"]
    content: str = Field(..., min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    """
    Corpo da requisição POST /v1/chat.
    `session_id` permite manter histórico entre requisições.
    Se omitido, uma sessão efêmera é criada automaticamente.
    """
    message: str = Field(..., min_length=1, max_length=8_000, description="Pergunta do usuário")
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="ID de sessão para manutenção de contexto. Gere um UUID v4 e reutilize.",
    )
    model: str = Field(
        default="ollama/qwen2.5-coder:7b",
        description="Modelo LiteLLM a usar (ex: 'ollama/qwen2.5-coder:7b', 'gpt-4o')",
    )
    top_k: int = Field(default=5, ge=1, le=20, description="Chunks recuperados do Qdrant")
    stream: bool = Field(default=False, description="Retornar resposta como SSE stream")
    project_source_filter: str | None = Field(
        default=None,
        description=(
            "Filtra a busca vetorial pelo campo 'project_source' gravado no "
            "payload do Qdrant durante a ingestão (ex: 'engine' para código "
            "nativo da Unreal Engine, ou o nome de um projeto de amostra como "
            "'Lyra'). None (padrão) não aplica nenhum filtro — busca em todo "
            "o corpus, misturando todas as procedências."
        ),
    )


# ---------------------------------------------------------------------------
# Payloads de saída (Response)
# ---------------------------------------------------------------------------

class RetrievedChunk(BaseModel):
    """
    Chunk de documentação recuperado do Qdrant.
    Campos alinhados com o payload do ue5_rag_ingestor.py.
    """
    score: float = Field(..., description="Score de similaridade coseno [0, 1]")
    text: str
    title: str
    description: str = ""
    source_file: str
    chunk_id: int
    ue_version: str
    doc_type: str
    project_source: str = Field(
        default="",
        description=(
            "Procedência do chunk, gravada no payload do Qdrant durante a "
            "ingestão — distingue código-fonte nativo da Engine de projetos "
            "de amostra (ex: 'Lyra'). Default '' garante compatibilidade "
            "retroativa com pontos antigos do Qdrant que não possuem esse "
            "campo no payload."
        ),
    )
    raw_content: str = Field(
        default="",
        description=(
            "HYBRID RAG — Conteúdo original bruto (código C++ limpo no "
            "Canal A, ou Markdown .udn limpo no Canal B) armazenado no "
            "payload enriquecido do Qdrant. Usado por rag_service.py como "
            "'Referência Direta' — verdade soberana para assinaturas, "
            "tipos e nomes exatos. Default '' garante compatibilidade "
            "retroativa com pontos antigos do Qdrant, ingeridos antes "
            "desta mudança, que não possuem esse campo no payload."
        ),
    )


class ChatResponse(BaseModel):
    """Resposta completa do endpoint de chat."""
    session_id: str
    answer: str
    model_used: str
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list,
        description="Chunks da documentação utilizados para gerar a resposta",
    )
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class DependencyStatus(BaseModel):
    """Status de uma dependência individual."""
    status: Literal["ok", "degraded", "error"]
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """Resposta do endpoint GET /health."""
    status: Literal["ok", "degraded", "error"]
    version: str
    environment: str
    dependencies: dict[str, DependencyStatus]
    timestamp: datetime = Field(default_factory=_utcnow)