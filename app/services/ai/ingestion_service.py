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

    TRADE-OFF CONSCIENTE — ordem "deletar órfãos antes de reprocessar":
        Para um arquivo cujo hash mudou, os chunks antigos são removidos
        do Qdrant (filtro por `source_file`) ANTES de a nova versão ser
        processada e re-inserida. Isso evita definitivamente chunks órfãos
        (ex: a versão antiga gerava 5 seções/chunks, a nova gera 3 — os 2
        excedentes nunca seriam sobrescritos pelos IDs determinísticos
        uuid5, pois esses IDs dependem do índice do chunk). O preço é uma
        janela transitória em que, se o reprocessamento falhar (timeout de
        LLM, etc.), o arquivo fica temporariamente SEM chunks no Qdrant.
        O manifesto não é atualizado nesse cenário (o hash antigo é
        mantido ou removido, nunca o novo), então a próxima execução
        detecta a divergência de hash novamente e tenta o ciclo completo
        de remoção+reprocessamento uma vez mais — o sistema se autocorrige,
        mas a lacuna de retrieval para aquele arquivo específico persiste
        até a próxima ingestão bem-sucedida.

    LIMITAÇÃO CONHECIDA — custo de I/O do hash: o SHA-256 é calculado a
    partir dos bytes crus do arquivo, o que exige ler o arquivo inteiro do
    disco mesmo quando ele acaba sendo pulado por já estar no manifesto.
    Uma evolução futura natural seria um pré-filtro barato por
    st_size/st_mtime antes do hash — fora do escopo desta entrega, que
    prioriza a garantia de correção (detectar QUALQUER mudança de
    conteúdo) sobre o menor custo possível de I/O.
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

from app.core.config import settings
from app.db.redis import ingestion_manifest_key
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


async def _read_file_bytes(path: Path) -> bytes:
    """
    Leitura de arquivo em modo BINÁRIO, em thread separada. É a ÚNICA
    leitura de disco por arquivo: os mesmos bytes crus servem tanto para
    o hash SHA-256 do manifesto de checkpoint (identidade de conteúdo,
    calculada ANTES de qualquer decodificação/limpeza) quanto — via
    `.decode(..., errors="ignore")` no chamador — como fonte do texto
    usado no processamento semântico (Canal A/B). Evita ler o mesmo
    arquivo duas vezes do disco.
    """
    return await asyncio.to_thread(path.read_bytes)


def _compute_file_hash(raw_bytes: bytes) -> str:
    """
    SHA-256 (hexdigest) do conteúdo bruto de um arquivo — a identidade de
    conteúdo usada como valor no manifesto de checkpoint `manifest:{collection}`.
    Calculado a partir dos bytes crus para detectar qualquer alteração
    binária real do arquivo-fonte, independentemente de como o texto é
    posteriormente decodificado para envio à LLM documentadora ou ao
    sanitizador .udn.
    """
    return hashlib.sha256(raw_bytes).hexdigest()


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


async def _process_header_file(path: Path, raw: str, ue_version: str) -> list[dict]:
    """
    Processa um único header .h: filtra, limpa, envia para a LLM
    documentadora e quebra o Markdown resultante em chunks com metadados.

    `raw` já vem lido e decodificado do orquestrador (process_folder_ingestion),
    que reaproveita os mesmos bytes usados para o hash do checkpoint —
    este método não faz I/O de disco.

    Retorna lista vazia (sem levantar exceção) se o arquivo não for
    relevante — isso NÃO é um erro, é o filtro de ruído funcionando.
    """
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


async def _process_udn_file(path: Path, raw: str, ue_version: str) -> list[dict]:
    """
    Processa um único arquivo .udn: sanitização via regex (sem LLM — é
    documentação oficial, já bem escrita, só precisa de limpeza estrutural)
    seguida de chunking estrutural e enriquecimento de metadados.

    `raw` já vem lido e decodificado do orquestrador (process_folder_ingestion),
    que reaproveita os mesmos bytes usados para o hash do checkpoint —
    este método não faz I/O de disco.
    """
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
# CHECKPOINT / MANIFESTO DE INGESTÃO (idempotência via Redis)
# =============================================================================

@dataclass
class _PendingFileCommit:
    """
    Estado de commit pendente de UM arquivo cujos chunks já foram gerados
    e colocados no buffer compartilhado `pending`, mas que ainda não
    tiveram TODOS os seus chunks confirmados como persistidos no Qdrant.

    Enquanto `confirmed_chunks < total_chunks`, o arquivo está "em voo":
    já foi processado (chamada à LLM documentadora ou sanitização já
    ocorreu), mas o manifesto no Redis NÃO pode ser atualizado, porque
    ainda não há garantia de que 100% dos seus chunks sobreviveram a um
    upsert bem-sucedido no Qdrant — é justamente essa distinção entre
    "gerado" e "confirmado" que torna o checkpoint seguro mesmo quando um
    lote mistura chunks de vários arquivos diferentes.
    """

    relative_key: str
    file_hash: str
    total_chunks: int
    confirmed_chunks: int = 0


async def _load_manifest_cache(redis: Redis, manifest_key: str) -> dict[str, str]:
    """
    Carrega o manifesto inteiro de uma coleção em uma única chamada
    (HGETALL) para servir como cache de leitura local durante toda a
    ingestão — evita um round-trip de rede por arquivo em pastas com
    dezenas de milhares de arquivos. As ESCRITAS de checkpoint continuam
    granulares (HSET por arquivo, feitas em `_commit_confirmed_files`),
    apenas a leitura inicial é agregada.

    Falha ao contatar o Redis não aborta a ingestão: degrada de forma
    segura para "nenhum arquivo está no manifesto" (todo arquivo é tratado
    como novo), o que na pior das hipóteses causa reprocessamento
    desnecessário — nunca perda de dados.
    """
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
            "cache — todo arquivo será tratado como novo (nenhum dado é "
            "perdido, apenas reprocessamento desnecessário nesta execução).",
            manifest_key,
            exc,
            exc_info=True,
        )
        return {}


async def _delete_file_chunks(
    qdrant: AsyncQdrantClient,
    collection_name: str,
    source_file: str,
) -> None:
    """
    Remove do Qdrant TODOS os pontos cujo payload `source_file` corresponda
    ao arquivo informado. Chamado antes de reprocessar um arquivo cujo hash
    mudou desde a última ingestão (detectado via manifesto), para eliminar
    chunks órfãos da versão anterior — por exemplo, se a versão antiga
    gerava 5 seções/chunks e a nova gera apenas 3, os 2 chunks excedentes
    da versão antiga NUNCA seriam sobrescritos pelos IDs determinísticos
    (uuid5 de `source_file::chunk_id`) da nova versão, pois esses IDs
    dependem do índice do chunk dentro do arquivo.

    Propaga a exceção em caso de falha (não a engole) — o chamador deve
    tratar isso como falha de processamento do arquivo (stats["errors"]) e
    NÃO atualizar o manifesto, garantindo que o ciclo de
    remoção+reprocessamento seja tentado novamente na próxima execução.
    """
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
        "Chunks antigos removidos (arquivo modificado) | arquivo=%s status=%s",
        source_file,
        result.status,
    )


async def _commit_confirmed_files(
    redis: Redis,
    manifest_key: str,
    tracking: dict[str, _PendingFileCommit],
    per_file_counts: dict[str, int],
) -> None:
    """
    Aplica os créditos de chunks confirmados (retornados por `_flush_batch`
    após um upsert bem-sucedido no Qdrant) ao rastreador de cada arquivo em
    voo, e faz o "commit" do manifesto de checkpoint (HSET) para todo
    arquivo cujo total de chunks gerados já foi 100% persistido.

    ESTA é a fronteira de atomicidade do sistema: o Redis só é atualizado
    aqui, depois que `_flush_batch` confirma (via `wait=True`) que o Qdrant
    persistiu os pontos. Se o processo morrer a qualquer momento antes
    desta função rodar para um dado arquivo, esse arquivo simplesmente não
    é commitado — nunca fica um estado parcial mentiroso no manifesto.

    Uma falha ao escrever o HSET (ex: Redis momentaneamente indisponível)
    é logada e NÃO interrompe a ingestão: os dados já estão salvos no
    Qdrant, apenas o checkpoint fica pendente, e o arquivo será
    (redundantemente, mas corretamente) reprocessado na próxima execução.
    """
    for source_file, confirmed in per_file_counts.items():
        tracker = tracking.get(source_file)
        if tracker is None:
            # Chunk pertence a um arquivo que não está mais sendo
            # rastreado (ex: já foi commitado por um flush anterior, ou o
            # arquivo teve seu processamento abortado por exceção). Não há
            # nada a fazer aqui — apenas ignora.
            continue

        tracker.confirmed_chunks += confirmed
        if tracker.confirmed_chunks < tracker.total_chunks:
            continue

        try:
            await redis.hset(manifest_key, tracker.relative_key, tracker.file_hash)
        except Exception as exc:
            log.error(
                "Falha ao commitar checkpoint para '%s' (chunks JÁ persistidos "
                "no Qdrant, mas o manifesto não foi atualizado — o arquivo "
                "será reprocessado na próxima ingestão): %s",
                tracker.relative_key,
                exc,
                exc_info=True,
            )
            continue

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

    Returns:
        Um dict {source_file: quantidade_de_chunks_efetivamente_upsertados}
        para ESTE lote. É a granularidade exigida pelo checkpoint: como um
        lote pode misturar chunks de N arquivos diferentes, o chamador
        (process_folder_ingestion) precisa saber exatamente quantos chunks
        de CADA arquivo sobreviveram, para só commitar o manifesto quando
        o total de um arquivo específico bater 100%. Se o upsert do lote
        falhar como um todo, o dict retornado é vazio — nenhum arquivo
        recebe crédito algum, o que é correto: não podemos confirmar a
        persistência de nada nesse lote.
    """
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
        log.warning(
            "Lote descartado: todos os %d embeddings falharam neste lote.",
            len(batch),
        )
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

    # O upsert é atômico do ponto de vista do chamador: ou toda a lista
    # `points` foi persistida (chegamos aqui sem exceção), ou nenhuma foi
    # (capturado no except acima). Por isso é seguro creditar TODOS os
    # pontos da lista aos seus respectivos arquivos.
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
    Executa o pipeline de ETL Code-to-Doc completo para uma pasta, com
    checkpoint/idempotência via manifesto no Redis (ver NOTA DE
    ARQUITETURA no topo do módulo).

    Roda como BackgroundTask — não tem valor de retorno consumido por
    ninguém, então toda a comunicação de progresso/resultado acontece via
    logs estruturados em nível INFO (arquivos encontrados, progresso por
    arquivo, lotes inseridos, checkpoints commitados) e ERROR (falhas
    pontuais, sem propagar).

    Robustez: a falha catastrófica em UM arquivo (ex: header malformado,
    timeout da LLM documentadora, falha ao limpar órfãos no Qdrant) é
    capturada e logada, e o loop continua para o próximo arquivo — a pasta
    inteira nunca é abortada por causa de um único arquivo problemático.
    Nesses casos, o manifesto simplesmente não é atualizado para o arquivo
    afetado, garantindo que ele seja retentado na próxima execução.
    """
    root = Path(folder_path)
    collection_name = settings.qdrant_collection
    batch_size = settings.ingestion_batch_size
    manifest_key = ingestion_manifest_key(collection_name)

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
            "Falha catastrófica ao garantir a coleção '%s': %s. Abortando ingestão.",
            collection_name,
            exc,
            exc_info=True,
        )
        return

    manifest_cache = await _load_manifest_cache(redis, manifest_key)

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
        "processed": 0,        # arquivos (re)processados nesta execução (novos ou modificados)
        "modified": 0,         # subconjunto de 'processed': já existiam no manifesto com hash diferente
        "skipped": 0,          # ignorados: checkpoint (hash idêntico) OU sem conteúdo semântico relevante
        "errors": 0,           # falhas de leitura, limpeza de órfãos, chamada de LLM ou parsing
        "chunks_indexed": 0,   # total de chunks efetivamente upsertados no Qdrant nesta execução
        "partial_failures": 0, # processados mas NÃO commitados no manifesto (persistência incompleta)
    }

    for i, file_path in enumerate(files, start=1):
        log.info("[%d/%d] Avaliando arquivo: %s", i, total_files, file_path)

        try:
            raw_bytes = await _read_file_bytes(file_path)
        except Exception as exc:
            stats["errors"] += 1
            log.error(
                "Falha ao ler '%s' do disco — pulando para o próximo arquivo: %s",
                file_path,
                exc,
                exc_info=True,
            )
            continue

        file_hash = _compute_file_hash(raw_bytes)

        try:
            relative_key = file_path.relative_to(root).as_posix()
        except ValueError:
            # Defensivo: só ocorreria se `file_path` não estivesse de fato
            # sob `root` (ex: symlink incomum). Cai para o caminho absoluto
            # como chave em vez de abortar o arquivo.
            relative_key = file_path.as_posix()
            log.warning(
                "Não foi possível calcular caminho relativo de '%s' em relação "
                "a '%s' — usando caminho absoluto como chave do manifesto.",
                file_path,
                root,
            )

        source_file_str = str(file_path)
        existing_hash = manifest_cache.get(relative_key)

        # --- Checkpoint: arquivo inalterado desde a última ingestão -----
        if existing_hash == file_hash:
            stats["skipped"] += 1
            log.info(
                "[%d/%d] Ignorado — checkpoint OK (hash inalterado): %s",
                i,
                total_files,
                file_path,
            )
            continue

        is_modification = existing_hash is not None

        try:
            # --- Arquivo modificado: limpa órfãos ANTES de reprocessar ---
            if is_modification:
                log.info(
                    "Arquivo modificado detectado (hash difere do manifesto) — "
                    "removendo chunks antigos antes de reindexar: %s",
                    file_path,
                )
                await _delete_file_chunks(qdrant, collection_name, source_file_str)
                stats["modified"] += 1

            raw_text = raw_bytes.decode("utf-8", errors="ignore")

            if content_type == "source_code":
                chunks = await _process_header_file(file_path, raw_text, ue_version)
            else:
                chunks = await _process_udn_file(file_path, raw_text, ue_version)

            if not chunks:
                stats["skipped"] += 1
                continue

            stats["processed"] += 1
            tracking[source_file_str] = _PendingFileCommit(
                relative_key=relative_key,
                file_hash=file_hash,
                total_chunks=len(chunks),
            )
            pending.extend(chunks)

            # Esvazia o buffer em lotes de tamanho batch_size sempre que
            # atinge (ou ultrapassa) o limite — loop, não "if", porque um
            # único arquivo grande pode gerar mais chunks que um lote inteiro.
            while len(pending) >= batch_size:
                batch, pending = pending[:batch_size], pending[batch_size:]
                per_file_counts = await _flush_batch(qdrant, collection_name, batch)
                stats["chunks_indexed"] += sum(per_file_counts.values())
                await _commit_confirmed_files(redis, manifest_key, tracking, per_file_counts)

        except Exception as exc:
            stats["errors"] += 1
            # Descarta qualquer rastreamento parcial deste arquivo: como o
            # manifesto mantém o hash antigo (ou nenhum), a próxima
            # execução detectará a divergência de hash novamente e tentará
            # o ciclo completo de remoção+reprocessamento uma vez mais.
            tracking.pop(source_file_str, None)
            log.error(
                "Erro ao processar '%s' — pulando para o próximo arquivo: %s",
                file_path,
                exc,
                exc_info=True,
            )
            continue

    # Flush final do que restou no buffer (menor que batch_size).
    if pending:
        per_file_counts = await _flush_batch(qdrant, collection_name, pending)
        stats["chunks_indexed"] += sum(per_file_counts.values())
        await _commit_confirmed_files(redis, manifest_key, tracking, per_file_counts)

    # Qualquer arquivo que ainda reste em `tracking` neste ponto teve
    # persistência PARCIAL (ex: falha de embedding em algum chunk, ou
    # falha do upsert do lote correspondente) — nunca atingiu 100% dos
    # seus chunks confirmados, portanto o manifesto NUNCA foi atualizado
    # para ele. Será reprocessado integralmente na próxima ingestão.
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
        "modificados=%d ignorados=%d erros=%d parciais=%d chunks_indexados=%d",
        root,
        total_files,
        stats["processed"],
        stats["modified"],
        stats["skipped"],
        stats["errors"],
        stats["partial_failures"],
        stats["chunks_indexed"],
    )