"""
app/schemas/ingestion.py
==========================
ATENÇÃO: mesmo aviso de app/schemas/chat.py — este arquivo não foi enviado;
reconstrução best-effort a partir dos campos usados em ingest.py
(payload.folder_path, payload.content_type, payload.ue_version,
payload.project_source, payload.sample_mode + os mesmos campos no response).
Único acréscimo intencional: `domain_id`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    folder_path: str
    content_type: Literal["source_code", "documentation"]
    ue_version: str | None = None  # NOTA: nome legado — ver PATCH_NOTES.md (vira "engine_version")
    project_source: str
    sample_mode: bool = False

    # NOVO — multi-domain. Se None, process_folder_ingestion() cai para
    # settings.active_domain (ver ingest.py e roadmap de ingestion_service.py
    # em PATCH_NOTES.md).
    domain_id: str | None = Field(
        default=None,
        description="domain_id explícito (ex: 'godot_4', 'ue5'). Se omitido, usa settings.active_domain.",
    )


class IngestAcceptedResponse(BaseModel):
    message: str
    folder_path: str
    content_type: str
    project_source: str
    sample_mode: bool
    domain_id: str = "godot_4"
