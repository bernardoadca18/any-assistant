"""
ue5_rag_ingestor.py
===================
Pipeline de Ingestão RAG — Documentação Unreal Engine 5.8 (formato .INT.udn)

Decisões Arquiteturais
----------------------
1. **Scanning seletivo**: `pathlib.Path.rglob` com filtro explícito garante que apenas
   arquivos `.INT.udn` sejam processados, evitando qualquer lixo de outros idiomas ou
   arquivos `.tps` de licença.

2. **Parsing de cabeçalho por estado**: lemos linha a linha até encontrar a primeira
   linha em branco, extraindo chaves conhecidas (Title, Description). Isso é robusto
   frente a variações de espaçamento sem depender de um parser de Markdown completo.

3. **Limpeza via Regex compiladas**: expressões pré-compiladas reduzem overhead em
   loops de milhares de arquivos. Removemos apenas as tags proprietárias UDN, preservando
   o texto semântico útil para o embedding.

4. **Chunking configurável**: `RecursiveCharacterTextSplitter` respeita quebras naturais
   de parágrafo/sentença antes de cortar no limite de caracteres, produzindo chunks mais
   coesos que um corte cego por token count.

5. **Batch upsert**: acumulamos pontos em lote (BATCH_SIZE) antes de enviar ao Qdrant,
   minimizando round-trips de rede e reduzindo a pressão sobre o Ollama.

6. **Embeddings via Ollama SDK**: chamamos `ollama.embeddings()` diretamente — sem a
   camada de abstração do LangChain — para controle fino sobre erros e latência.

7. **Idempotência**: `recreate_collection=False` (via get-or-create) permite reexecutar
   o script sem apagar dados já ingeridos; pontos com mesmo ID são atualizados via upsert.

Uso
---
    python ue5_rag_ingestor.py --docs-dir /caminho/para/UDN

Dependências (instalar no .venv):
    pip install qdrant-client langchain-text-splitters ollama
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import ollama
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

# ---------------------------------------------------------------------------
# Configuração central — altere aqui sem tocar na lógica do pipeline
# ---------------------------------------------------------------------------

QDRANT_URL: str = "http://localhost:6333"
COLLECTION_NAME: str = "ue5_docs"
EMBEDDING_MODEL: str = "mxbai-embed-large"
EMBEDDING_DIM: int = 1024

CHUNK_SIZE: int = 1200       # caracteres por chunk
CHUNK_OVERLAP: int = 150     # sobreposição entre chunks consecutivos
BATCH_SIZE: int = 64         # pontos enviados ao Qdrant por request

UE_VERSION: str = "5.8"
DOC_TYPE: str = "UDN Manual"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex pré-compiladas para limpeza de sintaxe UDN
# ---------------------------------------------------------------------------

# Remove blocos de marcação tipo [EXCERPT:Nome] ... [/EXCERPT]
_RE_EXCERPT_BLOCK = re.compile(
    r"\[EXCERPT:[^\]]*\].*?\[/EXCERPT\]", re.DOTALL | re.IGNORECASE
)
# Remove tags de abertura/fechamento isoladas que sobrem
_RE_TAG_INLINE = re.compile(
    r"\[/?(?:EXCERPT|VAR|REGION|OBJECT|PARAM|INCLUDE)[^\]]*\]",
    re.IGNORECASE,
)
# Remove linhas de variável: [VAR:chave] valor [/VAR]
_RE_VAR_BLOCK = re.compile(
    r"\[VAR:[^\]]*\].*?\[/VAR\]", re.DOTALL | re.IGNORECASE
)
# Colapsa espaços em branco excessivos
_RE_BLANK_LINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

@dataclass
class UDNDocument:
    """Representa um arquivo .INT.udn parseado e limpo."""
    source_file: str
    title: str
    description: str
    body: str


@dataclass
class QdrantPoint:
    """Ponto pronto para upsert no Qdrant."""
    id: str
    vector: list[float]
    payload: dict


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------

def iter_udn_files(root: Path) -> Generator[Path, None, None]:
    """
    Varre `root` recursivamente e gera apenas arquivos `.INT.udn`.
    Arquivos `.tps` e outras variantes de idioma são ignorados silenciosamente.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Aceita estritamente sufixo .INT.udn (case-insensitive para robustez)
        if path.name.upper().endswith(".INT.UDN"):
            yield path


def parse_udn_file(path: Path) -> UDNDocument | None:
    """
    Lê e parseia um arquivo .INT.udn.

    Retorna `None` se o arquivo não puder ser lido ou estiver vazio.
    O parâmetro `errors='replace'` evita crash em caracteres inválidos raros.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Falha ao ler '%s': %s", path, exc)
        return None

    if not raw.strip():
        log.debug("Arquivo vazio ignorado: %s", path)
        return None

    lines = raw.splitlines()
    title = ""
    description = ""
    body_start = 0

    # O cabeçalho UDN termina na primeira linha em branco
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            body_start = i + 1
            break
        if stripped.lower().startswith("title:"):
            title = stripped[len("title:"):].strip()
        elif stripped.lower().startswith("description:"):
            description = stripped[len("description:"):].strip()

    body_raw = "\n".join(lines[body_start:])
    body_clean = clean_udn_syntax(body_raw)

    if not body_clean.strip():
        log.debug("Corpo vazio após limpeza: %s", path)
        return None

    return UDNDocument(
        source_file=str(path),
        title=title or path.stem,
        description=description,
        body=body_clean,
    )


def clean_udn_syntax(text: str) -> str:
    """
    Remove marcações proprietárias UDN para reduzir ruído nos embeddings.

    Ordem importa: blocos completos primeiro, depois tags órfãs.
    """
    text = _RE_EXCERPT_BLOCK.sub("", text)
    text = _RE_VAR_BLOCK.sub("", text)
    text = _RE_TAG_INLINE.sub("", text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def split_document(doc: UDNDocument, splitter: RecursiveCharacterTextSplitter) -> list[str]:
    """Divide o corpo do documento em chunks configurados."""
    return splitter.split_text(doc.body)


def get_embedding(text: str) -> list[float]:
    """
    Gera embedding via Ollama para um único texto.

    Lança `RuntimeError` se a resposta do Ollama for inválida,
    permitindo tratamento no nível superior.
    """
    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        vector = response.get("embedding") or response.get("embeddings")
        if not vector:
            raise RuntimeError(f"Ollama retornou resposta sem embedding: {response}")
        # Suporte a ambos os formatos de resposta (lista plana ou lista de listas)
        if isinstance(vector[0], list):
            vector = vector[0]
        return vector
    except Exception as exc:
        raise RuntimeError(f"Erro ao chamar Ollama: {exc}") from exc


# ---------------------------------------------------------------------------
# Inicialização do Qdrant
# ---------------------------------------------------------------------------

def ensure_collection(client: QdrantClient) -> None:
    """
    Cria a coleção se não existir e garante o índice de keyword no campo `doc_type`.

    Usar `get_collection` + captura de exceção é mais seguro do que
    `recreate_collection`, pois preserva dados em reexecuções.
    """
    existing = {c.name for c in client.get_collections().collections}

    if COLLECTION_NAME not in existing:
        log.info("Criando coleção '%s' no Qdrant...", COLLECTION_NAME)
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=qdrant_models.VectorParams(
                size=EMBEDDING_DIM,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        log.info("Coleção criada com sucesso.")
    else:
        log.info("Coleção '%s' já existe. Continuando.", COLLECTION_NAME)

    # Índice keyword no campo doc_type para filtragens eficientes em queries futuras
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="doc_type",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    log.info("Índice keyword em 'doc_type' garantido.")


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def build_points(
    doc: UDNDocument,
    chunks: list[str],
) -> list[QdrantPoint]:
    """
    Gera embeddings e monta os objetos `QdrantPoint` para cada chunk do documento.

    Chunks com falha de embedding são descartados com aviso — o restante do
    documento continua sendo processado normalmente.
    """
    points: list[QdrantPoint] = []

    for idx, chunk in enumerate(chunks):
        try:
            vector = get_embedding(chunk)
        except RuntimeError as exc:
            log.warning(
                "  [SKIP] Chunk %d de '%s' descartado por erro de embedding: %s",
                idx,
                doc.source_file,
                exc,
            )
            continue

        points.append(
            QdrantPoint(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "chunk_id": idx,
                    "ue_version": UE_VERSION,
                    "doc_type": DOC_TYPE,
                    "title": doc.title,
                    "description": doc.description,
                    "source_file": doc.source_file,
                },
            )
        )

    return points


def flush_batch(client: QdrantClient, batch: list[QdrantPoint]) -> None:
    """Envia um lote de pontos ao Qdrant via upsert."""
    if not batch:
        return
    try:
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                qdrant_models.PointStruct(
                    id=p.id,
                    vector=p.vector,
                    payload=p.payload,
                )
                for p in batch
            ],
        )
    except Exception as exc:
        log.error("Falha no upsert de lote com %d pontos: %s", len(batch), exc)
        raise


def run_ingestion(docs_dir: Path) -> None:
    """Orquestra toda a pipeline de ingestão."""

    # Clientes
    log.info("Conectando ao Qdrant em %s...", QDRANT_URL)
    try:
        qdrant = QdrantClient(url=QDRANT_URL)
    except Exception as exc:
        log.critical("Não foi possível conectar ao Qdrant: %s", exc)
        sys.exit(1)

    ensure_collection(qdrant)

    # Text splitter
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    # Contadores para resumo final
    stats = {
        "files_found": 0,
        "files_processed": 0,
        "files_skipped": 0,
        "total_chunks": 0,
        "total_points": 0,
    }

    pending_batch: list[QdrantPoint] = []

    def flush_if_needed(force: bool = False) -> None:
        nonlocal pending_batch
        if len(pending_batch) >= BATCH_SIZE or (force and pending_batch):
            flush_batch(qdrant, pending_batch)
            stats["total_points"] += len(pending_batch)
            pending_batch = []

    # Loop principal
    for udn_path in iter_udn_files(docs_dir):
        stats["files_found"] += 1

        doc = parse_udn_file(udn_path)
        if doc is None:
            stats["files_skipped"] += 1
            continue

        chunks = split_document(doc, splitter)
        if not chunks:
            log.debug("Nenhum chunk gerado para: %s", udn_path.name)
            stats["files_skipped"] += 1
            continue

        log.info(
            "Processando '%s': %d chunk(s) gerados...",
            udn_path.name,
            len(chunks),
        )
        stats["files_processed"] += 1
        stats["total_chunks"] += len(chunks)

        points = build_points(doc, chunks)
        pending_batch.extend(points)
        flush_if_needed()

    # Flush do lote final
    flush_if_needed(force=True)

    # Resumo
    log.info("=" * 60)
    log.info("INGESTÃO CONCLUÍDA")
    log.info("  Arquivos encontrados : %d", stats["files_found"])
    log.info("  Arquivos processados : %d", stats["files_processed"])
    log.info("  Arquivos ignorados   : %d", stats["files_skipped"])
    log.info("  Chunks gerados       : %d", stats["total_chunks"])
    log.info("  Pontos no Qdrant     : %d", stats["total_points"])
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Função de teste / validação semântica
# ---------------------------------------------------------------------------

def test_search(query: str, top_k: int = 5) -> None:
    """
    Realiza uma busca semântica simples para validar a ingestão.

    Exemplo de uso:
        python ue5_rag_ingestor.py --test "How to set up Lumen in UE5?"
    """
    log.info("Executando busca de validação: '%s'", query)

    try:
        qdrant = QdrantClient(url=QDRANT_URL)
        vector = get_embedding(query)
    except Exception as exc:
        log.error("Erro durante a busca: %s", exc)
        return

    results = qdrant.search(
        collection_name=COLLECTION_NAME,
        query_vector=vector,
        limit=top_k,
        with_payload=True,
    )

    if not results:
        log.warning("Nenhum resultado encontrado. Verifique se a ingestão foi executada.")
        return

    print("\n" + "=" * 60)
    print(f"TOP {top_k} RESULTADOS PARA: \"{query}\"")
    print("=" * 60)
    for rank, hit in enumerate(results, start=1):
        payload = hit.payload or {}
        print(f"\n[{rank}] Score: {hit.score:.4f}")
        print(f"    Título      : {payload.get('title', 'N/A')}")
        print(f"    Chunk ID    : {payload.get('chunk_id', 'N/A')}")
        print(f"    Arquivo     : {Path(payload.get('source_file', '')).name}")
        print(f"    Trecho      : {payload.get('text', '')[:200].strip()}...")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline de ingestão RAG para documentação UE5 (.INT.udn) → Qdrant."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--docs-dir",
        type=Path,
        metavar="DIRETÓRIO",
        help="Caminho raiz da documentação UDN a ser varrida recursivamente.",
    )
    group.add_argument(
        "--test",
        type=str,
        metavar="QUERY",
        help="Realiza uma busca semântica de validação em vez de ingerir dados.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.test:
        test_search(args.test)
    else:
        docs_path = args.docs_dir.resolve()
        if not docs_path.is_dir():
            log.critical("Diretório não encontrado: %s", docs_path)
            sys.exit(1)
        log.info("Iniciando ingestão a partir de: %s", docs_path)
        run_ingestion(docs_path)