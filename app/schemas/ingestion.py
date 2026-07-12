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
    project_source: str = Field(
        default="UE5_Engine",
        min_length=1,
        max_length=64,
        description=(
            "Rótulo de proveniência gravado em cada chunk indexado (payload "
            "do Qdrant, nunca no texto embedado). Identifica de qual projeto "
            "o conteúdo veio — ex: 'UE5_Engine' (padrão, para a pasta "
            "Source/ da própria Engine), 'Lyra', 'CitySample', "
            "'VehicleSample'. Permite segmentar a busca por origem no "
            "futuro (via filtro no Qdrant) sem precisar reingerir nada."
        ),
    )
    sample_mode: bool = Field(
        default=False,
        description=(
            "Ative para ingerir projetos-sample de mecânica (Lyra, City "
            "Sample, Vehicle Sample, Game Animation Sample, projetos "
            "próprios com jogo implementado) em vez da Engine base. "
            "Efeitos quando True, aplicáveis apenas a content_type="
            "'source_code': (1) desliga o filtro de relevância por "
            "palavra-chave, já que esses projetos usam convenções de nome "
            "próprias (ex: ALyraWeaponInstance) que não bateriam na "
            "allowlist calibrada para a Engine; (2) além dos headers .h, "
            "também varre e documenta arquivos .cpp — a implementação real "
            "das mecânicas vive lá, não só na declaração. Mantenha False "
            "para ingestão da Engine (Source/ da UE5): mais barato e mais "
            "estrito, evita ruído em um volume de arquivos ordens de "
            "magnitude maior."
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
    project_source: str = Field(
        description="Rótulo de proveniência que será gravado nos chunks desta ingestão.",
    )
    sample_mode: bool = Field(
        description="Confirma com qual configuração de filtro/canais esta ingestão foi disparada.",
    )
    accepted_at: datetime = Field(default_factory=_utcnow)