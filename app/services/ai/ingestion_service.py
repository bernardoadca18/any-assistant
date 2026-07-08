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
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Iterator

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

from app.core.config import settings
from app.services.ai.embedding_service import embed_text
from app.services.ai.rag_service import call_litellm

log = logging.getLogger(__name__)


# =============================================================================
# CONSTANTES — Filtros do motor de duplo canal
# =============================================================================

# Diretórios cujo conteúdo nunca tem valor semântico para um assistente de
# gameplay — são detalhes de build, implementação interna ou bibliotecas
# de terceiros. Os 4 primeiros foram explicitamente pedidos; os demais são
# ruído clássico de árvores de projeto/engine UE5 e foram incluídos como
# extensão natural do mesmo filtro.
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

# Palavras-chave de classes/conceitos centrais de gameplay no UE5. Usadas
# tanto para checar a declaração de herança (`class AMyPawn : public APawn`)
# quanto como fallback pelo nome do arquivo. A lista pedida explicitamente
# foi estendida com bases igualmente centrais (PlayerController, HUD,
# Subsystem, Interface, GameInstance) — mesmo espírito do "etc." do pedido.
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

# Detecta `class [MACRO_API] NomeDaClasse : public Base1, public Base2 ...`
# Cobre o padrão UE5 com macro de exportação (ex: ENGINE_API, MYGAME_API).
_CLASS_DECL_RE = re.compile(
    r"class\s+(?:[A-Z][A-Z0-9_]*_API\s+)?(\w+)\s*:\s*"
    r"((?:public|protected|private)\s+\w+(?:\s*,\s*(?:public|protected|private)\s+\w+)*)"
)

_INCLUDE_RE = re.compile(r"^\s*#include\s+.*$", re.MULTILINE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

# --- Sanitização .udn -------------------------------------------------------

# Campos de metadados estruturais conhecidos do formato .udn da Epic.
# Removidos por nome explícito (em vez de "qualquer linha com ':'") para
# não destruir prosa legítima do tipo "Nota: configure isso antes de...".
_UDN_METADATA_RE = re.compile(
    r"^(?:Availability|Title|Crumbs|Description|Navigation|Parent|Order|"
    r"Type|Tags|SkillLevel|Version|topic-image|social-image|social-title|"
    r"social-description)\s*:.*$",
    re.MULTILINE | re.IGNORECASE,
)
_UDN_TITLE_RE = re.compile(r"^Title\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

# Macros proprietárias da Epic no formato [TAG], [TAG:valor], [/TAG],
# [TAG(args)] e [@id]. Três padrões cobrem os formatos vistos na prática:
#   [DIR(output...)]        -> tag com parênteses
#   [REGION:valor] / [/REGION] -> tag com dois-pontos ou fechamento
#   [@some.id]               -> âncoras internas
_UDN_MACRO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[/?[A-Z][A-Z0-9_]*\([^)]*\)\]"),
    re.compile(r"\[/?[A-Z][A-Z0-9_]*(?::[^\]]*)?\]"),
    re.compile(r"\[@[\w.\-]+\]"),
)

_UE_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_MARKDOWN_HEADING_SPLIT_RE = re.compile(r"(?m)^(?=#{2,3}\s+)")


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
# UTILITÁRIOS DE I/O (despachados para thread — ver nota de performance acima)
# =============================================================================

def _walk_files(root: Path, extension: str) -> Iterator[Path]:
    """
    Geração síncrona dos caminhos de arquivo com a extensão informada,
    podando diretórios ignorados durante a própria varredura (evita
    descer em árvores gigantes e irrelevantes como ThirdParty/Intermediate).
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for fname in filenames:
            if fname.endswith(extension):
                yield Path(dirpath) / fname


async def _discover_files(root: Path, extension: str) -> list[Path]:
    """Varredura recursiva da pasta, executada em thread separada."""
    return await asyncio.to_thread(lambda: list(_walk_files(root, extension)))


async def _read_file_text(path: Path) -> str:
    """Leitura de arquivo em thread separada, tolerante a encoding."""
    return await asyncio.to_thread(
        path.read_text, encoding="utf-8", errors="ignore"
    )


# =============================================================================
# CANAL A — Códigos-fonte C++ (.h)
# =============================================================================

def _is_gameplay_relevant_header(content: str, filename: str) -> tuple[bool, str]:
    """
    Heurística de relevância: primeiro tenta achar uma declaração de classe
    cujo nome OU cuja(s) classe(s)-base contenha(m) uma keyword de gameplay
    (ex: `class MYGAME_API AMyWeapon : public AActor`). Se nenhuma
    declaração de classe bater, cai para um fallback pelo nome do arquivo
    (cobre casos como structs/enums em arquivos como "GameplayTags.h").

    Returns:
        (relevante, nome_da_classe_detectado) — nome vazio se não relevante.
    """
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
    """
    Remove diretivas #include e colapsa linhas em branco excessivas antes
    de enviar o header para a LLM — economiza tokens de contexto sem
    perder nenhuma informação semântica relevante (UPROPERTY/UFUNCTION,
    comentários Doxygen e a declaração da classe são todos preservados).
    """
    cleaned = _INCLUDE_RE.sub("", content)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


async def _process_header_file(path: Path, ue_version: str) -> list[dict]:
    """
    Processa um único header .h: filtra, limpa, envia para a LLM
    documentadora e quebra o Markdown resultante em chunks com metadados.

    Retorna lista vazia (sem levantar exceção) se o arquivo não for
    relevante — isso NÃO é um erro, é o filtro de ruído funcionando.
    """
    raw = await _read_file_text(path)

    relevant, class_name = _is_gameplay_relevant_header(raw, path.name)
    if not relevant:
        log.debug("Header ignorado (sem relevância de gameplay): %s", path)
        return []

    cleaned = _clean_cpp_header(raw)
    if not cleaned:
        log.debug("Header vazio após limpeza, ignorado: %s", path)
        return []

    title = class_name or path.stem

    user_message = f"Source file: {path.name}\n\n```cpp\n{cleaned}\n```"
    messages = [
        {"role": "system", "content": _DOCUMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # Reaproveita a infraestrutura de chamada ao LiteLLM já validada em
    # rag_service.py — mesmo client HTTP, mesmos headers, mesmo tratamento
    # de erro de timeout/HTTP. Evita duplicar lógica de transporte.
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
    """
    Remove metadados estruturais (Title:, Availability:, Crumbs:, ...) e
    macros proprietárias da Epic ([REGION], [DIR(...)], [@id], ...) via
    regex, preservando a hierarquia de títulos Markdown (#, ##, ###) e o
    texto corrido. Retorna (texto_limpo, título_extraído).
    """
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


async def _process_udn_file(path: Path, ue_version: str) -> list[dict]:
    """
    Processa um único arquivo .udn: sanitização via regex (sem LLM — é
    documentação oficial, já bem escrita, só precisa de limpeza estrutural)
    seguida de chunking estrutural e enriquecimento de metadados.
    """
    raw = await _read_file_text(path)

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
# CHUNKING ESTRUTURAL (compartilhado pelos dois canais)
# =============================================================================

def _split_by_markdown_headers(markdown_text: str) -> list[str]:
    """
    Divide o Markdown pelas suas seções de subtítulo (## ou ###), em vez de
    um corte cego por número de caracteres — garante que cada chunk tenha
    começo, meio e fim lógicos (ex: a seção inteira "Como configurar as
    transições do AnimGraph" vira um único chunk coeso).

    O texto antes do primeiro ## (tipicamente o título H1 + parágrafo de
    abertura) vira o chunk 0. Se esse trecho de abertura for trivialmente
    curto (só o título, sem corpo), ele é fundido à primeira seção real
    para evitar um chunk "órfão" sem contexto suficiente para o reranker.
    """
    raw_sections = _MARKDOWN_HEADING_SPLIT_RE.split(markdown_text)
    sections = [s.strip() for s in raw_sections if s.strip()]

    if not sections:
        return []

    INTRO_MERGE_THRESHOLD_CHARS = 100
    if (
        len(sections) > 1
        and not sections[0].lstrip().startswith("##")
        and len(sections[0]) < INTRO_MERGE_THRESHOLD_CHARS
    ):
        sections = [f"{sections[0]}\n\n{sections[1]}", *sections[2:]]

    return sections


def _enforce_max_chunk_size(sections: list[str], max_chars: int) -> list[str]:
    """
    Rede de segurança aprimorada: garante que textos nunca sejam cortados e perdidos.
    """
    result: list[str] = []
    for section in sections:
        if len(section) <= max_chars:
            result.append(section)
            continue

        paragraphs = section.split("\n\n")
        buffer = ""
        for paragraph in paragraphs:
            if len(paragraph) > max_chars:
                if buffer:
                    result.append(buffer.strip())
                    buffer = ""
                for i in range(0, len(paragraph), max_chars):
                    result.append(paragraph[i:i+max_chars].strip())
                continue

            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            if len(candidate) > max_chars and buffer:
                result.append(buffer.strip())
                buffer = paragraph
            else:
                buffer = candidate
        if buffer.strip():
            result.append(buffer.strip())

    return result


def _extract_ue_version(path_str: str) -> str:
    """
    Extrai um padrão "X.Y" (ou "X.Y.Z") do caminho informado — cobre
    convenções comuns como ".../UE_5.8/Engine/..." ou ".../5.8/Source/...".
    É uma heurística best-effort; cai no fallback configurável quando não
    encontra nada.
    """
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
    """
    Aplica o chunking estrutural + rede de segurança e monta o payload
    final de cada chunk, já no formato esperado pelo Qdrant (compatível
    com o schema RetrievedChunk consumido por rag_service.py).

    HYBRID RAG — PAYLOAD ENRIQUECIDO:
        `raw_content` é o conteúdo ORIGINAL e INTEGRAL do arquivo-fonte já
        limpo (o header C++ inteiro no Canal A, ou o Markdown .udn inteiro
        já sanitizado no Canal B) — não é fatiado junto com `markdown_text`.
        Ele é replicado em TODOS os chunks gerados a partir deste arquivo,
        de propósito: cada chunk continua sendo uma unidade de busca
        semântica independente (via `text`), mas qualquer um deles, ao ser
        recuperado, carrega consigo a referência bruta completa para
        validação sintática em rag_service.py. `raw_content` NUNCA é
        passado para embed_text() — apenas `text` é embedado.

        Trade-off consciente: para arquivos grandes com muitas seções
        Markdown, isso duplica `raw_content` entre os chunks do mesmo
        arquivo (mais espaço em disco no Qdrant, mais tokens no prompt
        final quando vários chunks do mesmo arquivo são recuperados
        simultaneamente). Se isso se tornar um problema de custo/latência,
        uma evolução natural seria armazenar `raw_content` uma única vez
        por `source_file` (ex: numa coleção/tabela auxiliar) em vez de
        replicá-lo por chunk.
    """
    sections = _split_by_markdown_headers(markdown_text)
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
                "description": "",  # mantém compatibilidade com RetrievedChunk
                "raw_content": raw_content,
            }
        )
    return chunks


# =============================================================================
# CONFIGURAÇÃO IDEMPOTENTE DO QDRANT
# =============================================================================

async def ensure_collection_exists(qdrant: AsyncQdrantClient) -> None:
    """
    Garante que a coleção configurada em settings.qdrant_collection existe,
    criando-a automaticamente (vetores de tamanho settings.embedding_dim,
    distância COSINE) caso ainda não exista. Idempotente: chamadas
    repetidas em uma coleção já existente são no-ops seguros.
    """
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
# EMBEDDING EM LOTE + UPSERT
# =============================================================================

async def _flush_batch(
    qdrant: AsyncQdrantClient,
    collection_name: str,
    batch: list[dict],
) -> int:
    """
    Gera embeddings para um lote de chunks (em paralelo, limitado por
    settings.ingestion_embedding_concurrency para não sobrecarregar um
    Ollama local rodando em hardware de desenvolvimento) e faz UM único
    upsert no Qdrant para o lote inteiro — minimiza chamadas de rede.

    IDs determinísticos (uuid5 a partir de source_file + chunk_id): uma
    reingestão da mesma pasta SOBRESCREVE os pontos existentes em vez de
    duplicá-los, estendendo a idempotência também ao nível do chunk.

    Falha no embedding de um chunk individual não derruba o lote inteiro
    — o chunk problemático é pulado e logado; o restante segue normalmente.
    """
    if not batch:
        return 0

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
        log.warning(
            "Lote descartado: todos os %d embeddings falharam neste lote.",
            len(batch),
        )
        return 0

    try:
        await qdrant.upsert(collection_name=collection_name, points=points, wait=True)
    except Exception as exc:
        log.error(
            "Falha no upsert do lote (%d pontos) na coleção '%s': %s",
            len(points),
            collection_name,
            exc,
        )
        return 0

    log.info(
        "Batch inserido com sucesso | %d pontos | coleção=%s",
        len(points),
        collection_name,
    )
    return len(points)


# =============================================================================
# ORQUESTRADOR PRINCIPAL — entrypoint chamado pelo BackgroundTasks
# =============================================================================

async def process_folder_ingestion(
    folder_path: str,
    content_type: str,
    qdrant: AsyncQdrantClient,
    ue_version_override: str | None = None,
) -> None:
    """
    Executa o pipeline de ETL Code-to-Doc completo para uma pasta.

    Roda como BackgroundTask — não tem valor de retorno consumido por
    ninguém, então toda a comunicação de progresso/resultado acontece via
    logs estruturados em nível INFO (arquivos encontrados, progresso por
    arquivo, lotes inseridos) e ERROR (falhas pontuais, sem propagar).

    Robustez: a falha catastrófica em UM arquivo (ex: header malformado,
    timeout da LLM documentadora) é capturada e logada, e o loop continua
    para o próximo arquivo — a pasta inteira nunca é abortada por causa de
    um único arquivo problemático.
    """
    root = Path(folder_path)
    collection_name = settings.qdrant_collection
    batch_size = settings.ingestion_batch_size

    log.info(
        "Ingestão iniciada | pasta=%s tipo=%s coleção=%s",
        root,
        content_type,
        collection_name,
    )

    try:
        await ensure_collection_exists(qdrant)
    except Exception as exc:
        log.error(
            "Falha catastrófica ao garantir a coleção '%s': %s. Abortando ingestão.",
            collection_name,
            exc,
            exc_info=True,
        )
        return

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
    stats = {"processed": 0, "skipped": 0, "errors": 0, "chunks_indexed": 0}

    for i, file_path in enumerate(files, start=1):
        log.info("[%d/%d] Processando arquivo: %s", i, total_files, file_path)
        try:
            if content_type == "source_code":
                chunks = await _process_header_file(file_path, ue_version)
            else:
                chunks = await _process_udn_file(file_path, ue_version)

            if not chunks:
                stats["skipped"] += 1
                continue

            stats["processed"] += 1
            pending.extend(chunks)

            # Esvazia o buffer em lotes de tamanho batch_size sempre que
            # atinge (ou ultrapassa) o limite — loop, não "if", porque um
            # único arquivo grande pode gerar mais chunks que um lote inteiro.
            while len(pending) >= batch_size:
                inserted = await _flush_batch(qdrant, collection_name, pending[:batch_size])
                stats["chunks_indexed"] += inserted
                pending = pending[batch_size:]

        except Exception as exc:
            stats["errors"] += 1
            log.error(
                "Erro ao processar '%s' — pulando para o próximo arquivo: %s",
                file_path,
                exc,
                exc_info=True,
            )
            continue

    # Flush final do que restou no buffer (menor que batch_size).
    if pending:
        inserted = await _flush_batch(qdrant, collection_name, pending)
        stats["chunks_indexed"] += inserted

    log.info(
        "Ingestão concluída | pasta=%s arquivos_encontrados=%d processados=%d "
        "ignorados=%d erros=%d chunks_indexados=%d",
        root,
        total_files,
        stats["processed"],
        stats["skipped"],
        stats["errors"],
        stats["chunks_indexed"],
    )