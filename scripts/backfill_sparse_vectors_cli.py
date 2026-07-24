"""
backfill_sparse_vectors_cli.py  (STANDALONE — sem dependências do pacote app.*)
=================================================================================
Só é ÚTIL DEPOIS de você ter um espaço 'keywords_sparse' habilitado na
coleção — ou seja, depois de:
  (a) migrar via migrate_to_hybrid_collection.py, OU
  (b) atualizar o Qdrant para >= v1.18 e rodar a app normalmente (o
      próprio startup já habilita o schema nesse caso).

Popula o vetor esparso em pontos que ainda não o têm (idempotente — pula
quem já tem). Ver docstring de migrate_to_hybrid_collection.py para a
explicação completa de por que este script é standalone (não depende do
pacote `app`, só de `qdrant-client`) e como rodá-lo (docker cp + exec, ou
direto do host via --host localhost).

USO:
    docker cp backfill_sparse_vectors_cli.py ue5-agent-api:/tmp/backfill.py
    docker compose exec ue5-agent python /tmp/backfill.py

    # ou do host (porta 6333 exposta no docker-compose.yml):
    python backfill_sparse_vectors_cli.py --host localhost

    # opções:
    python backfill_sparse_vectors_cli.py --host qdrant --port 6333 \\
        --collection ue5_docs_hybrid --batch-size 200
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import zlib

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_sparse_vectors_cli")

# --- Tokenização/hashing — cópia intencional de sparse_vector_service.py ---
SPARSE_VECTOR_NAME = "keywords_sparse"
_SPARSE_VECTOR_DIM = 2**24
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+"
)


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw_word in _NON_ALNUM_RE.split(text):
        if not raw_word:
            continue
        lowered = raw_word.lower()
        if len(lowered) > 1:
            tokens.append(lowered)
        sub_parts = _CAMEL_SPLIT_RE.findall(raw_word)
        if len(sub_parts) > 1:
            for part in sub_parts:
                part_lower = part.lower()
                if len(part_lower) > 1:
                    tokens.append(part_lower)
    return tokens


def build_sparse_vector(text: str) -> qdrant_models.SparseVector:
    if not text:
        return qdrant_models.SparseVector(indices=[], values=[])
    tokens = _tokenize(text)
    if not tokens:
        return qdrant_models.SparseVector(indices=[], values=[])
    term_frequency: dict[int, float] = {}
    for token in tokens:
        idx = zlib.crc32(token.encode("utf-8")) % _SPARSE_VECTOR_DIM
        term_frequency[idx] = term_frequency.get(idx, 0.0) + 1.0
    indices = list(term_frequency.keys())
    values = [term_frequency[i] for i in indices]
    return qdrant_models.SparseVector(indices=indices, values=values)


async def backfill(host: str, port: int, collection: str, batch_size: int) -> None:
    qdrant = AsyncQdrantClient(host=host, port=port, timeout=30.0)
    try:
        if not await qdrant.collection_exists(collection):
            raise RuntimeError(f"Coleção '{collection}' não existe em http://{host}:{port}.")

        info = await qdrant.get_collection(collection)
        if SPARSE_VECTOR_NAME not in (info.config.params.sparse_vectors or {}):
            raise RuntimeError(
                f"Coleção '{collection}' ainda não tem o espaço "
                f"'{SPARSE_VECTOR_NAME}' no schema. Rode "
                f"migrate_to_hybrid_collection.py primeiro (ou atualize o "
                f"Qdrant para >= v1.18)."
            )

        updated_count = 0
        scanned_count = 0
        next_offset = None

        while True:
            points, next_offset = await qdrant.scroll(
                collection_name=collection,
                limit=batch_size,
                offset=next_offset,
                with_payload=["title", "raw_content", "summary_markdown"],
                with_vectors=True,
            )
            if not points:
                break
            scanned_count += len(points)

            updates: list[qdrant_models.PointVectors] = []
            for point in points:
                existing_vectors = point.vector or {}
                if isinstance(existing_vectors, dict) and SPARSE_VECTOR_NAME in existing_vectors:
                    continue

                payload = point.payload or {}
                sparse_text = " ".join(
                    (
                        payload.get("title") or "",
                        payload.get("raw_content") or "",
                        payload.get("summary_markdown") or "",
                    )
                )
                sparse_vector = build_sparse_vector(sparse_text)
                updates.append(
                    qdrant_models.PointVectors(
                        id=point.id,
                        vector={SPARSE_VECTOR_NAME: sparse_vector},
                    )
                )

            if updates:
                await qdrant.update_vectors(collection_name=collection, points=updates)
                updated_count += len(updates)
                log.info(
                    "+%d pontos neste lote (escaneados=%d, atualizados_total=%d)",
                    len(updates), scanned_count, updated_count,
                )

            if next_offset is None:
                break

        log.info(
            "Backfill concluído | escaneados=%d atualizados=%d já_migrados=%d",
            scanned_count, updated_count, scanned_count - updated_count,
        )
    finally:
        await qdrant.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="qdrant")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--collection", default="ue5_docs")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    asyncio.run(backfill(args.host, args.port, args.collection, args.batch_size))