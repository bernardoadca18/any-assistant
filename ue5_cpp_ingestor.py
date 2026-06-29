"""
ue5_cpp_ingestor.py
===================
Pipeline de Ingestão RAG para Código-Fonte da Unreal Engine 5 (.h, .cpp, .cs)
Autenticado no LiteLLM Proxy (localhost:4000) usando a Master Key.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import httpx
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.http import models as qdrant_models

# ---------------------------------------------------------------------------
# Configurações Padrão
# ---------------------------------------------------------------------------
QDRANT_URL: str = "http://localhost:6333"
LITELLM_URL: str = "http://localhost:4000/v1"
COLLECTION_NAME: str = "ue5_docs"
EMBEDDING_MODEL: str = "ollama-embed"
EMBEDDING_DIM: int = 1024

CHUNK_SIZE: int = 1500
CHUNK_OVERLAP: int = 200
BATCH_SIZE: int = 64
DOC_TYPE: str = "C++ Source"

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

@dataclass
class CodeDocument:
    source_file: str
    filename: str
    extension: str
    body: str

@dataclass
class QdrantPoint:
    id: str
    vector: list[float]
    payload: dict

# ---------------------------------------------------------------------------
# Embedding via LiteLLM com Autenticação Bearer Token
# ---------------------------------------------------------------------------
def get_embedding(text: str, api_url: str, api_key: str) -> list[float]:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        log.warning("Aviso: Nenhuma LITELLM_MASTER_KEY foi fornecida. Pode gerar erro 401.")

    try:
        resp = httpx.post(
            f"{api_url.rstrip('/')}/embeddings",
            json={"model": EMBEDDING_MODEL, "input": text},
            headers=headers,
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM retornou erro {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Falha ao chamar LiteLLM em {api_url}: {exc}") from exc

    data = resp.json()
    try:
        vector = data["data"][0]["embedding"]
        return vector
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Resposta inesperada do formato de embedding do LiteLLM: {data}") from exc

# ---------------------------------------------------------------------------
# Varredura de Arquivos
# ---------------------------------------------------------------------------
def iter_source_files(root: Path) -> Generator[Path, None, None]:
    extensions = {".H", ".CPP", ".CS"}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.upper() in extensions:
            yield path

def parse_source_file(path: Path) -> CodeDocument | None:
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Falha ao ler '%s': %s", path, exc)
        return None

    if not body.strip():
        return None

    return CodeDocument(
        source_file=str(path),
        filename=path.name,
        extension=path.suffix.lower(),
        body=body,
    )

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
    else:
        log.info("Coleção '%s' detectada. Injetando novos vetores de código via LiteLLM.", COLLECTION_NAME)

    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="doc_type",
        field_schema=qdrant_models.PayloadSchemaType.KEYWORD,
    )

def build_points(doc: CodeDocument, chunks: list[str], api_url: str, api_key: str) -> list[QdrantPoint]:
    points: list[QdrantPoint] = []
    for idx, chunk in enumerate(chunks):
        try:
            vector = get_embedding(chunk, api_url, api_key)
        except RuntimeError as exc:
            log.warning("  [SKIP] Chunk %d de '%s' descartado por erro de embedding: %s", idx, doc.filename, exc)
            continue

        points.append(
            QdrantPoint(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "chunk_id": idx,
                    "doc_type": DOC_TYPE,
                    "title": f"Code: {doc.filename}",
                    "description": f"Arquivo de código fonte Unreal Engine ({doc.extension})",
                    "source_file": doc.source_file,
                },
            )
        )
    return points

def flush_batch(client: QdrantClient, batch: list[QdrantPoint]) -> None:
    if not batch:
        return
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            qdrant_models.PointStruct(id=p.id, vector=p.vector, payload=p.payload)
            for p in batch
        ],
    )

# ---------------------------------------------------------------------------
# Pipeline Principal
# ---------------------------------------------------------------------------
def run_ingestion(source_dir: Path, qdrant_url: str, api_url: str, api_key: str) -> None:
    qdrant = QdrantClient(url=qdrant_url)
    ensure_collection(qdrant)

    cpp_splitter = RecursiveCharacterTextSplitter.from_language(
        language=Language.CPP, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    generic_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, separators=["\n\n", "\n", " ", ""]
    )

    stats = {"found": 0, "processed": 0, "chunks": 0, "points": 0}
    pending_batch: list[QdrantPoint] = []

    def flush_if_needed(force: bool = False) -> None:
        nonlocal pending_batch
        if len(pending_batch) >= BATCH_SIZE or (force and pending_batch):
            flush_batch(qdrant, pending_batch)
            stats["points"] += len(pending_batch)
            pending_batch = []

    log.info("Iniciando varredura de código em: %s", source_dir)
    for path in iter_source_files(source_dir):
        stats["found"] += 1
        doc = parse_source_file(path)
        if not doc:
            continue

        if doc.extension in [".h", ".cpp"]:
            chunks = cpp_splitter.split_text(doc.body)
        else:
            chunks = generic_splitter.split_text(doc.body)

        if not chunks:
            continue

        log.info("Processando '%s': %d chunk(s)...", doc.filename, len(chunks))
        stats["processed"] += 1
        stats["chunks"] += len(chunks)

        points = build_points(doc, chunks, api_url, api_key)
        pending_batch.extend(points)
        flush_if_needed()

    flush_if_needed(force=True)
    log.info("=" * 60)
    log.info("INGESTÃO DE CÓDIGO CONCLUÍDA")
    log.info("  Arquivos encontrados : %d", stats["found"])
    log.info("  Arquivos processados : %d", stats["processed"])
    log.info("  Chunks de código     : %d", stats["chunks"])
    log.info("  Pontos adicionados   : %d", stats["points"])
    log.info("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pipeline de ingestão RAG para código-fonte da UE5.")
    parser.add_argument("--src-dir", type=Path, required=True, help="Diretório raiz do código fonte.")
    parser.add_argument("--qdrant-url", default=QDRANT_URL, help=f"URL do Qdrant (padrão: {QDRANT_URL})")
    parser.add_argument("--litellm-url", default=LITELLM_URL, help=f"URL do LiteLLM (padrão: {LITELLM_URL})")
    parser.add_argument("--api-key", default=os.environ.get("LITELLM_MASTER_KEY", ""), help="Chave Mestra do LiteLLM")
    args = parser.parse_args()

    if not args.src_dir.is_dir():
        log.critical("Diretório inválido: %s", args.src_dir)
        sys.exit(1)

    # Garante que temos a chave necessária antes de rodar
    current_key = args.api_key
    if not current_key:
        log.critical("Erro: LITELLM_MASTER_KEY não encontrada no ambiente e nem passada via --api-key.")
        sys.exit(1)

    run_ingestion(args.src_dir, qdrant_url=args.qdrant_url, api_url=args.litellm_url, api_key=current_key)