"""
app/schemas/ingestion.py
=========================
Schemas Pydantic para o endpoint POST /v1/ingest/process-folder.

Segue o mesmo padrão de app/schemas/chat.py: payload de entrada validado
e uma resposta de "aceite imediato", já que o processamento real acontece
de forma assíncrona via BackgroundTasks (ver app/services/ai/ingestion_service.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Retorna o datetime atual em UTC com timezone-aware (Python 3.12 safe)."""
    return datetime.now(timezone.utc)


# Canal A ("source_code", varre .h) ou Canal B ("documentation", varre .udn).
ContentType = Literal["source_code", "documentation"]


# ---------------------------------------------------------------------------
# Payload de entrada (Request)
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    """
    Corpo da requisição POST /v1/ingest/process-folder.

    ATENÇÃO: `folder_path` deve ser um caminho ACESSÍVEL DENTRO DO
    CONTAINER `ue5-agent` — não um caminho do host Windows/WSL. Garanta
    que a pasta de origem esteja montada como volume no docker-compose.yml
    (veja as instruções de configuração que acompanham esta entrega).
    """

    folder_path: str = Field(
        ...,
        min_length=1,
        description=(
            "Caminho absoluto, DENTRO DO CONTAINER, da pasta a ser varrida "
            "recursivamente (ex: '/data/ue5_source/Engine/Source/Runtime')."
        ),
    )
    content_type: ContentType = Field(
        ...,
        description=(
            "'source_code' para varrer headers .h C++ via Canal A (LLM "
            "documentadora), ou 'documentation' para varrer arquivos .udn "
            "via Canal B (sanitização regex)."
        ),
    )
    ue_version: str | None = Field(
        default=None,
        description=(
            "Override manual da versão alvo da UE5 (ex: '5.8'). Se omitido, "
            "a versão é extraída automaticamente a partir do caminho informado, "
            "com fallback para o valor configurado em INGESTION_DEFAULT_UE_VERSION."
        ),
    )


# ---------------------------------------------------------------------------
# Payload de saída (Response)
# ---------------------------------------------------------------------------

class IngestAcceptedResponse(BaseModel):
    """
    Resposta imediata de aceite (HTTP 202). O processamento real ocorre
    em background — acompanhe o progresso pelos logs da aplicação.
    """

    status: Literal["accepted"] = "accepted"
    message: str
    task_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Identificador correlacionável nos logs da aplicação para esta execução.",
    )
    folder_path: str
    content_type: ContentType
    accepted_at: datetime = Field(default_factory=_utcnow)