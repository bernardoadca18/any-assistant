"""
migrate_to_hybrid_collection.py  (STANDALONE — sem dependências do pacote app.*)
==================================================================================
Migração de schema para busca híbrida em servidores Qdrant ANTERIORES à
v1.18 — onde adicionar um vetor NOMEADO NOVO a uma coleção que já existe,
sem recriá-la, não é suportado pelo servidor ("Wrong input: Not existing
vector name error" — ver ingestion_service.ensure_sparse_vectors_enabled
para a explicação completa).

POR QUE ESTE SCRIPT É STANDALONE (não importa nada de `app.*`):
    O Dockerfile do projeto só copia a pasta `app/` para dentro da imagem
    (`COPY --chown=appuser:appuser app/ ./app/`) — qualquer script fora
    dela (ex: em `/scripts` na raiz do repo) simplesmente não existe
    dentro do container, e tentar rodá-lo como `-m app.scripts.algo` ou
    `-m scripts.algo` sempre vai dar ModuleNotFoundError, não importa o
    nome do módulo. Em vez de mexer no Dockerfile/volumes só por causa de
    um script de manutenção pontual, este arquivo NÃO depende de nada do
    pacote `app` — só de `qdrant-client` (já instalado no venv do
    container, faz parte do requirements.txt do projeto). A lógica de
    tokenização/hashing do vetor esparso está DUPLICADA aqui de propósito
    (copiada de app/services/ai/sparse_vector_service.py) — são ~30 linhas
    puras, sem risco real de desalinhar, e o ganho é poder rodar este
    script de QUALQUER lugar sem se preocupar com PYTHONPATH/pacotes.

COMO RODAR — duas opções:

  OPÇÃO 1 (recomendada, dentro do container, sem precisar ser um módulo):
    docker cp migrate_to_hybrid_collection.py ue5-agent-api:/tmp/migrate.py
    docker compose exec ue5-agent python /tmp/migrate.py

  OPÇÃO 2 (direto do host, fora do container — funciona porque o
  docker-compose.yml expõe a porta do Qdrant: "6333:6333"):
    # de dentro do seu venv local (o mesmo que já tem qdrant-client, ou
    # rode `pip install qdrant-client --break-system-packages` /
    # `.venv/bin/pip install qdrant-client` primeiro se não tiver)
    python migrate_to_hybrid_collection.py --host localhost --port 6333

Em ambos os casos, argumentos customizáveis:
    python migrate_to_hybrid_collection.py \\
        --host qdrant --port 6333 \\
        --source ue5_docs --target ue5_docs_hybrid \\
        --batch-size 200

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
     LOCALMENTE (CPU, sem chamada de rede) a partir de title +
     raw_content + summary_markdown já armazenados no payload.
  4. NÃO apaga a coleção origem. NÃO troca a coleção que a aplicação usa
     automaticamente — isso é uma decisão manual sua, feita DEPOIS de
     validar a coleção nova (ver instruções impressas ao final).

Ou seja: isto NÃO é um re-ingestão do zero. Nenhuma chamada à LLM
documentadora (Canal A) nem ao modelo de embedding acontece — é uma cópia
de pontos + geração local do vetor esparso.

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
import re
import zlib

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("migrate_to_hybrid_collection")


# =============================================================================
# TOKENIZAÇÃO/HASHING DO VETOR ESPARSO — cópia intencional e mínima de
# app/services/ai/sparse_vector_service.py (ver docstring do módulo acima
# para o porquê da duplicação). Mantenha as duas em sincronia se um dia
# mudar a estratégia de tokenização.
# =============================================================================

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


# =============================================================================
# MIGRAÇÃO
# =============================================================================

async def migrate(host: str, port: int, source: str, target: str, batch_size: int) -> None:
    qdrant = AsyncQdrantClient(host=host, port=port, timeout=60.0)
    try:
        if not await qdrant.collection_exists(source):
            raise RuntimeError(f"Coleção de origem '{source}' não existe em http://{host}:{port}.")

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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="qdrant", help="Host do Qdrant (padrão: 'qdrant' — hostname interno do Docker; use 'localhost' se rodar do seu host, fora do container)")
    parser.add_argument("--port", type=int, default=6333)
    parser.add_argument("--source", default="ue5_docs")
    parser.add_argument("--target", default="ue5_docs_hybrid")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    asyncio.run(migrate(args.host, args.port, args.source, args.target, args.batch_size))