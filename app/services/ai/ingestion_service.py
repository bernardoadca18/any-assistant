"""
app/services/ai/ingestion_service.py
=====================================
Pipeline de ETL "Code-to-Doc" — ingestão unificada e idempotente para o
Qdrant, executada em background dentro do próprio container `ue5-agent`.

Substitui os antigos scripts de ingestão externos (que rodavam no host WSL
e indexavam .cpp/.h brutos sem nenhum tratamento semântico, gerando ruído
no banco vetorial). Este serviço resolve o problema original em duas frentes:

    1. FILTRAGEM AGRESSIVA na origem — só chega ao Qdrant aquilo que tem
       valor semântico para responder dúvidas de gameplay.
    2. TRANSFORMAÇÃO SEMÂNTICA — código C++ bruto nunca é embedado
       diretamente; ele primeiro vira um texto explicativo gerado por LLM,
       que é o que de fato ajuda um modelo pequeno (ex: Qwen 9B) a responder
       com precisão em vez de entrar em loop por contradição lógica.

Motor de Duplo Canal:
    Canal A (.h C++)   → varre headers de gameplay relevantes, limpa,
                          envia para uma LLM rápida via LiteLLM que atua
                          como Documentador Técnico Sênior da Epic Games
                          e devolve um guia em Markdown.
    Canal B (.udn)     → sanitiza a documentação oficial via regex,
                          removendo metadados e macros proprietárias,
                          sem nenhuma chamada de LLM (é só limpeza).

Ambos os canais convergem para o mesmo pipeline de chunking estrutural
(por headers Markdown), enriquecimento de metadados, geração de embeddings
em lote e upsert idempotente no Qdrant.

NOTA DE ARQUITETURA — Hybrid RAG com Payload Enriquecido:
    O embedding em si SEMPRE é gerado a partir do texto semântico em
    Markdown (via `chunk["text"]`), exatamente como antes — isso preserva
    a qualidade da busca vetorial. A mudança está no PAYLOAD: cada chunk
    agora carrega também `raw_content`, o conteúdo ORIGINAL e integral do
    arquivo-fonte (o header C++ já limpo no Canal A, ou o Markdown .udn já
    sanitizado no Canal B), sem NUNCA ser embedado. O rag_service.py
    recupera esse campo junto do resto do payload e o injeta no prompt
    final como "Referência Direta" — uma segunda fonte, soberana para
    sintaxe exata (assinaturas, tipos, UPROPERTY/UFUNCTION), que
    complementa o resumo conceitual gerado pela LLM documentadora.

NOTA DE ARQUITETURA — idioma da documentação gerada:
    O `_DECOMPOSITION_SYSTEM_PROMPT` em rag_service.py já força as
    sub-queries de busca a serem em INGLÊS técnico, porque a documentação
    oficial da Epic (.udn) é nativamente em inglês. Para manter a base
    vetorial semanticamente consistente (e maximizar a qualidade do
    embedding/retrieval), o Canal A também instrui a LLM documentadora a
    escrever em inglês técnico — mesmo com estes comentários em PT-BR.

NOTA DE PERFORMANCE — por que asyncio.to_thread():
    BackgroundTasks do FastAPI rodam no MESMO event loop que atende as
    requisições HTTP normais (ex: POST /v1/chat). Uma varredura recursiva
    de uma árvore de engine (potencialmente dezenas de milhares de
    arquivos) feita de forma síncrona bloquearia o loop inteiro, travando
    outros usuários do assistente durante a ingestão. Por isso, toda
    operação de I/O de disco potencialmente pesada (os.walk, leitura de
    arquivo) é despachada para uma thread separada via asyncio.to_thread().

NOTA DE ARQUITETURA — Manifesto de Checkpoint via Redis (idempotência):
    Cada arquivo-fonte tem sua identidade de conteúdo rastreada em uma
    Hash do Redis `manifest:{collection_name}` (HSET caminho_relativo →
    sha256_hexdigest). Isso permite interromper e retomar uma ingestão
    grande sem reprocessar (e sem chamar a LLM documentadora de novo para)
    arquivos já indexados, e detectar automaticamente arquivos novos,
    inalterados ou modificados entre execuções.

    FRONTEIRA DE ATOMICIDADE ("o commit"): como os chunks de vários
    arquivos são acumulados em um buffer compartilhado (`pending`) e
    inseridos em lotes por `_flush_batch` (para minimizar chamadas de rede
    ao Qdrant), o manifesto NUNCA pode ser atualizado apenas por termos
    gerado os chunks de um arquivo — isso mapearia incorretamente "chunks
    na memória" para "chunks persistidos". Cada arquivo em voo é
    rastreado por um `_PendingFileCommit` (total de chunks gerados vs.
    chunks efetivamente confirmados por upserts bem-sucedidos). Só quando
    `confirmed_chunks == total_chunks` de um arquivo específico o HSET do
    seu hash é disparado — nunca antes, e nunca com base em contagens
    agregadas do lote. Se o processo morrer a qualquer momento antes
    disso, o arquivo simplesmente não aparece (ou aparece com o hash
    antigo) no manifesto, e será reprocessado integralmente na próxima
    execução — nunca há um "meio-termo" mentiroso no checkpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from redis.asyncio import Redis
from redis.asyncio.lock import Lock

from app.core.config import settings
from app.db.redis import ingestion_failures_key, ingestion_lock_key, ingestion_manifest_key
from app.services.ai.embedding_service import embed_text
from app.services.ai.rag_service import call_litellm

log = logging.getLogger(__name__)


# =============================================================================
# CONSTANTES — Filtros do motor de duplo canal e Chunking
# =============================================================================

MIN_BODY_CHARS: int = 100  # Quantidade mínima de caracteres de corpo para emitir um chunk isolado

_IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "Private",
        "Detail",
        "Details",
        "ThirdParty",
        "Internal",
        "Intermediate",
        "Binaries",
        "Build",
        "DerivedDataCache",
        "Saved",
        ".git",
        ".vs",
        "__pycache__",
    }
)

_GAMEPLAY_KEYWORDS: tuple[str, ...] = (
    "Character",
    "Pawn",
    "Actor",
    "Component",
    "AnimInstance",
    "GameplayStatics",
    "GameMode",
    "GameState",
    "PlayerController",
    "PlayerState",
    "Controller",
    "MovementComponent",
    "GameInstance",
    "HUD",
    "Subsystem",
    "Interface",
)

_CLASS_DECL_RE = re.compile(
    r"class\s+(?:[A-Z][A-Z0-9_]*_API\s+)?(\w+)\s*:\s*"
    r"((?:public|protected|private)\s+\w+(?:\s*,\s*(?:public|protected|private)\s+\w+)*)"
)

_INCLUDE_RE = re.compile(r"^\s*#include\s+.*$", re.MULTILINE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

_UDN_METADATA_RE = re.compile(
    r"^(?:Availability|Title|Crumbs|Description|Navigation|Parent|Order|"
    r"Type|Tags|SkillLevel|Version|topic-image|social-image|social-title|"
    r"social-description)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)
_UDN_TITLE_RE = re.compile(r"^Title\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

_UDN_MACRO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[/?[A-Z][A-Z0-9_]*\([^)]*\)\]"),
    re.compile(r"\[/?[A-Z][A-Z0-9_]*(?::[^\]]*)?\]"),
    re.compile(r"\[@[\w.\-]+\]"),
)

_UE_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


# =============================================================================
# SYSTEM PROMPT — Documentador Técnico Sênior (Canal A)
# =============================================================================

_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Technical Documentation Writer at Epic Games, specialized \
in translating raw Unreal Engine 5 C++ header files into clear, \
gameplay-focused educational documentation for game developers.

Your task: given a cleaned C++ header file, produce a Markdown guide that \
explains HOW a developer uses this class in practice — not a line-by-line \
restatement of the code.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title (`# ClassName`) followed by a short \
paragraph explaining the class's purpose and where it fits in the gameplay \
framework (e.g. "subclass of ACharacter, used as the player-controlled \
pawn").
3. Organize the rest of the content using H2 sections (`## `), for example: \
"## Key UPROPERTYs exposed to Blueprint", "## Key UFUNCTIONs and how to \
call them", "## Typical usage in C++", "## Typical usage in Blueprint". \
Use H3 (`### `) for sub-topics inside a section when it helps clarity.
4. For every UPROPERTY/UFUNCTION you document, explain its practical \
effect in gameplay terms — not just its signature. Mention relevant \
specifiers (BlueprintReadWrite, BlueprintCallable, EditAnywhere, etc.) \
only when they change how a developer interacts with the member.
5. Ignore purely structural code (empty constructors, boilerplate \
GENERATED_BODY(), forward declarations) unless relevant to usage.
6. Be concise but complete — prioritize developer usefulness over \
exhaustive coverage of every single line.
7. Never invent members, functions or parameters that are not present in \
the provided header.\
"""


# =============================================================================
# UTILITÁRIOS DE I/O (despachados para thread)
# =============================================================================

def _walk_files(root: Path, extension: str) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for fname in filenames:
            if fname.endswith(extension):
                yield Path(dirpath) / fname


async def _discover_files(root: Path, extension: str) -> list[Path]:
    return await asyncio.to_thread(lambda: list(_walk_files(root, extension)))


async def _read_file_bytes(path: Path) -> bytes:
    return await asyncio.to_thread(path.read_bytes)


def _compute_file_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


# =============================================================================
# CANAL A — Códigos-fonte C++ (.h)
# =============================================================================

def _is_gameplay_relevant_header(content: str, filename: str) -> tuple[bool, str]:
    for match in _CLASS_DECL_RE.finditer(content):
        class_name = match.group(1)
        inheritance_blob = match.group(2)
        base_classes = re.findall(r"\b\w+\b", inheritance_blob)
        candidates = [class_name, *base_classes]
        for candidate in candidates:
            candidate_lower = candidate.lower()
            if any(kw.lower() in candidate_lower for kw in _GAMEPLAY_KEYWORDS):
                return True, class_name

    stem = Path(filename).stem
    stem_lower = stem.lower()
    if any(kw.lower() in stem_lower for kw in _GAMEPLAY_KEYWORDS):
        return True, stem

    return False, ""


def _clean_cpp_header(content: str) -> str:
    cleaned = _INCLUDE_RE.sub("", content)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


async def _process_header_file(path: Path, raw: str, ue_version: str) -> list[dict]:
    relevant, class_name = _is_gameplay_relevant_header(raw, path.name)
    if not relevant:
        log.debug("Header ignorado (sem relevância de gameplay): %s", path)
        return []

    cleaned = _clean_cpp_header(raw)
    if not cleaned:
        log.debug("Header vazio após limpeza, ignorado: %s", path)
        return []

    # Rede de segurança: um header excepcionalmente grande pode estourar o
    # contexto da LLM documentadora (erro context_length_exceeded). Sem
    # truncar aqui, esse arquivo falharia sempre no mesmo ponto e seria
    # reprocessado do zero em toda ingestão futura, sem jamais conseguir um
    # checkpoint no manifesto. Cortar defensivamente é preferível a perder o
    # arquivo inteiro — a documentação gerada cobre a parte inicial da classe.
    max_chars = settings.ingestion_max_header_chars
    if len(cleaned) > max_chars:
        log.warning(
            "Header '%s' tem %d chars após limpeza (limite=%d) — truncando "
            "antes de enviar à LLM documentadora para evitar estouro de contexto.",
            path, len(cleaned), max_chars,
        )
        cleaned = cleaned[:max_chars]

    title = class_name or path.stem

    user_message = f"Source file: {path.name}\n\n```cpp\n{cleaned}\n```"
    messages = [
        {"role": "system", "content": _DOCUMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    markdown, _prompt_tokens, _completion_tokens = await call_litellm(
        messages=messages,
        model=settings.ingestion_documenter_model,
    )

    return _build_chunks_with_metadata(
        markdown_text=markdown,
        source_file=str(path),
        doc_type="C++ Source API",
        title=title,
        ue_version=ue_version,
        raw_content=cleaned,
    )


# =============================================================================
# CANAL B — Documentação oficial (.udn)
# =============================================================================

def _sanitize_udn(content: str) -> tuple[str, str]:
    title_match = _UDN_TITLE_RE.search(content)
    title = title_match.group(1).strip() if title_match else ""

    cleaned = _UDN_METADATA_RE.sub("", content)
    for pattern in _UDN_MACRO_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned).strip()

    if not title:
        heading_match = re.search(r"^#\s+(.+)$", cleaned, re.MULTILINE)
        if heading_match:
            title = heading_match.group(1).strip()

    return cleaned, title


async def _process_udn_file(path: Path, raw: str, ue_version: str) -> list[dict]:
    clean_text, title = _sanitize_udn(raw)
    if not clean_text:
        log.debug("Documento .udn vazio após sanitização, ignorado: %s", path)
        return []

    return _build_chunks_with_metadata(
        markdown_text=clean_text,
        source_file=str(path),
        doc_type="Official Documentation (.udn)",
        title=title or path.stem,
        ue_version=ue_version,
        raw_content=clean_text,
    )


# =============================================================================
# CHUNKING ESTRUTURAL
# =============================================================================

def _hierarchical_markdown_chunker(markdown_text: str, root_title: str) -> list[str]:
    """
    Varredura sequencial do documento construindo breadcrumbs hierárquicos
    (até H3). Resolve o problema de fragmentação garantindo contexto contínuo
    para seções pequenas e unificando chunks órfãos.
    """
    stack: dict[int, str] = {1: root_title}
    chunks: list[str] = []
    current_body: list[str] = []
    carried_text: str = ""  # Acumula textos de seções curtas anteriores para fluir para a próxima
    in_code_block: bool = False

    def _build_breadcrumb() -> str:
        parts = []
        for lvl in sorted(stack.keys()):
            if stack[lvl]:
                parts.append(f"{'#' * lvl} {stack[lvl]}")
        return " > ".join(parts)

    def _flush_section(is_end_of_doc: bool = False) -> None:
        nonlocal current_body, carried_text
        if not current_body and not carried_text:
            return

        raw_body = "\n".join(current_body).strip()
        # Mescla o texto acumulado de seções curtas anteriores com o corpo atual
        body_text = f"{carried_text}\n\n{raw_body}".strip() if carried_text else raw_body
        current_body = []
        carried_text = ""

        if not body_text:
            return

        if len(body_text) >= MIN_BODY_CHARS:
            breadcrumb = _build_breadcrumb()
            chunks.append(f"{breadcrumb}\n\n{body_text}")
        else:
            # Correção: Se for o fim do documento, aplica a estratégia de segurança
            if is_end_of_doc:
                if chunks:
                    chunks[-1] += f"\n\n{body_text}"  # Merge para trás
                else:
                    breadcrumb = _build_breadcrumb()
                    chunks.append(f"{breadcrumb}\n\n{body_text}")  # Documento inteiro é curto
            else:
                # Se for no meio do documento, segura o texto para fluir para a próxima seção
                carried_text = body_text

    header_re = re.compile(r"^(#{1,3})\s+(.+)$")
    
    for line in markdown_text.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            current_body.append(line)
            continue

        match = header_re.match(line) if not in_code_block else None
        if match:
            _flush_section(is_end_of_doc=False)
            level = len(match.group(1))
            title = match.group(2).strip()
            
            for k in list(stack.keys()):
                if k >= level:
                    stack.pop(k, None)
            
            stack[level] = title
        else:
            current_body.append(line)
            
    _flush_section(is_end_of_doc=True)
    return chunks


def _enforce_max_chunk_size(sections: list[str], max_chars: int) -> list[str]:
    """
    Garante que chunks maiores que o limite sejam fatiados, mas preservando e
    replicando o breadcrumb hierárquico estruturado no topo de CADA fatia.
    """
    result: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            result.append(section)
            continue

        parts = section.split("\n\n", 1)
        if len(parts) == 2 and parts[0].startswith("#"):
            breadcrumb = parts[0]
            body = parts[1]
        else:
            breadcrumb = ""
            body = section
            
        breadcrumb_len = len(breadcrumb) + 2 if breadcrumb else 0
        effective_max = max_chars - breadcrumb_len

        # Fallback de segurança caso o breadcrumb consuma espaço demais
        if effective_max <= 50:
            effective_max = max_chars

        paragraphs = body.split("\n\n")
        buffer = ""
        
        for paragraph in paragraphs:
            # Correção: Avalia o tamanho usando o teto efetivo disponível
            if len(paragraph) > effective_max:
                if buffer:
                    final_chunk = f"{breadcrumb}\n\n{buffer.strip()}".strip() if breadcrumb else buffer.strip()
                    result.append(final_chunk)
                    buffer = ""
                # Fatiamento forçado intra-parágrafo respeitando o effective_max
                for i in range(0, len(paragraph), effective_max):
                    slice_text = paragraph[i:i+effective_max].strip()
                    if slice_text:
                        final_chunk = f"{breadcrumb}\n\n{slice_text}".strip() if breadcrumb else slice_text
                        result.append(final_chunk)
                continue

            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            # Correção: O acumulador testa contra o limite efetivo com breadcrumb somado
            if len(candidate) > effective_max and buffer:
                final_chunk = f"{breadcrumb}\n\n{buffer.strip()}".strip() if breadcrumb else buffer.strip()
                result.append(final_chunk)
                buffer = paragraph
            else:
                buffer = candidate
                
        if buffer.strip():
            final_chunk = f"{breadcrumb}\n\n{buffer.strip()}".strip() if breadcrumb else buffer.strip()
            result.append(final_chunk)

    return result


def _extract_ue_version(path_str: str) -> str:
    match = _UE_VERSION_RE.search(path_str)
    return match.group(1) if match else settings.ingestion_default_ue_version


def _build_chunks_with_metadata(
    markdown_text: str,
    source_file: str,
    doc_type: str,
    title: str,
    ue_version: str,
    raw_content: str,
) -> list[dict]:
    sections = _hierarchical_markdown_chunker(markdown_text, title)
    sections = _enforce_max_chunk_size(sections, settings.ingestion_max_chunk_chars)

    chunks: list[dict] = []
    for chunk_id, section_text in enumerate(sections):
        chunks.append(
            {
                "text": section_text,
                "source_file": source_file,
                "doc_type": doc_type,
                "title": title,
                "chunk_id": chunk_id,
                "ue_version": ue_version,
                "description": "",  
                "raw_content": raw_content,
            }
        )
    return chunks


# =============================================================================
# CONFIGURAÇÃO IDEMPOTENTE DO QDRANT
# =============================================================================

async def ensure_collection_exists(qdrant: AsyncQdrantClient) -> None:
    collection_name = settings.qdrant_collection

    exists = await qdrant.collection_exists(collection_name)
    if exists:
        log.info("Coleção '%s' já existe — validação OK (idempotente).", collection_name)
        return

    log.warning(
        "Coleção '%s' não encontrada no Qdrant. Criando automaticamente...",
        collection_name,
    )
    await qdrant.create_collection(
        collection_name=collection_name,
        vectors_config=qdrant_models.VectorParams(
            size=settings.embedding_dim,
            distance=qdrant_models.Distance.COSINE,
        ),
    )
    log.info(
        "Coleção '%s' criada com sucesso | dim=%d | distância=COSINE",
        collection_name,
        settings.embedding_dim,
    )


# =============================================================================
# CHECKPOINT / MANIFESTO DE INGESTÃO (idempotência via Redis)
# =============================================================================

@dataclass
class _PendingFileCommit:
    relative_key: str
    file_hash: str
    total_chunks: int
    confirmed_chunks: int = 0


async def _load_manifest_cache(redis: Redis, manifest_key: str) -> dict[str, str]:
    try:
        cache = await redis.hgetall(manifest_key)
        log.info(
            "Manifesto de checkpoint carregado | chave=%s arquivos_conhecidos=%d",
            manifest_key,
            len(cache),
        )
        return cache
    except Exception as exc:
        log.error(
            "Falha ao carregar manifesto '%s' do Redis: %s. Prosseguindo sem "
            "cache — todo arquivo será tratado como novo.",
            manifest_key,
            exc,
            exc_info=True,
        )
        return {}


# =============================================================================
# QUARENTENA — falhas consecutivas por arquivo
# =============================================================================
# Sem isso, um arquivo permanentemente quebrado (ex: um chunk que sempre
# falha no embedding, ou um erro de parsing determinístico) nunca alcança
# `confirmed_chunks == total_chunks`, nunca entra no manifesto, e é
# reprocessado do zero — chamando a LLM documentadora de novo — em TODA
# ingestão futura, para sempre. Rastreamos falhas consecutivas por arquivo
# num hash separado do manifesto e paramos de tentar após N falhas seguidas,
# até intervenção manual.

async def _load_failures_cache(redis: Redis, failures_key: str) -> dict[str, int]:
    try:
        raw = await redis.hgetall(failures_key)
        return {key: int(value) for key, value in raw.items()}
    except Exception as exc:
        log.error(
            "Falha ao carregar contador de falhas '%s' do Redis: %s. "
            "Prosseguindo sem cache de quarentena.",
            failures_key,
            exc,
            exc_info=True,
        )
        return {}


async def _record_failure(
    redis: Redis,
    failures_key: str,
    failures_cache: dict[str, int],
    relative_key: str,
) -> int:
    new_count = failures_cache.get(relative_key, 0) + 1
    failures_cache[relative_key] = new_count
    try:
        await redis.hset(failures_key, relative_key, new_count)
    except Exception as exc:
        log.error(
            "Falha ao persistir contador de falhas para '%s': %s",
            relative_key,
            exc,
            exc_info=True,
        )
    return new_count


async def _clear_failure(
    redis: Redis,
    failures_key: str,
    failures_cache: dict[str, int],
    relative_key: str,
) -> None:
    if relative_key not in failures_cache:
        return
    failures_cache.pop(relative_key, None)
    try:
        await redis.hdel(failures_key, relative_key)
    except Exception as exc:
        log.warning(
            "Falha ao limpar contador de falhas para '%s' (não crítico): %s",
            relative_key,
            exc,
        )

async def _delete_file_chunks(
    qdrant: AsyncQdrantClient,
    collection_name: str,
    source_file: str,
) -> None:
    result = await qdrant.delete(
        collection_name=collection_name,
        points_selector=qdrant_models.FilterSelector(
            filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="source_file",
                        match=qdrant_models.MatchValue(value=source_file),
                    )
                ]
            )
        ),
        wait=True,
    )
    log.info(
        "Limpeza defensiva executada (chunks anteriores removidos) | arquivo=%s status=%s",
        source_file,
        result.status,
    )


async def _commit_confirmed_files(
    redis: Redis,
    manifest_key: str,
    tracking: dict[str, _PendingFileCommit],
    per_file_counts: dict[str, int],
    failures_key: str | None = None,
    failures_cache: dict[str, int] | None = None,
) -> None:
    for source_file, confirmed in per_file_counts.items():
        tracker = tracking.get(source_file)
        if tracker is None:
            continue

        tracker.confirmed_chunks += confirmed
        if tracker.confirmed_chunks < tracker.total_chunks:
            continue

        try:
            await redis.hset(manifest_key, tracker.relative_key, tracker.file_hash)
        except Exception as exc:
            log.error(
                "Falha ao commitar checkpoint para '%s': %s",
                tracker.relative_key,
                exc,
                exc_info=True,
            )
            continue

        # Sucesso confirmado — o arquivo sai de quarentena, se estava nela.
        if failures_key is not None and failures_cache is not None:
            await _clear_failure(redis, failures_key, failures_cache, tracker.relative_key)

        log.info(
            "Checkpoint commitado | arquivo=%s hash=%s chunks=%d/%d",
            tracker.relative_key,
            tracker.file_hash[:12],
            tracker.confirmed_chunks,
            tracker.total_chunks,
        )
        del tracking[source_file]


# =============================================================================
# EMBEDDING EM LOTE + UPSERT
# =============================================================================

async def _flush_batch(
    qdrant: AsyncQdrantClient,
    collection_name: str,
    batch: list[dict],
) -> dict[str, int]:
    if not batch:
        return {}

    semaphore = asyncio.Semaphore(settings.ingestion_embedding_concurrency)

    async def _embed_one(chunk: dict) -> tuple[dict, list[float] | None]:
        async with semaphore:
            try:
                vector = await embed_text(chunk["text"])
                return chunk, vector
            except Exception as exc:
                log.error(
                    "Falha ao gerar embedding | arquivo=%s chunk_id=%d: %s",
                    chunk["source_file"],
                    chunk["chunk_id"],
                    exc,
                )
                return chunk, None

    results = await asyncio.gather(*[_embed_one(c) for c in batch])

    points: list[qdrant_models.PointStruct] = []
    for chunk, vector in results:
        if vector is None:
            continue
        point_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['source_file']}::{chunk['chunk_id']}")
        )
        points.append(
            qdrant_models.PointStruct(id=point_id, vector=vector, payload=chunk)
        )

    if not points:
        log.warning("Lote descartado: todos os %d embeddings falharam.", len(batch))
        return {}

    try:
        await qdrant.upsert(collection_name=collection_name, points=points, wait=True)
    except Exception as exc:
        log.error(
            "Falha no upsert do lote (%d pontos) na coleção '%s': %s",
            len(points),
            collection_name,
            exc,
        )
        return {}

    log.info(
        "Batch inserido com sucesso | %d pontos | coleção=%s",
        len(points),
        collection_name,
    )

    per_file_counts: dict[str, int] = {}
    for point in points:
        source_file = point.payload["source_file"]
        per_file_counts[source_file] = per_file_counts.get(source_file, 0) + 1

    return per_file_counts


# =============================================================================
# ORQUESTRADOR PRINCIPAL — entrypoint chamado pelo BackgroundTasks
# =============================================================================

async def process_folder_ingestion(
    folder_path: str,
    content_type: str,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    ue_version_override: str | None = None,
) -> None:
    """
    Entrypoint chamado pelo BackgroundTasks. Serializa a execução por
    coleção via lock distribuído no Redis antes de delegar o trabalho para
    `_run_ingestion`.

    NOTA DE CONCORRÊNCIA: sem esse lock, duas ingestões concorrentes para a
    mesma coleção (ex: um retry acidental do operador, ou dois canais
    disparados em paralelo) podem intercalar o ciclo
    delete-antigo → gera-chunks → upsert-novo do MESMO arquivo. Isso pode
    fazer uma execução apagar pontos que a outra acabou de inserir, enquanto
    o manifesto é commitado como se tudo estivesse íntegro — uma perda
    silenciosa de dados que só aparece depois, como um buraco na base
    vetorial. O lock evita esse cenário aceitando apenas uma execução por
    vez por coleção; uma segunda tentativa concorrente é simplesmente
    rejeitada com um aviso, em vez de rodar em paralelo.
    """
    collection_name = settings.qdrant_collection
    lock_key = ingestion_lock_key(collection_name)
    lock = Lock(
        redis,
        lock_key,
        timeout=settings.ingestion_lock_timeout_seconds,
        blocking=False,
    )

    acquired = await lock.acquire()
    if not acquired:
        log.warning(
            "Ingestão para a coleção '%s' já está em andamento (lock '%s' "
            "ocupado) — descartando esta chamada duplicada em vez de rodar "
            "em paralelo.",
            collection_name,
            lock_key,
        )
        return

    try:
        await _run_ingestion(
            folder_path=folder_path,
            content_type=content_type,
            qdrant=qdrant,
            redis=redis,
            collection_name=collection_name,
            ue_version_override=ue_version_override,
        )
    finally:
        try:
            await lock.release()
        except Exception as exc:
            # Não crítico: se o lock já expirou por TTL (ingestion_lock_timeout_seconds),
            # release() pode falhar por já não pertencer mais a este processo.
            log.warning(
                "Falha ao liberar lock '%s' (provavelmente já expirou por TTL): %s",
                lock_key,
                exc,
            )


async def _run_ingestion(
    folder_path: str,
    content_type: str,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    collection_name: str,
    ue_version_override: str | None = None,
) -> None:
    """Corpo real do pipeline de ingestão, protegido pelo lock do chamador."""
    root = Path(folder_path)
    batch_size = settings.ingestion_batch_size
    manifest_key = ingestion_manifest_key(collection_name)
    failures_key = ingestion_failures_key(collection_name)
    max_failures = settings.ingestion_max_consecutive_failures

    log.info(
        "Ingestão iniciada | pasta=%s tipo=%s coleção=%s manifesto=%s",
        root,
        content_type,
        collection_name,
        manifest_key,
    )

    try:
        await ensure_collection_exists(qdrant)
    except Exception as exc:
        log.error(
            "Falha catastrófica ao garantir a coleção '%s': %s",
            collection_name,
            exc,
            exc_info=True,
        )
        return

    manifest_cache = await _load_manifest_cache(redis, manifest_key)
    failures_cache = await _load_failures_cache(redis, failures_key)
    ue_version = ue_version_override or _extract_ue_version(str(root))
    extension = ".h" if content_type == "source_code" else ".udn"
    
    files = await _discover_files(root, extension)
    total_files = len(files)

    log.info(
        "%d arquivo(s) '%s' encontrado(s) em '%s' para avaliação.",
        total_files,
        extension,
        root,
    )

    pending: list[dict] = []
    tracking: dict[str, _PendingFileCommit] = {}
    stats = {
        "processed": 0,
        "modified": 0,
        "skipped": 0,
        "errors": 0,
        "quarantined": 0,
        "chunks_indexed": 0,
        "partial_failures": 0,
    }

    for i, file_path in enumerate(files, start=1):
        log.info("[%d/%d] Avaliando arquivo: %s", i, total_files, file_path)

        source_file_str = str(file_path)

        try:
            relative_key = file_path.relative_to(root).as_posix()
        except ValueError:
            relative_key = file_path.as_posix()
            log.warning("Caminho relativo falhou para '%s' — usando absoluto.", file_path)

        # Quarentena: arquivo já falhou de forma consecutiva N vezes — não
        # tenta de novo automaticamente até que o contador seja limpo
        # manualmente (HDEL na chave de falhas) ou o arquivo seja corrigido
        # e reingestão manual disparada.
        if failures_cache.get(relative_key, 0) >= max_failures:
            stats["quarantined"] += 1
            log.error(
                "[%d/%d] Arquivo em quarentena (%d falhas consecutivas) — "
                "pulando sem tentar novamente: %s",
                i, total_files, failures_cache[relative_key], file_path,
            )
            continue

        try:
            raw_bytes = await _read_file_bytes(file_path)
        except Exception as exc:
            stats["errors"] += 1
            await _record_failure(redis, failures_key, failures_cache, relative_key)
            log.error("Falha ao ler '%s' do disco: %s", file_path, exc, exc_info=True)
            continue

        file_hash = _compute_file_hash(raw_bytes)
        existing_hash = manifest_cache.get(relative_key)

        # Checkpoint: Ignora apenas se confirmadamente inalterado
        if existing_hash == file_hash:
            stats["skipped"] += 1
            log.info("[%d/%d] Ignorado — checkpoint OK (hash inalterado).", i, total_files)
            continue

        # Gatilho de Limpeza Unificado (sempre executa se for processar)
        try:
            log.info("Limpando órfãos do Qdrant para garantir idempotência: %s", file_path)
            await _delete_file_chunks(qdrant, collection_name, source_file_str)
            if existing_hash is not None:
                stats["modified"] += 1
        except Exception as exc:
            stats["errors"] += 1
            await _record_failure(redis, failures_key, failures_cache, relative_key)
            log.error("Falha na limpeza de órfãos para '%s' — ignorando: %s", file_path, exc)
            continue

        # Processamento e Geração de Chunks
        try:
            raw_text = raw_bytes.decode("utf-8", errors="ignore")

            if content_type == "source_code":
                chunks = await _process_header_file(file_path, raw_text, ue_version)
            else:
                chunks = await _process_udn_file(file_path, raw_text, ue_version)

            if not chunks:
                stats["skipped"] += 1
                continue

            # --- FILTRAGEM DE CHUNKS VAZIOS / CABEÇALHOS ÓRFÃOS ---
            filtered_chunks = []
            for chunk in chunks:
                # Extrai o texto do chunk tratando de forma segura se ele for dict ou objeto
                text = chunk.get("text", "") if isinstance(chunk, dict) else getattr(chunk, "text", "")
                
                # Divide em linhas reais e limpa os espaços em branco das extremidades
                lines = [line.strip() for line in text.split("\n") if line.strip()]
                
                # Se o chunk não tiver nenhuma linha válida, descarta
                if not lines:
                    continue
                
                # Se só tem 1 linha e ela começa com '#', é um cabeçalho cortado/órfão. Descarta.
                if len(lines) == 1 and lines[0].startswith("#"):
                    log.info("Dropando cabeçalho órfão do pipeline: '%s'", lines[0])
                    continue
                
                filtered_chunks.append(chunk)
            
            # Substitui a lista original pela lista filtrada limpa
            chunks = filtered_chunks

            if not chunks:
                stats["skipped"] += 1
                continue
            # -----------------------------------------------------

            stats["processed"] += 1
            tracking[source_file_str] = _PendingFileCommit(
                relative_key=relative_key,
                file_hash=file_hash,
                total_chunks=len(chunks),
            )
            pending.extend(chunks)

            # Flush condicionado ao buffer > batch_size (atomicidade do commit)
            while len(pending) >= batch_size:
                batch, pending = pending[:batch_size], pending[batch_size:]
                per_file_counts = await _flush_batch(qdrant, collection_name, batch)
                stats["chunks_indexed"] += sum(per_file_counts.values())
                await _commit_confirmed_files(
                    redis, manifest_key, tracking, per_file_counts,
                    failures_key=failures_key, failures_cache=failures_cache,
                )

        except Exception as exc:
            stats["errors"] += 1
            tracking.pop(source_file_str, None)
            await _record_failure(redis, failures_key, failures_cache, relative_key)
            log.error("Erro processando '%s' — ignorando: %s", file_path, exc, exc_info=True)
            continue

    # Flush de resíduos finais
    if pending:
        per_file_counts = await _flush_batch(qdrant, collection_name, pending)
        stats["chunks_indexed"] += sum(per_file_counts.values())
        await _commit_confirmed_files(
            redis, manifest_key, tracking, per_file_counts,
            failures_key=failures_key, failures_cache=failures_cache,
        )

    # Identificação de falhas parciais (chunks perdidos no embedding/upsert limitaram commit)
    for source_file, tracker in tracking.items():
        stats["partial_failures"] += 1
        log.warning(
            "Arquivo NÃO commitado no manifesto (persistência parcial) | "
            "arquivo=%s confirmados=%d/%d — será reprocessado na próxima ingestão.",
            source_file,
            tracker.confirmed_chunks,
            tracker.total_chunks,
        )

    log.info(
        "Ingestão concluída | pasta=%s arquivos_encontrados=%d processados=%d "
        "modificados=%d ignorados=%d erros=%d quarentena=%d parciais=%d chunks_indexados=%d",
        root,
        total_files,
        stats["processed"],
        stats["modified"],
        stats["skipped"],
        stats["errors"],
        stats["quarantined"],
        stats["partial_failures"],
        stats["chunks_indexed"],
    )