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
from redis.asyncio import Redis

from app.db.qdrant import get_qdrant_client
from app.db.redis import get_redis_client
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
        "regex, sem chamada de LLM. Com `sample_mode=true` (para projetos-"
        "sample de mecânica como Lyra/City Sample/Vehicle Sample, não a "
        "Engine base), um Canal C adicional também varre e documenta "
        "arquivos .cpp, focando na IMPLEMENTAÇÃO da mecânica, e o filtro de "
        "relevância por palavra-chave do Canal A é desligado. O "
        "processamento roda em background — esta chamada retorna "
        "imediatamente (HTTP 202). Um manifesto de checkpoint no Redis "
        "garante que arquivos já indexados (hash inalterado) sejam "
        "ignorados em reingestões subsequentes."
    ),
    tags=["ingestion"],
)
async def process_folder(
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis_client),
) -> IngestAcceptedResponse:
    """
    - **folder_path**: caminho ABSOLUTO dentro do container `ue5-agent`
      (confira os volumes configurados no docker-compose.yml).
    - **content_type**: `source_code` (varre headers .h, e .cpp se
      `sample_mode=true`) ou `documentation` (varre arquivos .udn).
    - **ue_version**: opcional — sobrescreve a detecção automática via regex
      no caminho informado.
    - **project_source**: rótulo de proveniência gravado em cada chunk
      (ex: 'UE5_Engine', 'Lyra', 'CitySample') — permite segmentar a busca
      por origem no futuro sem reingerir.
    - **sample_mode**: ative para projetos-sample de mecânica (Lyra, City
      Sample, Vehicle Sample, projetos próprios) em vez da Engine base.
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

    # Prioriza engine_version, mas mantém compatibilidade com ue_version legado
    version_override = payload.engine_version or payload.ue_version

    background_tasks.add_task(
        process_folder_ingestion,
        folder_path=str(folder),
        content_type=payload.content_type,
        qdrant=qdrant,
        redis=redis,
        engine_version_override=version_override,
        project_source=payload.project_source,
        sample_mode=payload.sample_mode,
        domain_id=payload.domain_id,
    )

    log.info(
        "Ingestão agendada em background | pasta=%s tipo=%s project_source=%s sample_mode=%s",
        folder,
        payload.content_type,
        payload.project_source,
        payload.sample_mode,
    )

    return IngestAcceptedResponse(
        message=(
            f"Ingestão da pasta '{folder}' agendada com sucesso "
            f"(project_source='{payload.project_source}', sample_mode={payload.sample_mode}). "
            f"Acompanhe o progresso pelos logs da aplicação (nível INFO)."
        ),
        folder_path=str(folder),
        content_type=payload.content_type,
        project_source=payload.project_source,
        sample_mode=payload.sample_mode,
    )