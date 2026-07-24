"""
backfill_sparse_vectors_cli.py
===============================
Script standalone para disparar o backfill de vetores esparsos
('keywords_sparse') nos pontos LEGADOS da coleção Qdrant — ou seja, pontos
ingeridos ANTES da migração para busca híbrida (Dense + Sparse via RRF).

NÃO re-ingere nada: não chama a LLM documentadora, não recalcula embedding
denso, não toca em nenhum campo do payload. Só lê os campos de texto já
armazenados (title, raw_content, summary_markdown), gera o vetor esparso
localmente (CPU, sem chamada de rede) e grava apenas esse vetor via
`update_vectors`. Idempotente — seguro rodar múltiplas vezes.

INSTALAÇÃO:
    Copie este arquivo para dentro do projeto, por exemplo em:
        app/scripts/backfill_sparse_vectors.py
    (crie também app/scripts/__init__.py vazio, se o diretório não existir)

USO (dentro do container ue5-agent-api, via docker compose):

    # Se colocado como app/scripts/backfill_sparse_vectors.py:
    docker compose exec ue5-agent python -m app.scripts.backfill_sparse_vectors

    # Ou rodando este arquivo diretamente (ex: montado/copiado na raiz
    # do container, onde o pacote `app` já está no PYTHONPATH):
    docker compose exec ue5-agent python backfill_sparse_vectors_cli.py

    # Batch size customizado (padrão: 200):
    docker compose exec ue5-agent python backfill_sparse_vectors_cli.py 500

Não precisa parar o container nem interromper o tráfego normal de
/v1/chat — o backfill só lê e atualiza vetores, não interfere na busca
densa já em uso, e o retrieval híbrido já cai automaticamente para os
pontos migrados assim que eles forem atualizados (sem restart necessário).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from qdrant_client import AsyncQdrantClient

from app.core.config import settings
from app.services.ai.ingestion_service import (
    backfill_sparse_vectors,
    ensure_sparse_vectors_enabled,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_sparse_vectors_cli")


async def main(batch_size: int) -> None:
    log.info(
        "Conectando ao Qdrant em %s:%s (coleção=%s)...",
        settings.qdrant_host,
        settings.qdrant_port,
        settings.qdrant_collection,
    )
    qdrant = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        timeout=30.0,
    )
    try:
        # Garante que o schema está migrado antes do backfill — idempotente,
        # não faz nada se 'keywords_sparse' já estiver habilitado.
        await ensure_sparse_vectors_enabled(qdrant)

        total_updated = await backfill_sparse_vectors(qdrant, batch_size=batch_size)

        log.info(
            "Backfill concluído com sucesso | %d ponto(s) atualizado(s) "
            "com vetor esparso.",
            total_updated,
        )
    finally:
        await qdrant.close()


if __name__ == "__main__":
    batch_size_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    asyncio.run(main(batch_size=batch_size_arg))