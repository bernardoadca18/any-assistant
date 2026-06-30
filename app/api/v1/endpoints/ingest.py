"""
app/api/v1/endpoints/ingest.py
================================
Endpoint POST /v1/ingest/process-folder — dispara o pipeline de ETL
Code-to-Doc em background, usando as BackgroundTasks nativas do FastAPI.

O endpoint faz apenas uma validação síncrona barata (a pasta existe e é
um diretório?) e devolve HTTP 202 Accepted imediatamente. Todo o trabalho
pesado — leitura de arquivos, chamadas à LLM documentadora, geração de
embeddings e upsert no Qdrant — acontece de forma assíncrona depois que a
resposta HTTP já foi enviada, evitando timeout em pastas grandes (ex: a
árvore de Source/ inteira da engine).

Por que HTTP 202 em vez de 200: 202 Accepted é o código semanticamente
correto para "a requisição foi aceita para processamento, mas ainda não
foi concluída" — comunica corretamente ao cliente que não deve esperar o
resultado final nesta resposta.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from qdrant_client import AsyncQdrantClient

from app.db.qdrant import get_qdrant_client
from app.schemas.ingestion import IngestAcceptedResponse, IngestRequest
from app.services.ai.ingestion_service import process_folder_ingestion

log = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/process-folder",
    response_model=IngestAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Disparar ingestão ETL Code-to-Doc de uma pasta",
    description=(
        "Varre recursivamente a pasta informada (caminho DENTRO DO CONTAINER) "
        "e indexa seu conteúdo no Qdrant via pipeline Code-to-Doc de duplo "
        "canal: Canal A (.h C++) filtra headers de gameplay relevantes e usa "
        "uma LLM rápida via LiteLLM para gerar documentação Markdown "
        "educacional; Canal B (.udn) sanitiza a documentação oficial via "
        "regex, sem chamada de LLM. O processamento roda em background — "
        "esta chamada retorna imediatamente (HTTP 202)."
    ),
    tags=["ingestion"],
)
async def process_folder(
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
) -> IngestAcceptedResponse:
    """
    - **folder_path**: caminho ABSOLUTO dentro do container `ue5-agent`
      (confira os volumes configurados no docker-compose.yml).
    - **content_type**: `source_code` (varre headers .h) ou `documentation`
      (varre arquivos .udn).
    - **ue_version**: opcional — sobrescreve a detecção automática via regex
      no caminho informado.
    """
    folder = Path(payload.folder_path)

    # Validação síncrona e barata (apenas um stat() no filesystem) — falha
    # rápido com HTTP 400 se o caminho não existir, em vez de aceitar a
    # tarefa silenciosamente e o usuário só descobrir o erro nos logs do
    # background task minutos depois.
    if not folder.is_dir():
        log.warning("Tentativa de ingestão com pasta inexistente: %s", folder)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Diretório não encontrado ou inacessível dentro do container: "
                f"'{payload.folder_path}'. Verifique se o caminho está correto "
                f"e se o volume correspondente foi montado no serviço "
                f"'ue5-agent' do docker-compose.yml."
            ),
        )

    background_tasks.add_task(
        process_folder_ingestion,
        folder_path=str(folder),
        content_type=payload.content_type,
        qdrant=qdrant,
        ue_version_override=payload.ue_version,
    )

    log.info(
        "Ingestão agendada em background | pasta=%s tipo=%s",
        folder,
        payload.content_type,
    )

    return IngestAcceptedResponse(
        message=(
            f"Ingestão da pasta '{folder}' agendada com sucesso. "
            f"Acompanhe o progresso pelos logs da aplicação (nível INFO)."
        ),
        folder_path=str(folder),
        content_type=payload.content_type,
    )