"""
migrate_to_hybrid_collection.py
=================================
Migração de schema para busca híbrida em servidores Qdrant ANTERIORES à
v1.18 — onde adicionar um vetor NOMEADO NOVO a uma coleção que já existe,
sem recriá-la, não é suportado pelo servidor (ver
ingestion_service.ensure_sparse_vectors_enabled para a explicação completa
do porquê `update_collection` falha com 'Not existing vector name error'
nessas versões).

O QUE ESTE SCRIPT FAZ (e o que NÃO faz):
  1. Lê a config da coleção ORIGEM (distância, tamanho do vetor denso,
     on_disk_payload) via get_collection — para replicar exatamente o
     mesmo espaço denso na coleção nova.
  2. Cria uma coleção DESTINO (--target) já com os DOIS espaços de vetor
     definidos desde a criação: o denso (mesma config da origem, chave "")
     e o esparso 'keywords_sparse' (modifier=IDF).
  3. Copia TODOS os pontos da origem para o destino via scroll + upsert em
     lote, reaproveitando o vetor denso JÁ CALCULADO (sem chamar o
     LiteLLM/modelo de embedding de novo) e computando o vetor esparso
     LOCALMENTE (build_sparse_vector — CPU, sem chamada de rede) a partir
     de title + raw_content + summary_markdown já armazenados no payload.
  4. NÃO apaga a coleção origem. NÃO troca a coleção que a aplicação usa
     automaticamente — isso é uma decisão manual sua, feita DEPOIS de
     validar a coleção nova (ver instruções impressas ao final).

Ou seja: isto NÃO é um re-ingestão do zero. Nenhuma chamada à LLM
documentadora (Canal A) nem ao modelo de embedding acontece — é uma cópia
de pontos + geração local do vetor esparso.

USO:
    docker compose exec ue5-agent python -m app.scripts.migrate_to_hybrid_collection

    # Customizando nomes/batch:
    docker compose exec ue5-agent python -m app.scripts.migrate_to_hybrid_collection \\
        --source ue5_docs --target ue5_docs_hybrid --batch-size 200

    # Rodar de novo depois de interromper: idempotente na criação da
    # coleção destino (pula se já existir) e sobrescreve pontos já
    # copiados (upsert por id) — seguro reexecutar.

DEPOIS DE RODAR (passo manual):
    1. Confira o total de pontos na coleção destino (compare com a origem).
    2. Rode uma query de teste contra a coleção destino para validar o
       retrieval híbrido.
    3. Aponte a aplicação para a coleção nova: troque
       QDRANT_COLLECTION=<target> no seu .env / docker-compose.yml.
    4. Reinicie o container ue5-agent.
    5. Só depois de validar em produção, remova a coleção antiga (origem)
       manualmente, se quiser liberar o espaço em disco.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

from app.core.config import settings
from app.services.ai.sparse_vector_service import SPARSE_VECTOR_NAME, build_sparse_vector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("migrate_to_hybrid_collection")


async def migrate(source: str, target: str, batch_size: int) -> None:
    qdrant = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        timeout=60.0,
    )
    try:
        if not await qdrant.collection_exists(source):
            raise RuntimeError(f"Coleção de origem '{source}' não existe em {settings.qdrant_url}.")

        if await qdrant.collection_exists(target):
            log.info(
                "Coleção destino '%s' já existe — pulando criação de schema, "
                "só copiando/atualizando pontos (upsert por id, idempotente).",
                target,
            )
        else:
            source_info = await qdrant.get_collection(source)
            # Coleção legada com vetor denso NÃO-nomeado: params.vectors é um
            # VectorParams único (não um dict) — reaproveitamos exatamente a
            # mesma config (distance, size, hnsw) para a coleção nova.
            dense_vectors_config = source_info.config.params.vectors

            log.info(
                "Criando coleção destino '%s' | vetor denso replicado de "
                "'%s' + vetor esparso '%s' (modifier=IDF)...",
                target,
                source,
                SPARSE_VECTOR_NAME,
            )
            await qdrant.create_collection(
                collection_name=target,
                vectors_config=dense_vectors_config,
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: qdrant_models.SparseVectorParams(
                        modifier=qdrant_models.Modifier.IDF,
                    ),
                },
                on_disk_payload=source_info.config.params.on_disk_payload,
            )

        copied = 0
        next_offset = None

        while True:
            points, next_offset = await qdrant.scroll(
                collection_name=source,
                limit=batch_size,
                offset=next_offset,
                with_payload=True,
                with_vectors=True,
            )
            if not points:
                break

            new_points: list[qdrant_models.PointStruct] = []
            for point in points:
                vectors = point.vector
                # Coleção origem legada: vetor denso não-nomeado. Dependendo
                # da versão/config do client, `point.vector` pode vir como
                # lista direta OU como dict {"": [...]} — cobrimos os dois.
                dense_vector = vectors.get("") if isinstance(vectors, dict) else vectors

                payload = point.payload or {}
                sparse_text = " ".join(
                    (
                        payload.get("title") or "",
                        payload.get("raw_content") or "",
                        payload.get("summary_markdown") or "",
                    )
                )
                sparse_vector = build_sparse_vector(sparse_text)

                new_points.append(
                    qdrant_models.PointStruct(
                        id=point.id,
                        vector={"": dense_vector, SPARSE_VECTOR_NAME: sparse_vector},
                        payload=payload,
                    )
                )

            await qdrant.upsert(collection_name=target, points=new_points)
            copied += len(new_points)
            log.info("Copiados %d pontos até agora...", copied)

            if next_offset is None:
                break

        log.info(
            "=" * 60 + "\n"
            "Migração concluída | %d ponto(s) copiado(s) de '%s' para '%s'.\n"
            "\n"
            "PRÓXIMOS PASSOS (manuais):\n"
            "  1. Valide o total de pontos e faça uma busca de teste contra "
            "'%s'.\n"
            "  2. Troque QDRANT_COLLECTION=%s no seu .env / "
            "docker-compose.yml.\n"
            "  3. Reinicie o container ue5-agent.\n"
            "  4. Só depois de validar, remova a coleção '%s' manualmente "
            "se quiser liberar espaço.\n" + "=" * 60,
            copied,
            source,
            target,
            target,
            target,
            source,
        )
    finally:
        await qdrant.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=settings.qdrant_collection, help="Coleção de origem (padrão: settings.qdrant_collection)")
    parser.add_argument("--target", default=f"{settings.qdrant_collection}_hybrid", help="Coleção destino a criar")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    asyncio.run(migrate(args.source, args.target, args.batch_size))