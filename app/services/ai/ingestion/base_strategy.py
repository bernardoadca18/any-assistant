"""
app/services/ai/ingestion/base_strategy.py
=============================================
Contrato mínimo que um "canal de ingestão" de um domínio deve implementar.
Não tenta reproduzir toda a orquestração já existente em
app/services/ai/ingestion_service.py (checkpoint no Redis, batch de
embeddings, upsert no Qdrant, quarentena de falhas consecutivas) — essa
orquestração é genérica o suficiente para ser reaproveitada como está.
O que MUDA por domínio é só: quais arquivos processar e como transformar
cada um em uma lista de `ChunkCandidate` prontos para embedding.

Ver PATCH_NOTES.md para o ponto exato de integração dentro do loop
principal de `process_folder_ingestion`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ChunkCandidate:
    """Saída de um canal de ingestão — unidade mínima antes do embedding.
    Mapeia 1:1 para os campos já consumidos por
    ingestion_service._build_chunks_with_metadata / _flush_batch."""

    title: str
    summary_markdown: str       # texto usado para gerar o embedding (Análise Semântica)
    raw_content: str            # conteúdo original bruto (Referência Direta, nunca embedado)
    doc_type: str                # deve existir em BaseDomainProfile.doc_types
    source_file: str
    engine_version: str = ""
    extra_metadata: dict[str, Any] = field(default_factory=dict)


class IngestionStrategy(Protocol):
    """Um canal de ingestão por domínio. `process_file` roda por-arquivo;
    a extração real (parsing, chamada de LLM documentadora se aplicável)
    deve usar `asyncio.to_thread` para qualquer parsing CPU-bound ou I/O
    síncrono, para não bloquear o event loop do FastAPI (spec seção 5)."""

    domain_id: str
    supported_extensions: tuple[str, ...]

    async def discover_files(self, root: Path) -> list[Path]:
        ...

    async def process_file(self, path: Path, *, engine_version_override: str | None) -> list[ChunkCandidate]:
        ...
