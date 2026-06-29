"""
ue5_rag_ingestor.py
===================
Pipeline de Ingestão RAG — Documentação Unreal Engine 5 (formato .INT.udn)

Este script roda no HOST (WSL/Linux), não dentro do Docker.
Acessa diretamente:
  - LiteLLM Proxy em localhost:4000/v1 (ou URL configurável via --litellm-url)
  - Qdrant em localhost:6333         (ou URL configurável via --qdrant-url)

Mapeia automaticamente a chave LITELLM_MASTER_KEY se estiver definida no ambiente.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

# ---------------------------------------------------------------------------
# Configuração central
# ---------------------------------------------------------------------------

QDRANT_URL: str = "http://localhost:6333"
LITELLM_URL: str = "http://localhost:4000/v1"
COLLECTION_NAME: str = "ue5_docs"
EMBEDDING_MODEL: str = "ollama-embed"
EMBEDDING_DIM: int = 1024

CHUNK_SIZE: int = 1200
CHUNK_OVERLAP: int = 150
BATCH_SIZE: int = 64

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

_RE_EXCERPT_BLOCK = re.compile(
    r"\[EXCERPT:[^\]]*\].*?\[/EXCERPT\]", re.DOTALL | re.IGNORECASE
)
_RE_TAG_INLINE = re.compile(
    r"\[/?(?:EXCERPT|VAR|REGION|OBJECT|PARAM|INCLUDE)[^\]]*\]",
    re.IGNORECASE,
)
_RE_VAR_BLOCK = re.compile(
    r"\[VAR:[^\]]*\].*?\[/VAR\]", re.DOTALL | re.IGNORECASE
)
_RE_BLANK_LINES = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------

@dataclass
class UDNDocument:
    source_file: str
    title: str
    description: str
    body: str


@dataclass
class QdrantPoint:
    id: str
    vector: list[float]
    payload: dict


# ---------------------------------------------------------------------------
# Embedding via LiteLLM (Padrão OpenAI)
# ---------------------------------------------------------------------------

def get_embedding(text: str, litellm_url: str) -> list[float]:
    """
    Gera embedding chamando o LiteLLM proxy usando o padrão OpenAI: POST /v1/embeddings
    """
    headers = {}
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"

    try:
        resp = httpx.post(
            f"{litellm_url.rstrip('/')}/embeddings",
            headers=headers,
            json={"model": EMBEDDING_MODEL, "input": text},
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Não foi possível conectar ao LiteLLM em {litellm_url}. "
            "Verifique se o container do LiteLLM está rodando de forma saudável."
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM retornou HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc

    data = resp.json()
    try:
        # Padrão OpenAI: data.data[0].embedding
        vector = data["data"][0]["embedding"]
        return vector
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LiteLLM retornou resposta sem formato OpenAI válido: {data}") from exc


# ---------------------------------------------------------------------------
# Parsing UDN
# ---------------------------------------------------------------------------

def iter_udn_files(root: Path) -> Generator[Path, None, None]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.upper().endswith(".INT.UDN"):
            yield path


def parse_udn_file(path: Path) -> UDNDocument | None:
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
    text = _RE_EXCERPT_BLOCK.sub("", text)
    text = _RE_VAR_BLOCK.sub("", text)
    text = _RE_TAG_INLINE.sub("", text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def split_document(doc: UDNDocument, splitter: RecursiveCharacterTextSplitter) -> list[str]:
    return splitter.split_text(doc.body)


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------

def ensure_collection(client: QdrantClient) -> None:
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

    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="doc_type",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )
    log.info("Índice keyword em 'doc_type' garantido.")


def build_points(
    doc: UDNDocument,
    chunks: list[str],
    litellm_url: str,
) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []

    for idx, chunk in enumerate(chunks):
        try:
            vector = get_embedding(chunk, litellm_url)
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


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run_ingestion(docs_dir: Path, qdrant_url: str, litellm_url: str) -> None:
    log.info("Conectando ao Qdrant em %s...", qdrant_url)
    try:
        qdrant = QdrantClient(url=qdrant_url)
        qdrant.get_collections()
    except Exception as exc:
        log.critical("Não foi possível conectar ao Qdrant: %s", exc)
        sys.exit(1)

    log.info("Testando conexão com LiteLLM em %s...", litellm_url)
    headers = {}
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"

    try:
        r = httpx.get(f"{litellm_url.rstrip('/')}/models", headers=headers, timeout=10.0)
        r.raise_for_status()
        models = [m["id"] for m in r.json().get("data", [])]
        log.info("LiteLLM conectado. Modelos disponíveis: %s", models)
    except Exception as exc:
        log.critical("Não foi possível conectar ao LiteLLM em %s: %s", litellm_url, exc)
        sys.exit(1)

    ensure_collection(qdrant)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

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

        log.info("Processando '%s': %d chunk(s)...", udn_path.name, len(chunks))
        stats["files_processed"] += 1
        stats["total_chunks"] += len(chunks)

        points = build_points(doc, chunks, litellm_url)
        pending_batch.extend(points)
        flush_if_needed()

    flush_if_needed(force=True)

    log.info("=" * 60)
    log.info("INGESTÃO CONCLUÍDA")
    log.info("  Arquivos encontrados : %d", stats["files_found"])
    log.info("  Arquivos processados : %d", stats["files_processed"])
    log.info("  Arquivos ignorados   : %d", stats["files_skipped"])
    log.info("  Chunks gerados       : %d", stats["total_chunks"])
    log.info("  Pontos no Qdrant     : %d", stats["total_points"])
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Busca de validação
# ---------------------------------------------------------------------------

def test_search(query: str, qdrant_url: str, litellm_url: str, top_k: int = 5) -> None:
    log.info("Executando busca de validação: '%s'", query)

    try:
        qdrant = QdrantClient(url=qdrant_url)
        vector = get_embedding(query, litellm_url)
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
        description="Pipeline de ingestão RAG para documentação UE5 (.INT.udn) → Qdrant via LiteLLM."
    )
    parser.add_argument(
        "--qdrant-url",
        default=QDRANT_URL,
        help=f"URL do Qdrant (padrão: {QDRANT_URL})",
    )
    parser.add_argument(
        "--litellm-url",
        default=LITELLM_URL,
        help=f"URL do LiteLLM Proxy (padrão: {LITELLM_URL})",
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
        test_search(args.test, qdrant_url=args.qdrant_url, litellm_url=args.litellm_url)
    else:
        docs_path = args.docs_dir.resolve()
        if not docs_path.is_dir():
            log.critical("Diretório não encontrado: %s", docs_path)
            sys.exit(1)
        log.info("Iniciando ingestão a partir de: %s", docs_path)
        run_ingestion(docs_path, qdrant_url=args.qdrant_url, litellm_url=args.litellm_url)