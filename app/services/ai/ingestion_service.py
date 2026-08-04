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
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpcore
import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse, ResponseHandlingException
from redis.asyncio import Redis
from redis.asyncio.lock import Lock

from app.core.config import settings
from app.core.domains.registry import domain_registry
from app.db.redis import ingestion_failures_key, ingestion_lock_key, ingestion_manifest_key
from app.services.ai.embedding_service import embed_text
from app.services.ai.ingestion.base_strategy import ChunkCandidate
from app.services.ai.ingestion.godot4_strategy import Godot4IngestionStrategy
from app.services.ai.rag_service import call_litellm
from app.services.ai.sparse_vector_service import SPARSE_VECTOR_NAME, build_sparse_vector

log = logging.getLogger(__name__)

# =============================================================================
# CORREÇÃO (pipeline duplo — auditoria RAG): unificação com a strategy nova
# =============================================================================
# app/services/ai/ingestion/godot4_strategy.py + base_strategy.py implementam
# um IngestionStrategy completo e domain-aware para Godot 4 (doc_types
# corretos por extensão, canal de Class Reference via XML, sanitização e
# extração de metadados delegadas a GODOT4_PROFILE) — mas até esta correção
# ele nunca era importado ou chamado por este módulo. O pipeline "real" era
# um dispatch legado hardcoded por extensão, herdado do canal original de
# UE5 e parcialmente adaptado para Godot com bugs (ver Correções 1-3 desta
# auditoria: domain_id/doc_type hardcoded e leitura de payload assimétrica).
#
# A partir daqui, `domain_id == "godot_4"` é roteado INTEIRAMENTE para
# Godot4IngestionStrategy — ela decide o doc_type, sanitização e parsing por
# extensão (inclusive .xml/.tres/.h/.gen.cpp, que o pipeline legado nunca
# descobria). O dispatch legado abaixo (_process_script_file/_process_
# tscn_file/_process_md_or_rst_file) passa a servir EXCLUSIVAMENTE UE5 —
# elimina a divergência de nomenclatura de doc_type e a necessidade de
# propagar domain_id manualmente para o canal Godot (a strategy já é
# domain-locked por construção).
_GODOT4_STRATEGY = Godot4IngestionStrategy()


def _chunk_candidates_to_payload_dicts(
    candidates: list[ChunkCandidate],
    project_source: str,
) -> list[dict]:
    """
    Converte a saída de um IngestionStrategy (ChunkCandidate — ver
    base_strategy.py) para o dict de payload que `_flush_batch` e o resto
    do pipeline genérico de embedding/checkpoint/upsert já esperam. Este é
    o ponto de integração entre a strategy nova (parsing por domínio) e a
    orquestração genérica existente (batching, retry, backoff, manifesto de
    checkpoint no Redis) — exatamente o ponto que o docstring de
    base_strategy.py descrevia como pendente.

    chunk_id é atribuído sequencialmente por arquivo (0..N-1), na ordem em
    que a strategy produziu os candidatos. Diferente do pipeline legado
    (que usava sufixo sintético "#groupN" em source_file para os Canais A/C
    de UE5), aqui todos os candidatos de um mesmo arquivo compartilham o
    MESMO `source_file` — a combinação (source_file, chunk_id) já garante
    um point_id determinístico único (ver uuid5 em _flush_batch), sem
    precisar de sufixo.
    """
    result: list[dict] = []
    for chunk_id, candidate in enumerate(candidates):
        result.append(
            {
                "summary_markdown": candidate.summary_markdown,
                "source_file": candidate.source_file,
                "source_file_root": candidate.source_file,
                "doc_type": candidate.doc_type,
                "title": candidate.title,
                "chunk_id": chunk_id,
                # Strategy é domain-locked para "godot_4" — a chave de
                # versão correta nunca precisa ser decidida em runtime aqui.
                "godot_version": candidate.engine_version,
                "raw_content": candidate.raw_content,
                "project_source": project_source,
                # Metadados estruturais específicos do domínio (class_name,
                # signals, nodes, tutoriais oficiais etc.) — nunca embedados,
                # disponíveis como payload extra para filtros futuros.
                "extra_metadata": candidate.extra_metadata,
            }
        )
    return result


# =============================================================================
# CONSTANTES — Filtros do motor de duplo canal e Chunking
# =============================================================================

MIN_BODY_CHARS: int = 100  # Quantidade mínima de caracteres de corpo para emitir um chunk isolado

_IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "ThirdParty",
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

# CORREÇÃO (ponto cego de indexação — subsistemas modernos de gameplay):
# uma auditoria direta no Qdrant (busca por pontos do módulo
# "NetworkPredictionExtras" retornando vazio) expôs que a poda de 'Private'
# acima, combinada com a allowlist estrita de _GAMEPLAY_KEYWORDS logo
# abaixo, formava um ponto cego duplo para os novos ecossistemas de
# movimentação/rede/física da UE5 (Mover, Network Prediction, Chaos
# Mover/Vehicles, Motion Warping, Parametric/Procedural Motion): eles
# dependem fortemente de structs auxiliares, controllers e lógica de
# simulação de baixo nível que (a) frequentemente moram em Private/ — que
# era podado incondicionalmente do os.walk, então nem CHEGAVAM a ser
# avaliados — e (b) muitas vezes não herdam de UActorComponent/AActor nem
# têm as palavras-chave legadas no nome, então mesmo quando alcançados
# caíam fora da allowlist.
#
# _STRATEGIC_ENGINE_MODULE_MARKERS resolve os dois lados: é uma lista de
# fragmentos de caminho (checados por substring, case-insensitive, contra
# o path POSIX completo) que identifica os módulos de runtime de gameplay
# moderno da Engine. Ela é usada em três pontos:
#   1. _walk_files: suspende a poda de 'Private' especificamente para
#      diretórios dentro de um desses módulos (nunca globalmente — outras
#      pastas Private/ThirdParty/Binaries continuam podadas normalmente,
#      preservando a proteção contra explosão de escopo).
#   2. _is_gameplay_relevant_header: bypassa a allowlist de
#      _GAMEPLAY_KEYWORDS para headers desses módulos, mesmo em modo
#      Engine (sample_mode=False).
#   3. _run_ingestion: permite que .cpp desses módulos específicos também
#      sejam descobertos e roteados ao Canal C em modo Engine (que antes
#      só existia em sample_mode).
#
# NOTA: os fragmentos abaixo refletem o layout padrão de plugins/módulos
# da Epic nas versões recentes da UE5; como a árvore de diretórios pode
# variar um pouco entre versões da Engine, vale conferir/ajustar esta
# lista contra a instalação local antes de rodar uma ingestão completa da
# Engine (é só uma tupla de strings — adicionar ou remover fragmentos não
# afeta nenhum outro mecanismo do pipeline).
_STRATEGIC_ENGINE_MODULE_MARKERS: tuple[str, ...] = (
    # Mover — framework de movimentação baseado em simulação
    # determinística, sucessor do CharacterMovementComponent clássico.
    "Plugins/Runtime/Mover",
    "Plugins/Experimental/Mover",
    # Network Prediction — framework genérico de simulação predita/
    # reconciliada em rede. "NetworkPredictionExtras" é coberto pelo
    # próprio prefixo "NetworkPrediction" abaixo (é o módulo citado na
    # auditoria original que motivou esta correção).
    "Plugins/Runtime/NetworkPrediction",
    "Plugins/Experimental/NetworkPrediction",
    # Chaos Vehicles / Chaos Mover — física veicular e de movimentação de
    # nova geração construída sobre o motor Chaos.
    "Plugins/Runtime/ChaosVehicles",
    "Plugins/Runtime/ChaosVehicleComponents",
    "Plugins/Runtime/ChaosMover",
    "Plugins/Experimental/ChaosMover",
    # Núcleo experimental do Chaos (baixo nível de simulação física) que
    # sustenta os módulos acima.
    "Source/Runtime/Experimental/Chaos",
    "Source/Runtime/PhysicsCore",
    # Motion Warping e movimentação paramétrica/procedural de animação.
    "Plugins/Animation/MotionWarping",
    "Plugins/Runtime/ParametricMotion",
)


def _is_strategic_gameplay_path(path: Path) -> bool:
    """
    True se `path` (arquivo OU diretório) pertence a um dos módulos de
    runtime de gameplay moderno listados em
    _STRATEGIC_ENGINE_MODULE_MARKERS. Ver o comentário acima da constante
    para os três pontos do pipeline que consultam esta função.
    """
    posix_lower = path.as_posix().lower()
    return any(marker.lower() in posix_lower for marker in _STRATEGIC_ENGINE_MODULE_MARKERS)


# =============================================================================
# KEYWORDS DE GAMEPLAY — Godot 4
# =============================================================================
# Classes fundamentais do Godot 4 para relevância de gameplay.
# Usado pelo filtro de headers/scripts em _is_gameplay_relevant_header.
_GAMEPLAY_KEYWORDS: tuple[str, ...] = (
    # Classes base fundamentais
    "Node",
    "Node2D",
    "Node3D",
    "Control",
    "Resource",
    "RefCounted",
    "CanvasItem",
    # Gameplay & Física
    "CharacterBody2D",
    "CharacterBody3D",
    "RigidBody2D",
    "RigidBody3D",
    "Area2D",
    "Area3D",
    "CollisionShape2D",
    "CollisionShape3D",
    "RayCast2D",
    "RayCast3D",
    "Camera2D",
    "Camera3D",
    # Arquitetura/Sistemas
    "Signal",
    "State",
    "StateMachine",
    "AStar2D",
    "AStar3D",
    "NavigationAgent2D",
    "NavigationAgent3D",
    "AnimationPlayer",
    "AnimationTree",
    # Terminologia comum de scripts GDScript/C#
    "_ready",
    "_process",
    "_physics_process",
    "_input",
    "@export",
    "@onready",
)

_CLASS_DECL_RE = re.compile(
    r"class\s+(?:[A-Z][A-Z0-9_]*_API\s+)?(\w+)\s*:\s*"
    r"((?:public|protected|private)\s+\w+(?:\s*,\s*(?:public|protected|private)\s+\w+)*)"
)

_INCLUDE_RE = re.compile(r"^\s*#include\s+.*$", re.MULTILINE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

# =============================================================================
# Constantes — Sanitização de documentação Godot (.md, .rst)
# =============================================================================
# Remove diretivas Sphinx/RST que não são conteúdo legível
_RST_DIRECTIVE_RE = re.compile(
    r"^\s*\.\.\s+(?:code-block|include|literalinclude|toctree|autoclass|"
    r"autofunction|automethod|automodule|class|function|method|module|"
    r"attribute|property|exception|data|note|warning|tip|seealso|versionadded|"
    r"versionchanged|deprecated|ref|doc|download|image|figure|table|"
    r"contents|sectnum|raw|include|import)\s*::.*$",
    re.MULTILINE | re.IGNORECASE,
)

# Remove referências cruzadas RST (:ref:, :doc:, :class:, :func:, etc.)
_RST_ROLE_RE = re.compile(r":[a-z]+:`([^`]+)`")

# Remove metadados frontmatter YAML (--- ... ---) no topo de arquivos .md
_MD_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---", re.DOTALL)

# Remove links de estilo RST/Markdown preservando o texto
# [texto](url) -> texto, `texto <url>`_ -> texto
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RST_LINK_RE = re.compile(r"`([^`<]+)\s*<[^>]+>`_")

# Extrai título do primeiro heading Markdown (# Title)
_MD_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Extrai título do primeiro heading RST (Title\n=====)
_RST_TITLE_RE = re.compile(r"^(.+)\n[=-]{3,}$", re.MULTILINE)

_UE_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")

# Detecta assinaturas de definição de função GDScript: "func name(args) -> ReturnType:"
_GDSCRIPT_FUNC_SIG_RE = re.compile(
    r"^\s*func\s+(\w+)\s*\([^)]*\)\s*(?:->\s*\w+\s*)?:",
    re.MULTILINE,
)

# Detecta assinaturas de método C#: "public/private/protected/override async void/ReturnType MethodName("
_CSHARP_METHOD_SIG_RE = re.compile(
    r"^\s*(?:public|private|protected|internal|static|virtual|override|abstract|async|readonly)\s+"
    r"(?:[\w<>\[\],?]+\s+)+(\w+)\s*\([^;{]*\)\s*(?:{)?",
    re.MULTILINE,
)

# Tamanho mínimo de função isolada para valer a pena documentar sozinha
GDSCRIPT_MIN_FUNCTION_CHARS: int = 60
CSHARP_MIN_FUNCTION_CHARS: int = 80

# Limite de chars por grupo de funções enviado à LLM documentadora
GDSCRIPT_MAX_GROUP_CHARS: int = 5000
CSHARP_MAX_GROUP_CHARS: int = 6000

# =============================================================================
# Constantes — Canal D (.bp.json, Blueprints exportados via Unreal Python API)
# =============================================================================
# CORREÇÃO ARQUITETURAL: o Canal D deixou de depender de exportação em
# texto T3D + limpeza por regex. Um script Editor Utility rodando DENTRO da
# Unreal (ver BP_GraphExporter.py) agora percorre o EdGraph via API Python
# e exporta um JSON já estruturado (ParentClass, Variables,
# ExecutionFlow linearizado com branches nomeados). Isso elimina a
# necessidade de qualquer regex de limpeza aqui — o JSON já chega limpo,
# sem ruído visual de editor (NodePosX, GUIDs, PinIds nunca são
# exportados pelo script Unreal). O trabalho deste canal passa a ser só:
# validar o JSON, formatá-lo de volta como texto legível para o prompt, e
# documentar via LLM.

# Teto de caracteres do JSON formatado (após json.dumps) enviado à LLM
# documentadora — Blueprints com muitos Custom Events e fluxos profundos
# ainda podem gerar um JSON grande mesmo sem ruído de editor. Truncar aqui
# é sobre o texto SERIALIZADO, não sobre regex de limpeza (que não existe
# mais neste canal).
BP_JSON_MAX_CHARS: int = 8000  # Legado — não usado mais, mantido para compatibilidade
TSCN_MAX_CHARS: int = 8000  # Limite de chars para arquivos .tscn do Godot


# =============================================================================
# SYSTEM PROMPT — Documentador Técnico Sênior (Canal A)
# =============================================================================

_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Godot Engine Developer and Technical Architect, specialized \
in translating raw Godot 4 GDScript and C# scripts into clear, \
gameplay-focused educational documentation for game developers.

Your task: given a cleaned GDScript or C# file, produce a Markdown guide that \
explains HOW a developer uses this class in practice — not a line-by-line \
restatement of the code.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title (`# ClassName`) followed by a short \
paragraph explaining the class's purpose and where it fits in the Godot \
node hierarchy (e.g. "extends CharacterBody2D, used as the player controller \
for a 2D platformer").
3. Organize the rest of the content using H2 sections (`## `), for example: \
"## Exported Properties (@export)", "## Lifecycle Methods (_ready, _process, _physics_process)", \
"## Signals", "## Public Methods". Use H3 (`### `) for sub-topics inside a \
section when it helps clarity.
4. For every @export property, signal, and method you document, explain its \
practical effect in gameplay terms — not just its signature. Mention relevant \
annotations (@export_range, @onready, @rpc) only when they change how a \
developer interacts with the member.
5. Ignore purely structural code (pass statements, boilerplate comments) \
unless relevant to usage.
6. Be concise but complete — prioritize developer usefulness over \
exhaustive coverage of every single line.
7. Never invent members, functions or parameters that are not present in \
the provided script.\
"""


# =============================================================================
# UTILITÁRIOS DE I/O (despachados para thread)
# =============================================================================

def _walk_files(root: Path, extensions: tuple[str, ...]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _IGNORED_DIR_NAMES
            # CORREÇÃO (ponto cego de indexação — ver comentário em
            # _STRATEGIC_ENGINE_MODULE_MARKERS): 'Private' normalmente é
            # podado por estar em _IGNORED_DIR_NAMES, mas para os módulos
            # estratégicos de gameplay moderno (Mover, Network Prediction,
            # Chaos Mover/Vehicles, Motion Warping) é exatamente ali que
            # vive a lógica de simulação/predição de rede que o pipeline
            # precisa capturar. A exceção é escopada ao diretório
            # resultante (dirpath/d) — nunca global — então Private/
            # ThirdParty/Binaries/etc. continuam podados normalmente em
            # todo o resto da árvore.
            or (d == "Private" and _is_strategic_gameplay_path(Path(dirpath) / d))
        ]
        for fname in filenames:
            if fname.endswith(extensions):
                yield Path(dirpath) / fname


async def _discover_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
    return await asyncio.to_thread(lambda: list(_walk_files(root, extensions)))


async def _read_file_bytes(path: Path) -> bytes:
    return await asyncio.to_thread(path.read_bytes)


def _compute_file_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


# =============================================================================
# CANAL A — Códigos-fonte C++ (.h)
# =============================================================================

def _is_gameplay_relevant_script(
    content: str, filename: str, sample_mode: bool = False, strategic_override: bool = False
) -> tuple[bool, str]:
    """
    Decide se um script GDScript/C# vale a pena documentar.

    Modo padrão (sample_mode=False): allowlist estrita por palavra-chave
    no nome da classe ou conteúdo. Necessário porque o volume de scripts
    pode ser grande — sem filtro, o custo de LLM documentadora explode.

    Modo sample (sample_mode=True): a allowlist é DESLIGADA. Projetos de
    exemplo usam convenções próprias e seria impossível manter uma lista
    exaustiva. Delega-se a decisão de relevância à LLM documentadora.

    Para Godot 4, as keywords incluem classes fundamentais como Node,
    CharacterBody2D/3D, sinais, e métodos de lifecycle (_ready, _process).
    """
    if sample_mode or strategic_override:
        stem = Path(filename).stem
        return True, stem

    # Para GDScript: busca por 'extends', 'class_name' e keywords
    content_lower = content.lower()

    # Verifica extends/classe base
    extends_match = re.search(r"extends\s+(\w+)", content, re.IGNORECASE)
    if extends_match:
        base_class = extends_match.group(1)
        if any(kw.lower() in base_class.lower() for kw in _GAMEPLAY_KEYWORDS):
            return True, base_class

    # Verifica class_name
    class_match = re.search(r"class_name\s+(\w+)", content, re.IGNORECASE)
    if class_match:
        class_name = class_match.group(1)
        if any(kw.lower() in class_name.lower() for kw in _GAMEPLAY_KEYWORDS):
            return True, class_name

    # Verifica se contém lifecycle methods do Godot
    lifecycle_patterns = ["_ready", "_process", "_physics_process", "_input"]
    if any(pattern in content for pattern in lifecycle_patterns):
        stem = Path(filename).stem
        return True, stem

    # Verifica nome do arquivo
    stem = Path(filename).stem
    stem_lower = stem.lower()
    if any(kw.lower() in stem_lower for kw in _GAMEPLAY_KEYWORDS):
        return True, stem

    return False, ""


def _is_gameplay_relevant_header(
    content: str, filename: str, sample_mode: bool = False, strategic_override: bool = False
) -> tuple[bool, str]:
    """
    Decide se um header C++ (.h) da UE5 vale a pena documentar.

    Modo padrão (sample_mode=False): allowlist estrita por palavra-chave
    no nome da classe ou conteúdo. Necessário porque o volume de headers
    da Engine é enorme — sem filtro, o custo de LLM documentadora explode.

    Modo sample (sample_mode=True): a allowlist é DESLIGADA. Projetos de
    exemplo usam convenções próprias e seria impossível manter uma lista
    exaustiva. Delega-se a decisão de relevância à LLM documentadora.

    Para UE5, as keywords incluem classes de gameplay como AActor,
    APawn, ACharacter, UActorComponent, e macros como UPROPERTY/UFUNCTION.
    """
    if sample_mode or strategic_override:
        stem = Path(filename).stem
        return True, stem

    # Busca declaração de classe C++ com herança
    class_match = _CLASS_DECL_RE.search(content)
    if class_match:
        class_name = class_match.group(1)
        base_classes = class_match.group(2)

        # Verifica se a classe herda de algo relevante
        if any(kw in base_classes for kw in _GAMEPLAY_KEYWORDS):
            return True, class_name

        # Verifica se o nome da classe contém keyword
        if any(kw.lower() in class_name.lower() for kw in _GAMEPLAY_KEYWORDS):
            return True, class_name

    # Verifica macros de reflexão da UE5 no conteúdo
    ue_macros = ["UPROPERTY", "UFUNCTION", "UCLASS", "USTRUCT", "UPARAM", "UMETA"]
    if any(macro in content for macro in ue_macros):
        # Tenta extrair nome da classe mesmo sem herança detectada
        if class_match:
            return True, class_match.group(1)
        stem = Path(filename).stem
        return True, stem

    # Verifica nome do arquivo
    stem = Path(filename).stem
    stem_lower = stem.lower()
    if any(kw.lower() in stem_lower for kw in _GAMEPLAY_KEYWORDS):
        return True, stem

    return False, ""


def _clean_cpp_header(content: str) -> str:
    cleaned = _INCLUDE_RE.sub("", content)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


# =============================================================================
# CANAL C — Implementação C++ de mecânicas (.cpp), modo sample apenas
# =============================================================================
# NOTA DE ARQUITETURA — por que .cpp é um canal separado do Canal A (.h):
#     A API pública (assinatura, propriedades) responde "o que existe". A
#     LÓGICA DE COMO uma mecânica é implementada (ex: como o double jump é
#     decidido, como um grafo PCG gera pontos) vive na IMPLEMENTAÇÃO, no
#     .cpp. Para projetos de Engine isso é irrelevante (você quer a API
#     pública, não detalhes internos). Para projetos-sample de mecânica
#     (Lyra, City Sample, Vehicle Sample, etc.) é o inverso: a implementação
#     É o conteúdo de valor.
#
# NOTA DE LIMITAÇÃO CONHECIDA — o splitter abaixo é uma heurística por
#     regex + balanceamento de chaves, NÃO um parser C++ completo. Ele lida
#     bem com o caso comum (métodos definidos no escopo de namespace/arquivo
#     no formato "RetType UClasse::Metodo(...) { ... }"), mas pode errar em:
#     macros complexas que injetam chaves, template especializations muito
#     aninhadas, ou código gerado por outras macros que mascaram a
#     assinatura. Nesses casos o método problemático cai no "restante do
#     arquivo" (bucket final) em vez de virar um grupo isolado — não trava
#     a ingestão, mas pode diluir a qualidade da documentação daquele
#     método específico. Aceitável como trade-off custo/benefício frente a
#     escrever um parser C++ completo.

def _split_gdscript_into_functions(content: str) -> list[tuple[str, str]]:
    """
    Divide um arquivo GDScript (.gd) em blocos por função/método.

    GDScript usa indentação significativa, então o balanceamento é por
    nível de indentação, não por chaves.

    Returns:
        Lista de tuplas (nome_da_funcao, texto_do_bloco).
    """
    matches = list(_GDSCRIPT_FUNC_SIG_RE.finditer(content))
    if not matches:
        return [("arquivo completo (nenhuma função reconhecida)", content)]

    blocks: list[tuple[str, str]] = []
    lines = content.splitlines(keepends=True)

    for i, match in enumerate(matches):
        func_name = match.group(1)
        start_pos = match.start()

        # Encontra o fim da função baseado em indentação
        # A função termina quando encontramos uma linha com indentação menor ou igual
        # à da assinatura da função (ou EOF)
        start_line_idx = content[:start_pos].count('\n')

        # Pega a indentação da assinatura da função
        first_line = lines[start_line_idx] if start_line_idx < len(lines) else ""
        base_indent = len(first_line) - len(first_line.lstrip())

        end_line_idx = start_line_idx + 1
        while end_line_idx < len(lines):
            line = lines[end_line_idx]
            # Linha vazia não conta
            if line.strip() == "":
                end_line_idx += 1
                continue
            # Se a indentação é menor ou igual à base, a função terminou
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent and line.strip():
                break
            end_line_idx += 1

        # Extrai o texto da função
        func_text = "".join(lines[start_line_idx:end_line_idx])
        blocks.append((func_name, func_text))

    # Código antes da primeira função (variáveis de classe, extends, etc.)
    if matches:
        leading = content[: matches[0].start()].strip()
        if leading:
            # CORREÇÃO: título contextual em vez de string fixa genérica
            # Extrai o nome do arquivo do contexto (assumindo que será passado via closure ou contexto)
            # Por ora, usamos um placeholder que será substituído pelo chamador
            blocks.insert(0, ("__TOP_LEVEL__", leading))

    return blocks


def _split_csharp_into_functions(content: str) -> list[tuple[str, str]]:
    """
    Divide um arquivo C# (.cs) em blocos por método.

    Usa balanceamento de chaves para encontrar o fim de cada método.

    Returns:
        Lista de tuplas (nome_do_metodo, texto_do_bloco).
    """
    matches = list(_CSHARP_METHOD_SIG_RE.finditer(content))
    if not matches:
        return [("arquivo completo (nenhum método reconhecido)", content)]

    blocks: list[tuple[str, str]] = []
    cursor = 0

    for i, match in enumerate(matches):
        method_name = match.group(1)

        # Encontra a chave de abertura do método
        brace_start = content.find("{", match.start())
        if brace_start == -1:
            continue  # Método sem corpo (interface/abstract)

        # Balanceamento de chaves
        depth = 0
        end = brace_start
        for pos in range(brace_start, len(content)):
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        else:
            end = len(content)

        func_text = content[match.start():end]
        blocks.append((method_name, func_text))
        cursor = end

    # Código antes do primeiro método (using, namespace, class declaration)
    if matches:
        leading = content[: matches[0].start()].strip()
        if leading:
            # CORREÇÃO: título contextual em vez de string fixa genérica
            # Placeholder que será substituído pelo chamador com contexto do arquivo
            blocks.insert(0, ("__TOP_LEVEL__", leading))

    return blocks


def _split_script_into_functions(content: str, extension: str) -> list[tuple[str, str]]:
    """
    Dispatcher para dividir scripts em funções/métodos baseado na extensão.

    Args:
        content: Conteúdo do arquivo
        extension: Extensão do arquivo (.gd ou .cs)

    Returns:
        Lista de tuplas (nome, texto_do_bloco).
    """
    if extension == ".gd":
        return _split_gdscript_into_functions(content)
    elif extension == ".cs":
        return _split_csharp_into_functions(content)
    else:
        return [("arquivo completo", content)]


def _group_script_blocks(
    blocks: list[tuple[str, str]], max_group_chars: int
) -> list[tuple[list[str], str]]:
    """
    Agrupa blocos de função pequenos entre si (até max_group_chars) para
    evitar uma chamada de LLM por função trivial, mas nunca junta um bloco
    já maior que o limite com outro (esse vai sozinho, truncado se preciso
    pelo chamador).

    Returns:
        Lista de (nomes_das_funcoes_no_grupo, texto_concatenado).
    """
    groups: list[tuple[list[str], str]] = []
    current_names: list[str] = []
    current_text = ""

    for name, text in blocks:
        candidate = f"{current_text}\n\n{text}".strip() if current_text else text
        if len(candidate) > max_group_chars and current_text:
            groups.append((current_names, current_text))
            current_names, current_text = [name], text
        else:
            current_names.append(name)
            current_text = candidate

    if current_text:
        groups.append((current_names, current_text))

    return groups


_SCRIPT_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Godot Engine Developer and Technical Architect, specialized \
in explaining HOW Godot 4 gameplay mechanics are actually implemented, \
based on real GDScript or C# code from a shipped project.

Your task: given one or more GDScript/C# function implementations from a real \
project, produce a Markdown guide that explains the MECHANIC or SYSTEM \
being implemented — not a line-by-line restatement of the code.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title describing the MECHANIC (e.g. \
"# Player Movement with State Machine", "# Enemy AI Detection System") — \
not just the raw function name.
3. Explain the ACTUAL LOGIC: what triggers this code, what decisions it \
makes, what state it reads/writes, and how it connects to other nodes and \
signals in the scene tree.
4. Focus on Godot-specific patterns: node lifecycle (_ready, _process, \
_physics_process), signal connections, exported properties for inspector \
configuration, and scene instantiation.
5. Use H2 sections (`## `) to separate distinct concerns when the input \
covers more than one function, e.g. `## How it's triggered`, \
`## Core logic`, `## Signal connections`.
6. Never invent behavior not present in the provided code. If the logic is \
unclear or incomplete from what's given, say so explicitly rather than \
guessing.
7. Be concise but complete — prioritize helping a developer replicate or \
adapt this mechanic in their own Godot project.\
"""


# Orçamento de caracteres dedicado para o header injetado como contexto no
# Canal C — deliberadamente menor que ingestion_max_header_chars (que vale
# para o Canal A processando o header como documento PRINCIPAL). Aqui o
# header é só CONTEXTO de apoio; o conteúdo principal é o .cpp. Um teto
# menor evita que um header grande domine o prompt e empurre o orçamento
# total (header + grupo de funções) para além do limite de contexto do
# modelo documentador.
CPP_HEADER_CONTEXT_MAX_CHARS: int = 4000


def _find_sibling_header(cpp_path: Path) -> Path | None:
    """
    Localiza o .h correspondente a um .cpp: mesmo nome, mesma pasta.

    Convenção universal em código UE5 (Engine e projetos/samples) — não
    cobre o caso raro de headers em pasta `Public/` separada de `Private/`
    com o .cpp (alguns módulos de plugin usam esse layout); nesse caso o
    Canal C simplesmente segue sem contexto de header, degradando
    graciosamente em vez de falhar.
    """
    candidate = cpp_path.with_suffix(".h")
    if candidate.is_file():
        return candidate
    return None


async def _load_header_context(cpp_path: Path) -> str:
    """
    Lê e prepara o .h irmão de um .cpp para uso como contexto injetado no
    prompt do Canal C. Retorna string vazia se o header não existir ou não
    puder ser lido — nunca lança, porque a ausência de header não deve
    impedir a documentação da implementação em si (apenas a empobrece).

    CORREÇÃO (bloqueio de event loop): a leitura do arquivo é despachada
    para thread via asyncio.to_thread, assim como as demais operações de
    I/O de disco do módulo (ver NOTA DE PERFORMANCE no topo do arquivo).
    Antes, path.read_text() rodava direto na corrotina, bloqueando o loop
    compartilhado com /v1/chat uma vez por .cpp processado em sample_mode.
    """
    header_path = _find_sibling_header(cpp_path)
    if header_path is None:
        log.debug("Nenhum .h irmão encontrado para '%s' — Canal C seguirá sem contexto de header.", cpp_path)
        return ""

    try:
        raw_header = await asyncio.to_thread(
            header_path.read_text, encoding="utf-8", errors="ignore"
        )
    except OSError as exc:
        log.warning("Falha ao ler header irmão '%s' de '%s': %s — seguindo sem contexto.", header_path, cpp_path, exc)
        return ""

    cleaned_header = _clean_cpp_header(raw_header)  # remove #includes, normaliza blanks
    if not cleaned_header:
        return ""

    if len(cleaned_header) > CPP_HEADER_CONTEXT_MAX_CHARS:
        log.debug(
            "Header de contexto '%s' truncado de %d para %d chars para o Canal C.",
            header_path, len(cleaned_header), CPP_HEADER_CONTEXT_MAX_CHARS,
        )
        cleaned_header = cleaned_header[:CPP_HEADER_CONTEXT_MAX_CHARS]

    return cleaned_header


async def _process_script_file(
    path: Path, raw: str, engine_version: str, project_source: str,
    domain_id: str = "ue5",
) -> list[dict]:
    """
    Processa um arquivo GDScript (.gd) ou C# (.cs) em modo sample:
    divide por função/método, agrupa blocos pequenos, e documenta cada
    grupo via LLM com foco em MECÂNICA (não API).

    Para GDScript: usa indentação para delimitar funções.
    Para C#: usa balanceamento de chaves.
    """
    extension = path.suffix.lower()
    cleaned = raw.strip()
    if not cleaned:
        log.debug("Script vazio após limpeza, ignorado: %s", path)
        return []

    # Define parâmetros baseados no tipo de script
    if extension == ".gd":
        max_group_chars = GDSCRIPT_MAX_GROUP_CHARS
        doc_type = "gdscript_implementation"
        code_lang = "gdscript"
    elif extension == ".cs":
        max_group_chars = CSHARP_MAX_GROUP_CHARS
        doc_type = "csharp_implementation"
        code_lang = "csharp"
    else:
        log.warning("Extensão de script não suportada: %s", extension)
        return []

    # CORREÇÃO (bloqueio de event loop): o split de funções é CPU-bound
    # e pode ser custoso em arquivos grandes — despachado para thread.
    blocks = await asyncio.to_thread(_split_script_into_functions, cleaned, extension)
    groups = _group_script_blocks(blocks, max_group_chars)

    all_chunks: list[dict] = []
    for group_idx, (names, group_text) in enumerate(groups):
        max_chars = settings.ingestion_max_header_chars
        if len(group_text) > max_chars:
            log.warning(
                "Grupo de funções '%s' em '%s' tem %d chars (limite=%d) — "
                "truncando antes de enviar à LLM documentadora.",
                names[:3], path, len(group_text), max_chars,
            )
            group_text = group_text[:max_chars]

        # CORREÇÃO: substitui o placeholder "__TOP_LEVEL__" por título contextual
        # baseado no nome do arquivo e diretório pai
        if names and names[0] == "__TOP_LEVEL__":
            # Fallback contextual: nome do arquivo + escopo de nível superior
            parent_dir = path.parent.name if path.parent.name else "root"
            title = f"{path.stem} - Top Level Scope"
        else:
            title = names[0] if names else path.stem

        user_message = (
            f"Script file: {path.name}\n"
            f"Functions in this group: {', '.join(names)}\n\n"
            f"Implementation:\n"
            f"```{code_lang}\n{group_text}\n```"
        )
        messages = [
            {"role": "system", "content": _SCRIPT_DOCUMENTER_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        try:
            markdown, _pt, _ct = await call_litellm(
                messages=messages,
                model=settings.ingestion_documenter_model,
            )
        except Exception as exc:
            log.error(
                "Falha ao documentar grupo %d/%d de '%s': %s — grupo ignorado.",
                group_idx + 1, len(groups), path, exc,
            )
            continue

        # source_file inclui o índice do grupo para não colidir com outros
        # grupos do mesmo arquivo na deleção/upsert idempotente por
        # source_file (ver _delete_file_chunks).
        group_source_file = f"{path}#group{group_idx}"

        all_chunks.extend(
            _build_chunks_with_metadata(
                markdown_text=markdown,
                source_file=group_source_file,
                doc_type=doc_type,
                title=title,
                engine_version=engine_version,
                raw_content=group_text,
                project_source=project_source,
                source_file_root=str(path),
                domain_id=domain_id,
                # CORREÇÃO (divergência ue_version/godot_version): domain_id
                # agora vem do chamador em vez de hardcoded "ue5" — esta
                # função processa .gd/.cs tanto de UE5 (.cpp reaproveitado
                # no Canal C) quanto de Godot 4, e cada domínio precisa da
                # chave de versão correta no payload (ue_version vs
                # godot_version). Ver relatório de auditoria RAG.
            )
        )

    return all_chunks


async def _process_header_file(
    path: Path, raw: str, engine_version: str, project_source: str, sample_mode: bool = False,
    domain_id: str = "ue5",
) -> list[dict]:
    # CORREÇÃO (ponto cego de indexação — ver _STRATEGIC_ENGINE_MODULE_MARKERS):
    # em modo Engine, headers de módulos estratégicos de gameplay moderno
    # bypassam a allowlist de _GAMEPLAY_KEYWORDS mesmo sem sample_mode=True.
    strategic_override = _is_strategic_gameplay_path(path)
    relevant, class_name = _is_gameplay_relevant_header(
        raw, path.name, sample_mode=sample_mode, strategic_override=strategic_override
    )
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

    try:
        markdown, _prompt_tokens, _completion_tokens = await call_litellm(
            messages=messages,
            model=settings.ingestion_documenter_model,
        )
    except Exception as exc:
        log.error(
            "Falha ao documentar header '%s' (Canal A): %s — arquivo ignorado.",
            path, exc,
        )
        return []

    return _build_chunks_with_metadata(
        markdown_text=markdown,
        source_file=str(path),
        doc_type="cpp_header_api",
        title=title,
        engine_version=engine_version,
        raw_content=cleaned,
        project_source=project_source,
        domain_id=domain_id,
        # CORREÇÃO: domain_id agora vem do chamador (ver _process_script_file
        # acima para o mesmo padrão de correção).
    )


# =============================================================================
# CANAL B — Documentação oficial (.udn)
# =============================================================================

def _sanitize_md_or_rst(content: str, extension: str) -> tuple[str, str]:
    """
    Sanitiza arquivos Markdown (.md) ou ReStructuredText (.rst) da documentação do Godot.

    Para .md: remove frontmatter YAML, links preservando texto.
    Para .rst: remove diretivas Sphinx, roles/referências cruzadas.

    Retorna (conteúdo_limpo, título_extraído).
    """
    cleaned = content
    title = ""

    if extension == ".md":
        # Remove frontmatter YAML
        cleaned = _MD_FRONTMATTER_RE.sub("", cleaned)
        # Converte links Markdown para texto simples
        cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
        # Extrai título do primeiro heading
        title_match = _MD_TITLE_RE.search(cleaned)
        if title_match:
            title = title_match.group(1).strip()

    elif extension == ".rst":
        # Remove diretivas Sphinx/RST
        cleaned = _RST_DIRECTIVE_RE.sub("", cleaned)
        # Remove roles/referências cruzadas (:ref:`title` -> title)
        cleaned = _RST_ROLE_RE.sub(r"\1", cleaned)
        # Converte links RST inline
        cleaned = _RST_LINK_RE.sub(r"\1", cleaned)
        # Extrai título do primeiro heading RST (sublinhado com === ou ---)
        title_match = _RST_TITLE_RE.search(cleaned)
        if title_match:
            title = title_match.group(1).strip()

    # Remove excesso de linhas em branco
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned).strip()

    return cleaned, title


async def _process_md_or_rst_file(
    path: Path, raw: str, engine_version: str, project_source: str,
    domain_id: str = "godot_4", doc_type: str = "godot_documentation",
) -> list[dict]:
    """
    Processa um arquivo de documentação Markdown (.md) ou RST (.rst).

    CORREÇÃO (divergência de payload): esta função é reaproveitada por TRÊS
    canais diferentes de DOIS domínios — docs oficiais do Godot (.md/.rst),
    docs oficiais da UE5 (.udn) e Blueprints exportados como JSON (.bp.json,
    que tecnicamente não é Markdown/RST, mas usa este canal por não precisar
    de chamada de LLM). Antes, `doc_type` e `domain_id` eram hardcoded para
    os valores do Godot, então .udn e .bp.json (ambos UE5) eram gravados no
    Qdrant como se fossem documentação Godot — chave de versão errada
    (godot_version em vez de ue_version) E doc_type enganoso. Os defaults
    abaixo preservam o comportamento atual para o canal Godot (chamador não
    precisa mudar nada); os outros dois call sites em `_run_ingestion`
    passam os valores corretos explicitamente.
    """
    clean_text, title = _sanitize_md_or_rst(raw, path.suffix.lower())
    if not clean_text:
        log.debug("Documento %s vazio após sanitização, ignorado: %s", path.suffix, path)
        return []

    return _build_chunks_with_metadata(
        markdown_text=clean_text,
        source_file=str(path),
        doc_type=doc_type,
        title=title or path.stem,
        engine_version=engine_version,
        raw_content=raw,  # CORREÇÃO: conteúdo bruto original, não sanitizado
        project_source=project_source,
        domain_id=domain_id,
    )


# =============================================================================
# CANAL D — Blueprints exportados como JSON estruturado (.bp.json)
# =============================================================================
# NOTA DE ARQUITETURA — por que Blueprints importam tanto quanto C++ aqui:
#     Gameplay em projetos UE5 reais é HÍBRIDO — muita mecânica (regras de
#     dano, montagem de árvore de habilidade, lógica de UI, triggers de
#     nível) é implementada em Blueprint, não em C++. Ignorar Blueprints no
#     RAG deixaria uma lacuna tão grande quanto ignorar .cpp (ver Canal C).
#
# MUDANÇA ARQUITETURAL (v2): este canal deixou de consumir Blueprints
# exportados em texto T3D + limpeza por regex. Um Editor Utility Script
# rodando DENTRO da Unreal (ver BP_GraphExporter.py, entregue junto) agora
# percorre o EdGraph via API Python e exporta um <Asset>.bp.json já
# ESTRUTURADO — com ParentClass, Variables e ExecutionFlow linearizado
# (nós de controle de fluxo como Branch/Sequence viram passos "branch" com
# sub-listas rotuladas). Isso elimina a limitação conhecida da versão
# anterior (regex não entende topologia de grafo): a topologia agora é
# resolvida DENTRO da Unreal, onde a API tem acesso real aos pinos de
# execução — o backend só recebe o resultado já linearizado.
#
# LIMITAÇÃO CONHECIDA REMANESCENTE (herdada do exportador, não deste
#     backend): o exportador não deduplica nós em ramos que reconvergem
#     (um nó alcançado por dois branches diferentes aparece uma vez em
#     cada ramo), e loops de execução são detectados e cortados, não
#     resolvidos como laço semântico. Ver docstring de
#     _trace_execution_flow em BP_GraphExporter.py para o detalhe completo.

_TSCN_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Godot Engine Developer and Technical Architect, specialized \
in explaining Godot 4 scene structure and node hierarchies from .tscn scene \
files.

The .tscn file format contains:
- External resources (ext_resource): scripts, textures, scenes referenced.
- Nodes (node): each with name, type (class), parent relationship, and properties.
- Connections (connection): signal bindings between nodes.

Your task: produce a Markdown guide explaining WHAT this scene does as a \
gameplay system — not a transcription of the .tscn format itself.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title naming the scene/system (e.g. \
"# Player Scene", "# Enemy Spawner", "# Main Menu UI") — not just the filename.
3. Describe the node hierarchy: root node, key child nodes, and their purposes.
4. Document attached scripts and their roles in the scene.
5. Explain signal connections: which nodes emit signals, which receive them, \
and what gameplay events they represent.
6. Highlight @export properties that configure the scene from the inspector.
7. Ignore transform data, raw vectors, and purely visual properties unless \
they affect gameplay logic.
8. Be concise but complete — prioritize helping a developer understand the \
scene architecture and how to modify or extend it.\
"""


def _extract_tscn_scene_name(path: Path) -> str:
    """
    Extrai o nome da cena a partir do path de um arquivo .tscn.

    Para 'player.tscn' retorna 'Player'.
    Para 'ui/main_menu.tscn' retorna 'MainMenu'.
    """
    stem = path.stem  # "player" para "player.tscn"
    # Converte snake_case ou kebab-case para PascalCase
    parts = stem.replace("-", "_").split("_")
    return "".join(part.capitalize() for part in parts)


async def _process_tscn_file(
    path: Path, raw: str, engine_version: str, project_source: str
) -> list[dict]:
    """
    Processa um arquivo de cena do Godot (.tscn):
    extrai hierarquia de nós, scripts anexados e conexões de sinais,
    documenta via LLM com foco em arquitetura de cena, e constrói
    os chunks finais.

    O formato .tscn é baseado em texto com seções:
    - [ext_resource]: recursos externos (scripts, texturas)
    - [node]: cada nó com nome, tipo, propriedades
    - [connection]: conexões de sinais entre nós
    """
    scene_name = _extract_tscn_scene_name(path)

    # Parseia o arquivo .tscn para extrair informações relevantes
    ext_resources: list[dict] = []
    nodes: list[dict] = []
    connections: list[dict] = []

    current_section = None
    current_props: dict = {}

    for line in raw.splitlines():
        line = line.strip()

        # Detecta início de seção
        if line.startswith("[ext_resource"):
            current_section = "ext_resource"
            current_props = _parse_tscn_header(line)
            ext_resources.append(current_props)
        elif line.startswith("[node"):
            current_section = "node"
            current_props = _parse_tscn_header(line)
            current_props["properties"] = []
            nodes.append(current_props)
        elif line.startswith("[connection"):
            current_section = "connection"
            current_props = _parse_tscn_header(line)
            connections.append(current_props)
        elif line.startswith("["):
            # Outra seção que não nos interessa
            current_section = None
            current_props = {}
        elif current_section and "=" in line and current_section in ("node", "ext_resource"):
            # Propriedade dentro de uma seção
            key, _, value = line.partition("=")
            if current_section == "node":
                current_props["properties"].append({key.strip(): value.strip()})

    # Filtra apenas scripts dos ext_resources
    scripts = [
        r for r in ext_resources
        if r.get("type") == "Script" or (r.get("path") or "").endswith((".gd", ".cs"))
    ]

    # Constrói representação estruturada para o LLM
    scene_data = {
        "SceneName": scene_name,
        "Scripts": [
            {"id": s.get("id"), "path": s.get("path")}
            for s in scripts
        ],
        "Nodes": [
            {
                "name": n.get("name"),
                "type": n.get("type"),
                "parent": n.get("parent"),
                "script": n.get("script"),
            }
            for n in nodes
            if n.get("type")  # Só nós com tipo definido
        ],
        "SignalConnections": [
            {
                "signal": c.get("signal"),
                "from": c.get("from"),
                "to": c.get("to"),
                "method": c.get("method"),
            }
            for c in connections
        ],
    }

    formatted_data = json.dumps(scene_data, indent=2, ensure_ascii=False)

    if len(formatted_data) > TSCN_MAX_CHARS:
        log.warning(
            "Cena '%s' tem %d chars formatado (limite=%d) — truncando "
            "antes de enviar à LLM documentadora. Nós mais profundos "
            "podem ser omitidos.",
            path, len(formatted_data), TSCN_MAX_CHARS,
        )
        formatted_data = formatted_data[:TSCN_MAX_CHARS]

    user_message = (
        f"Godot scene file: {scene_name}\n\n"
        f"```json\n{formatted_data}\n```"
    )
    messages = [
        {"role": "system", "content": _TSCN_DOCUMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    try:
        markdown, _pt, _ct = await call_litellm(
            messages=messages,
            model=settings.ingestion_documenter_model,
        )
    except Exception as exc:
        log.error(
            "Falha ao documentar cena '%s': %s — arquivo ignorado.",
            path, exc,
        )
        return []

    return _build_chunks_with_metadata(
        markdown_text=markdown,
        source_file=str(path),
        doc_type="godot_scene",
        title=scene_name,
        engine_version=engine_version,
        raw_content=formatted_data,
        project_source=project_source,
        domain_id="godot_4",  # Godot 4 scene files
    )


def _parse_tscn_header(line: str) -> dict:
    """
    Parseia o cabeçalho de uma seção .tscn.

    Exemplo: '[node name="Player" type="CharacterBody2D" parent="."]'
    Retorna: {"name": "Player", "type": "CharacterBody2D", "parent": "."}
    """
    result: dict = {}
    # Remove os colchetes externos
    content = line.strip("[]")
    # Separa o tipo da seção dos atributos
    parts = content.split(" ", 1)
    if len(parts) > 1:
        attrs = parts[1]
        # Parseia key="value" ou key=value
        import re
        for match in re.finditer(r'(\w+)=("([^"]*)"|(\S+))', attrs):
            key = match.group(1)
            value = match.group(3) if match.group(3) is not None else match.group(4)
            result[key] = value
    return result


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


def _extract_engine_version(path_str: str, domain_id: str = "ue5") -> tuple[str, str]:
    """
    Extrai a versão da engine do caminho do arquivo e retorna (versão, nome_do_campo).

    Args:
        path_str: Caminho do arquivo sendo processado
        domain_id: Identificador do domínio ("ue5" ou "godot_4")

    Returns:
        Tuple (version, version_field_name):
            - Para UE5: ("5.8", "ue_version")
            - Para Godot 4: ("4.7.1-stable", "godot_version")
    """
    if domain_id == "godot_4":
        # Godot 4 não tem padrão de versão no caminho - usa o default configurado
        return settings.ingestion_default_godot_version, "godot_version"
    else:
        # UE5: tenta extrair do caminho, senão usa default
        match = _UE_VERSION_RE.search(path_str)
        version = match.group(1) if match else settings.ingestion_default_ue_version
        return version, "ue_version"


def _build_chunks_with_metadata(
    markdown_text: str,
    source_file: str,
    doc_type: str,
    title: str,
    engine_version: str,
    raw_content: str,
    project_source: str,
    source_file_root: str | None = None,
    domain_id: str = "ue5",
) -> list[dict]:
    """
    Constrói chunks com metadados enriquecidos para upsert no Qdrant.

    Args:
        domain_id: Identificador do domínio ("ue5" ou "godot_4").
            Determina qual campo de versão será usado no payload:
            - "ue5" → "ue_version"
            - "godot_4" → "godot_version"
        engine_version: Versão da engine (UE5 ou Godot).
        raw_content: Conteúdo bruto completo do arquivo fonte.
            CORREÇÃO: raw_content é preservado em TODOS os chunks para
            garantir que o RAG tenha acesso ao contexto completo durante
            a geração. Anteriormente era NULL para chunk_id > 0, o que
            comprometia a recuperação.
    """
    sections = _hierarchical_markdown_chunker(markdown_text, title)
    sections = _enforce_max_chunk_size(sections, settings.ingestion_max_chunk_chars)

    # Determina o nome do campo de versão baseado no domínio
    version_field = "godot_version" if domain_id == "godot_4" else "ue_version"

    chunks: list[dict] = []
    for chunk_id, section_text in enumerate(sections):
        if not raw_content or not raw_content.strip():
            log.warning(
                "raw_content vazio ou ausente para chunk %d de '%s' — "
                "isso pode comprometer a qualidade do RAG.",
                chunk_id,
                source_file,
            )

        chunks.append(
            {
                "summary_markdown": section_text,
                "source_file": source_file,
                # Path puro do arquivo em disco, SEM sufixo sintético de grupo
                # (ex: "#group0"). Usado por _delete_file_chunks para limpar
                # todos os chunks de um arquivo antes de reprocessá-lo,
                # mesmo quando source_file varia entre grupos (Canal C).
                # Default = source_file para Canais A/B, onde os dois são
                # sempre idênticos (1 arquivo = 1 source_file, sem grupos).
                "source_file_root": source_file_root or source_file,
                "doc_type": doc_type,
                "title": title,
                "chunk_id": chunk_id,
                version_field: engine_version,
                # CORREÇÃO (RAG quality): raw_content agora é preservado em TODOS os chunks.
                # Anteriormente era NULL para chunk_id > 0, o que quebrava a recuperação
                # de contexto completo durante a geração. O RAG precisa do conteúdo bruto
                # para fornecer referências precisas ao LLM.
                "raw_content": raw_content,
                # Rótulo de proveniência — NUNCA entra no texto embedado.
                # Permite segmentar via query_filter no Qdrant no futuro
                # (ex: "só Engine", "só samples", "excluir Lyra") sem
                # precisar reingerir nada. Ver auditoria de arquitetura RAG.
                "project_source": project_source,
            }
        )
    return chunks


# =============================================================================
# CONFIGURAÇÃO IDEMPOTENTE DO QDRANT
# =============================================================================

async def ensure_collection_exists(
    qdrant: AsyncQdrantClient,
    collection_name: str | None = None,
) -> None:
    """
    Garante que a coleção Qdrant existe, criando-a se necessário.

    Args:
        qdrant: Cliente Qdrant assíncrono.
        collection_name: Nome da coleção. Se None, usa a coleção do domínio ativo.
    """
    resolved_collection = collection_name or settings.qdrant_collection_for(settings.active_domain)

    exists = await qdrant.collection_exists(resolved_collection)
    if exists:
        log.info("Coleção '%s' já existe — validação OK (idempotente).", resolved_collection)
        return

    log.warning(
        "Coleção '%s' não encontrada no Qdrant. Criando automaticamente...",
        resolved_collection,
    )
    await qdrant.create_collection(
        collection_name=resolved_collection,
        vectors_config=qdrant_models.VectorParams(
            size=settings.embedding_dim,
            distance=qdrant_models.Distance.COSINE,
        ),
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: qdrant_models.SparseVectorParams(
                modifier=qdrant_models.Modifier.IDF,
            ),
        },
    )
    log.info(
        "Coleção '%s' criada com sucesso | dim=%d | distância=COSINE | sparse=keywords_sparse",
        resolved_collection,
        settings.embedding_dim,
    )

async def ensure_sparse_vectors_enabled(
    qdrant: AsyncQdrantClient,
    collection_name: str | None = None,
    _retry: int = 0,
) -> None:
    """
    Tenta habilitar o espaço de vetor esparso 'keywords_sparse' numa coleção
    já existente, via `update_collection` (PATCH /collections/{name}).

    Args:
        qdrant: Cliente Qdrant assíncrono.
        collection_name: Nome da coleção. Se None, usa a coleção do domínio ativo.
        _retry: Contador interno de retries.

    modifier=Modifier.IDF: os pontos guardam só term-frequency bruto (ver
    build_sparse_vector); o Qdrant calcula e mantém a estatística de IDF em
    nível de coleção internamente e aplica na hora da busca.

    LIMITAÇÃO REAL DO SERVIDOR (não é corrida entre workers — correção de
    uma suposição anterior deste código): `update_collection` só permite
    alterar PARÂMETROS de um vetor nomeado que JÁ EXISTE no schema (ex:
    hnsw_config, quantization, on_disk). Adicionar um vetor NOMEADO NOVO
    (denso ou esparso) a uma coleção que já existe, sem recriá-la, só é
    suportado pelo Qdrant a partir da v1.18 ("Adding and Removing Named
    Vectors" — ver release notes oficiais), via um endpoint dedicado
    (`PUT /collections/{name}/vectors/{vector_name}`,
    `client.create_vector_name()` no qdrant-client). Em qualquer versão
    anterior, `update_collection` SEMPRE retorna
    'Wrong input: Not existing vector name error: keywords_sparse' pra essa
    operação — de forma consistente e determinística, não esporádica.

    Por isso esta função:
    1. Tenta o `update_collection` (funciona de primeira em Qdrant >= 1.18).
    2. Se falhar com essa mensagem específica, trata como PERMANENTE — não
       adianta re-tentar — loga um erro claro apontando o caminho de
       migração real (ver app/scripts/migrate_to_hybrid_collection.py ou
       upgrade do Qdrant) e retorna sem propagar exceção (não bloqueia o
       startup da aplicação, igual ao caso "coleção não existe" abaixo).
    3. Para qualquer OUTRA falha de `UnexpectedResponse` (ex: hiccup de
       rede, indisponibilidade momentânea do Qdrant) — essas sim podem ser
       transitórias — faz um retry curto com backoff antes de desistir.

    DEFENSIVO (chamável a qualquer momento, inclusive no startup do
    FastAPI antes de qualquer ingestão ter rodado): se a coleção ainda não
    existir, `get_collection` lançaria UnexpectedResponse 404. Em vez de
    propagar isso como erro, seguimos o mesmo padrão de
    `app/db/qdrant.py::_validate_collection` — registramos um warning e
    retornamos, já que o ingestor ainda vai criar a coleção (com o espaço
    esparso já habilitado desde o início, ver `_flush_batch`/upsert) na
    primeira execução.
    """
    resolved_collection = collection_name or settings.qdrant_collection_for(settings.active_domain)

    exists = await qdrant.collection_exists(resolved_collection)
    if not exists:
        log.warning(
            "Coleção '%s' ainda não existe — migração de sparse vectors "
            "adiada. Será aplicada automaticamente quando a ingestão "
            "criar a coleção, ou pode ser chamada de novo depois.",
            resolved_collection,
        )
        return

    info = await qdrant.get_collection(resolved_collection)
    existing_sparse = (info.config.params.sparse_vectors or {})

    if SPARSE_VECTOR_NAME in existing_sparse:
        log.info("Sparse vector '%s' já habilitado — nada a fazer.", SPARSE_VECTOR_NAME)
        return

    try:
        await qdrant.update_collection(
            collection_name=resolved_collection,
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: qdrant_models.SparseVectorParams(
                    modifier=qdrant_models.Modifier.IDF,
                ),
            },
        )
    except UnexpectedResponse as exc:
        error_body = getattr(exc, "content", b"") or b""
        error_text = (
            error_body.decode("utf-8", errors="ignore")
            if isinstance(error_body, (bytes, bytearray))
            else str(error_body)
        )

        if "Not existing vector name error" in error_text:
            log.error(
                "MIGRAÇÃO ADITIVA NÃO SUPORTADA NESTA VERSÃO DO QDRANT: "
                "adicionar o vetor nomeado '%s' a uma coleção JÁ EXISTENTE "
                "sem recriá-la só funciona a partir do Qdrant v1.18 "
                "('Adding and Removing Named Vectors'). Sua instância está "
                "numa versão anterior a essa, então esta operação falha de "
                "forma DETERMINÍSTICA (não é transitório — retentativas não "
                "resolvem, por isso não vamos re-tentar). Duas opções: "
                "(a) atualize a imagem do Qdrant em docker-compose.yml para "
                ">= v1.18 — faça isso incrementalmente, uma minor version "
                "por vez, pois a v1.17 trocou o storage engine (RocksDB -> "
                "Gridstore) e pular versões pode quebrar o storage "
                "existente; ou (b) rode "
                "'app/scripts/migrate_to_hybrid_collection.py', que copia os "
                "pontos existentes (SEM re-chamar a LLM documentadora nem "
                "recalcular embeddings) para uma coleção nova já criada com "
                "os dois espaços de vetor. Erro original do Qdrant: %s",
                SPARSE_VECTOR_NAME,
                error_text.strip(),
            )
            return

        # Qualquer outra falha de UnexpectedResponse (rede, indisponibilidade
        # momentânea) — aqui sim pode valer a pena um retry curto.
        max_retries = 3
        if _retry >= max_retries:
            log.error(
                "Falha ao habilitar sparse vector '%s' após %d tentativas "
                "(erro não relacionado à limitação de versão do Qdrant): %s",
                SPARSE_VECTOR_NAME,
                max_retries,
                exc,
            )
            raise

        backoff_seconds = 1.0 * (2**_retry)  # 1s, 2s, 4s
        log.warning(
            "update_collection para '%s' falhou (%s) — tentando de novo "
            "(%d/%d) em %.1fs.",
            SPARSE_VECTOR_NAME,
            exc,
            _retry + 1,
            max_retries,
            backoff_seconds,
        )
        await asyncio.sleep(backoff_seconds)
        await ensure_sparse_vectors_enabled(qdrant, collection_name=collection_name, _retry=_retry + 1)
        return

    log.info(
        "Sparse vector '%s' habilitado na coleção '%s' (modifier=IDF). "
        "Vetor denso existente não foi alterado.",
        SPARSE_VECTOR_NAME, collection_name,
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
    """
    Remove TODOS os chunks de um arquivo-fonte, incondicionalmente.

    NÃO é mais chamada automaticamente pelo fluxo de `_run_ingestion` (ver
    correção de idempotência: `_delete_orphaned_chunks`, que remove só o
    resíduo de versões anteriores e apenas após o commit confirmado).
    Mantida aqui como utilitário para limpeza manual/administrativa
    completa de um arquivo específico (ex: ferramenta de CLI/admin que
    queira forçar a remoção total antes de uma reingestão manual).

    CORREÇÃO (Canal C / .cpp): chunks gerados a partir de .cpp usam um
    `source_file` SINTÉTICO com sufixo "#groupN" (ver _process_script_file),
    porque um único .cpp pode virar múltiplos grupos de funções. Filtrar
    por match exato de `source_file` aqui NUNCA encontraria esses pontos
    (o path puro nunca bate com "path#group0", "path#group1", ...) — a
    deleção "funcionaria" sem erro mas silenciosamente não apagaria nada,
    e uma reingestão que mudasse o agrupamento de funções acumularia
    chunks órfãos para sempre. Por isso o filtro usa `source_file_root`
    (sempre o path puro, gravado em TODO chunk independente do canal — ver
    _build_chunks_with_metadata) em vez de `source_file`.
    """
    result = await qdrant.delete(
        collection_name=collection_name,
        points_selector=qdrant_models.FilterSelector(
            filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="source_file_root",
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


async def _delete_orphaned_chunks(
    qdrant: AsyncQdrantClient,
    collection_name: str,
    source_file: str,
    keep_chunk_count: int,
) -> None:
    """
    Remove SOMENTE chunks residuais de uma versão ANTERIOR do arquivo que
    tinha mais chunks que a versão atual (chunk_id >= keep_chunk_count).

    Chamado DEPOIS do commit confirmado no manifesto — nunca antes. Os
    chunks 0..keep_chunk_count-1 já foram sobrescritos in-place pelo
    upsert (point_id determinístico), então este delete nunca remove
    conteúdo vigente; na pior hipótese (falha aqui), sobra um chunk órfão
    da versão anterior até a próxima ingestão — nunca um buraco de
    conteúdo, que é o cenário que este fluxo elimina.
    """
    try:
        await qdrant.delete(
            collection_name=collection_name,
            points_selector=qdrant_models.FilterSelector(
                filter=qdrant_models.Filter(
                    must=[
                        qdrant_models.FieldCondition(
                            key="source_file_root",
                            match=qdrant_models.MatchValue(value=source_file),
                        ),
                        qdrant_models.FieldCondition(
                            key="chunk_id",
                            range=qdrant_models.Range(gte=keep_chunk_count),
                        ),
                    ]
                )
            ),
            wait=True,
        )
    except Exception as exc:
        log.warning(
            "Falha ao limpar chunks órfãos de '%s' (não bloqueia o commit, "
            "resíduo será limpo em ingestão futura): %s",
            source_file,
            exc,
        )


async def _commit_confirmed_files(
    redis: Redis,
    qdrant: AsyncQdrantClient,
    collection_name: str,
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

        # Limpeza de resíduo de versão anterior — só agora, com o commit já
        # confirmado no manifesto (ver docstring de _delete_orphaned_chunks).
        await _delete_orphaned_chunks(qdrant, collection_name, source_file, tracker.total_chunks)

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
    domain_id: str = "ue5",
) -> tuple[dict[str, int], int]:
    """
    Processa um lote de chunks, gera embeddings e insere no Qdrant.

    CORREÇÃO (canal esparso sem regex por domínio): `build_sparse_vector`
    era chamado sem `camel_split_regex`, caindo sempre no fallback legado
    (heurística de prefixo UE5: A/U/F/E — ver sparse_vector_service.py).
    Isso silenciosamente tokenizava identificadores Godot (@export,
    snake_case, CharacterBody3D) com a regra errada, degradando o recall
    do canal esparso/BM25 para todo o domínio Godot desde a ingestão — o
    lado da busca (rag_service._hybrid_search) já resolvia a regex certa
    por domínio, então a assimetria só existia neste lado. `domain_id`
    resolve `BaseDomainProfile.sparse_camel_split_regex` do mesmo jeito.

    Returns:
        tuple[dict[str, int], int]: (per_file_counts, embedding_failures)
            - per_file_counts: mapeia source_file_root -> contagem de chunks inseridos
            - embedding_failures: número de chunks que falharam no embedding
    """
    if not batch:
        return {}, 0

    camel_split_regex = domain_registry.get(domain_id).sparse_camel_split_regex

    semaphore = asyncio.Semaphore(settings.ingestion_embedding_concurrency)
    embedding_failures = 0

    async def _embed_one(chunk: dict) -> tuple[dict, list[float] | None]:
        async with semaphore:
            try:
                vector = await embed_text(chunk["summary_markdown"])
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
            embedding_failures += 1
            continue
        point_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['source_file']}::{chunk['chunk_id']}")
        )

        # BUSCA HÍBRIDA: o vetor esparso é gerado a partir da concatenação
        # de title + raw_content + summary_markdown — cobre tanto os
        # identificadores técnicos exatos (ex: "AddMovementInput",
        # "UPROPERTY") presentes no código/título quanto os termos do
        # resumo semântico gerado pela LLM documentadora. raw_content é
        # None para chunk_id != 0 (ver _build_chunks_with_metadata) — o
        # `or ""` cobre esse caso sem quebrar a concatenação.
        sparse_text = " ".join(
            (
                chunk.get("title") or "",
                chunk.get("raw_content") or "",
                chunk.get("summary_markdown") or "",
            )
        )
        sparse_vector = build_sparse_vector(sparse_text)

        points.append(
            qdrant_models.PointStruct(
                id=point_id,
                # Vetor denso mantido na chave "" (não-nomeada, mesmo
                # esquema já usado antes da migração híbrida) + vetor
                # esparso nomeado 'keywords_sparse' — coexistem no mesmo
                # ponto sem conflito, já que são espaços de índice
                # independentes no Qdrant.
                vector={"": vector, SPARSE_VECTOR_NAME: sparse_vector},
                payload=chunk,
            )
        )

    if not points:
        log.warning("Lote descartado: todos os %d embeddings falharam.", len(batch))
        return {}, embedding_failures

    # Retry com backoff exponencial para erros de conexão/transitórios
    max_retries = settings.qdrant_upsert_max_retries
    wait_enabled = settings.qdrant_upsert_wait
    batch_size = settings.ingestion_batch_size
    last_exc: Exception | None = None

    # Sub-batching: divide lotes grandes para evitar timeouts HTTP
    sub_batches: list[list[qdrant_models.PointStruct]] = []
    if len(points) > batch_size:
        for i in range(0, len(points), batch_size):
            sub_batches.append(points[i:i + batch_size])
        log.info(
            "Sub-batching ativado | %d pontos divididos em %d sub-lotes | coleção=%s",
            len(points),
            len(sub_batches),
            collection_name,
        )
    else:
        sub_batches = [points]

    # Processa cada sub-lote sequencialmente
    for sub_batch_idx, sub_points in enumerate(sub_batches):
        for attempt in range(1, max_retries + 1):
            try:
                await qdrant.upsert(
                    collection_name=collection_name,
                    points=sub_points,
                    wait=wait_enabled
                )
                break  # Sucesso — sai do loop de retry
            except (
                httpx.ReadError,
                httpx.ReadTimeout,
                httpcore.ReadError,
                httpcore.ReadTimeout,
                ResponseHandlingException,
                UnexpectedResponse,
            ) as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = 2 ** attempt  # 2, 4, 8 segundos...
                    log.warning(
                        "Tentativa %d/%d de upsert falhou (%s) — retry em %ds | "
                        "coleção=%s sub-lote=%d/%d pontos=%d",
                        attempt,
                        max_retries,
                        type(exc).__name__,
                        backoff,
                        collection_name,
                        sub_batch_idx + 1,
                        len(sub_batches),
                        len(sub_points),
                    )
                    await asyncio.sleep(backoff)
                else:
                    log.error(
                        "Todas as %d tentativas de upsert falharam | coleção=%s "
                        "sub-lote=%d/%d pontos=%d: %s",
                        max_retries,
                        collection_name,
                        sub_batch_idx + 1,
                        len(sub_batches),
                        len(sub_points),
                        exc,
                        exc_info=True,
                    )
                    return {}, embedding_failures
            except Exception as exc:
                # Erros não recuperáveis — loga e falha imediatamente
                log.error(
                    "Falha no upsert do sub-lote (%d pontos) na coleção '%s': %s",
                    len(sub_points),
                    collection_name,
                    exc,
                    exc_info=True,
                )
                return {}, embedding_failures

    if embedding_failures > 0:
        log.warning(
            "Batch inserido com falhas de embedding | %d pontos OK | %d falhas | coleção=%s",
            len(points),
            embedding_failures,
            collection_name,
        )
    else:
        log.info(
            "Batch inserido com sucesso | %d pontos | coleção=%s",
            len(points),
            collection_name,
        )

    # CORREÇÃO (Canal C / .cpp): agrupamos por source_file_root, não por
    # source_file. Chunks de .cpp têm source_file sintético por grupo
    # ("path#group0", "path#group1", ...), mas o rastreamento de commit em
    # _run_ingestion (`tracking`) é indexado pelo path real do arquivo.
    # Agrupar por source_file (chave antiga) faria os grupos NUNCA baterem
    # com `tracking`, e o manifesto de checkpoint nunca seria commitado para
    # .cpp — reprocessando (e re-chamando a LLM documentadora) o arquivo
    # inteiro a cada ingestão, para sempre. source_file_root é idêntico a
    # source_file para chunks de Canais A (.h) e B (.udn), então isso não
    # muda nada para eles.
    per_file_counts: dict[str, int] = {}
    for point in points:
        source_file_root = point.payload["source_file_root"]
        per_file_counts[source_file_root] = per_file_counts.get(source_file_root, 0) + 1

    return per_file_counts, embedding_failures


# =============================================================================
# BACKFILL DE SPARSE VECTORS — dados legados (busca híbrida)
# =============================================================================
# Popula o espaço esparso 'keywords_sparse' (ver ensure_sparse_vectors_enabled)
# em pontos que já existiam na coleção ANTES da migração para busca híbrida —
# ou seja, pontos cujo `vector` até aqui só tem a chave "" (denso).
#
# Não re-ingere nada: não chama a LLM documentadora, não recalcula o
# embedding denso, não toca em nenhum campo do payload. Apenas lê os campos
# de texto já armazenados (title, raw_content, summary_markdown) via scroll,
# gera o vetor esparso localmente (build_sparse_vector — CPU, síncrono,
# sem chamada de rede) e grava só esse vetor via `update_vectors`.
#
# IDEMPOTENTE: pontos que já têm o vetor esparso (detectado via
# `with_vectors=True` no scroll) são pulados — seguro rodar múltiplas vezes,
# inclusive em paralelo com ingestões novas (que já gravam o esparso desde
# o upsert, ver _flush_batch).
async def backfill_sparse_vectors(
    qdrant: AsyncQdrantClient,
    collection_name: str | None = None,
    batch_size: int = 200,
    domain_id: str | None = None,
) -> int:
    """
    Percorre TODOS os pontos já existentes na coleção (via `scroll`) e
    aplica `update_vectors` para popular o espaço esparso 'keywords_sparse'
    nos pontos legados que ainda não o têm, sem tocar nos vetores densos
    nem nos payloads existentes.

    Args:
        qdrant: Cliente Qdrant assíncrono.
        collection_name: Nome da coleção. Se None, usa a coleção do domínio ativo.
        batch_size: Tamanho do lote de scroll/update_vectors por iteração.
            200 é um valor conservador — build_sparse_vector é puramente
            local (sem I/O), então o gargalo real é a rede para
            scroll/update_vectors, não a geração do vetor em si.
        domain_id: Domínio dono da coleção (resolve a regex de tokenização
            correta via `BaseDomainProfile.sparse_camel_split_regex` — ver
            CORREÇÃO abaixo). Se None, usa `settings.active_domain`. Cada
            coleção pertence a exatamente um domínio (ver
            `settings.qdrant_collection_for`), então isso é seguro mesmo
            quando `collection_name` é passado explicitamente.

    CORREÇÃO (mesma classe de bug de _flush_batch): antes chamava
    `build_sparse_vector` sem `camel_split_regex`, caindo sempre no
    fallback legado (heurística de prefixo UE5). Pontos Godot legados
    passavam pelo backfill com a tokenização errada.

    Returns:
        Número total de pontos efetivamente atualizados com o vetor
        esparso (pontos já migrados anteriormente não contam).
    """
    resolved_domain_id = domain_id or settings.active_domain
    resolved_collection = collection_name or settings.qdrant_collection_for(resolved_domain_id)
    camel_split_regex = domain_registry.get(resolved_domain_id).sparse_camel_split_regex
    updated_count = 0
    scanned_count = 0
    next_offset = None

    log.info(
        "Backfill de sparse vectors iniciado | coleção=%s batch_size=%d",
        resolved_collection,
        batch_size,
    )

    while True:
        points, next_offset = await qdrant.scroll(
            collection_name=resolved_collection,
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
            # Pontos já migrados (por este backfill ou por uma ingestão
            # nova pós-migração) têm o vetor esparso no dict — pula.
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
            sparse_vector = build_sparse_vector(sparse_text, camel_split_regex=camel_split_regex)
            updates.append(
                qdrant_models.PointVectors(
                    id=point.id,
                    vector={SPARSE_VECTOR_NAME: sparse_vector},
                )
            )

        if updates:
            try:
                await qdrant.update_vectors(
                    collection_name=resolved_collection,
                    points=updates,
                )
            except Exception as exc:
                log.error(
                    "Falha ao gravar lote de sparse vectors (%d pontos): %s. "
                    "Backfill continua com o próximo lote — os pontos deste "
                    "lote serão retentados numa próxima execução.",
                    len(updates),
                    exc,
                )
            else:
                updated_count += len(updates)
                log.info(
                    "Backfill de sparse vectors: +%d pontos neste lote "
                    "(escaneados=%d, atualizados_total=%d)",
                    len(updates),
                    scanned_count,
                    updated_count,
                )

        if next_offset is None:
            break

    log.info(
        "Backfill de sparse vectors concluído | escaneados=%d atualizados=%d "
        "já_migrados=%d",
        scanned_count,
        updated_count,
        scanned_count - updated_count,
    )
    return updated_count


# =============================================================================
# ORQUESTRADOR PRINCIPAL — entrypoint chamado pelo BackgroundTasks
# =============================================================================

async def process_folder_ingestion(
    folder_path: str,
    content_type: str,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    engine_version_override: str | None = None,
    project_source: str = "UE5_Engine",
    sample_mode: bool = False,
    domain_id: str | None = None,
) -> None:
    """
    Entrypoint chamado pelo BackgroundTasks. Serializa a execução por
    coleção via lock distribuído no Redis antes de delegar o trabalho para
    `_run_ingestion`.

    Args:
        project_source: rótulo de proveniência gravado em CADA chunk
            (payload), nunca no texto embedado. Usado para segmentar a base
            depois via query_filter sem precisar reingerir (ex: "UE5_Engine",
            "Lyra", "CitySample", "VehicleSample"). Padrão "UE5_Engine" para
            não quebrar chamadas existentes que não passam este argumento.
        sample_mode: quando True, (1) desliga a allowlist de keywords de
            relevância — necessário porque projetos-sample usam convenções
            de nome próprias que não batem em _GAMEPLAY_KEYWORDS — e (2)
            também descobre e processa arquivos .cpp (Canal C), além dos
            .h, para capturar a IMPLEMENTAÇÃO da mecânica, não só a API.
            Deve ser False para a Engine (mantém o comportamento original,
            mais barato e mais estrito para a esmagadora maioria dos
            módulos). EXCEÇÃO: mesmo com sample_mode=False, os módulos
            estratégicos de gameplay moderno listados em
            _STRATEGIC_ENGINE_MODULE_MARKERS (Mover, Network Prediction,
            Chaos Mover/Vehicles, Motion Warping) recebem o mesmo
            tratamento do sample_mode — allowlist desligada e .cpp
            descoberto — de forma escopada, sem afetar o resto da Engine.
        domain_id: identificador do domínio (ex: "godot_4", "ue5").
            Se None, usa settings.active_domain.

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
    # Resolve domain_id e coleção correspondente
    resolved_domain_id = domain_id or settings.active_domain
    collection_name = settings.qdrant_collection_for(resolved_domain_id)
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

    # CORREÇÃO (ALTO — lock sem heartbeat): sem renovação, uma ingestão mais
    # longa que ingestion_lock_timeout_seconds perde o lock (expira por TTL)
    # NO MEIO da execução — uma segunda chamada concorrente pode então
    # adquiri-lo livremente e rodar em paralelo, gerando exatamente a
    # condição de corrida (delete/upsert intercalados do mesmo arquivo) que
    # este lock existe para prevenir. A task abaixo estende o TTL
    # periodicamente enquanto `_run_ingestion` estiver rodando.
    async def _renew_lock_periodically() -> None:
        interval = max(settings.ingestion_lock_timeout_seconds / 2, 1)
        while True:
            await asyncio.sleep(interval)
            try:
                await lock.reacquire()
            except Exception as exc:
                log.error(
                    "Falha ao renovar (heartbeat) o lock '%s' — a ingestão "
                    "pode perder exclusividade a partir daqui: %s",
                    lock_key,
                    exc,
                )
                return

    renewal_task = asyncio.create_task(_renew_lock_periodically())
    try:
        await _run_ingestion(
            folder_path=folder_path,
            content_type=content_type,
            qdrant=qdrant,
            redis=redis,
            collection_name=collection_name,
            engine_version_override=engine_version_override,
            project_source=project_source,
            sample_mode=sample_mode,
            resolved_domain_id=resolved_domain_id,
        )
    finally:
        renewal_task.cancel()
        try:
            await renewal_task
        except asyncio.CancelledError:
            pass
        try:
            await lock.release()
        except Exception as exc:
            # Não crítico: se o lock já expirou por TTL apesar do heartbeat
            # (ex: renovação falhou e não houve mais tentativa), release()
            # pode falhar por já não pertencer mais a este processo.
            log.warning(
                "Falha ao liberar lock '%s' (possivelmente já expirado): %s",
                lock_key,
                exc,
            )


async def _run_ingestion(
    folder_path: str,
    content_type: str,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    collection_name: str,
    engine_version_override: str | None = None,
    project_source: str = "UE5_Engine",
    sample_mode: bool = False,
    resolved_domain_id: str = "ue5",
) -> None:
    """Corpo real do pipeline de ingestão, protegido pelo lock do chamador."""
    root = Path(folder_path)
    batch_size = settings.ingestion_batch_size
    manifest_key = ingestion_manifest_key(collection_name)
    failures_key = ingestion_failures_key(collection_name)
    max_failures = settings.ingestion_max_consecutive_failures

    log.info(
        "Ingestão iniciada | pasta=%s tipo=%s coleção=%s manifesto=%s "
        "project_source=%s sample_mode=%s",
        root,
        content_type,
        collection_name,
        manifest_key,
        project_source,
        sample_mode,
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

    # BUSCA HÍBRIDA: garante que o espaço de vetor esparso 'keywords_sparse'
    # esteja habilitado ANTES de qualquer upsert deste run — idempotente e
    # aditivo (ver ensure_sparse_vectors_enabled), não bloqueante em caso de
    # falha (a ingestão pode prosseguir gravando só o vetor denso; o
    # backfill/retry da migração cobre esse caso depois).
    try:
        await ensure_sparse_vectors_enabled(qdrant)
    except Exception as exc:
        log.warning(
            "Falha ao habilitar sparse vectors na coleção '%s': %s. "
            "Ingestão prossegue apenas com vetor denso para este run.",
            collection_name,
            exc,
        )

    manifest_cache = await _load_manifest_cache(redis, manifest_key)
    failures_cache = await _load_failures_cache(redis, failures_key)

    # Extrai versão da engine baseada no domínio ativo
    engine_version, _ = _extract_engine_version(str(root), resolved_domain_id)
    if engine_version_override:
        engine_version = engine_version_override

    # CORREÇÃO (Canal C + Canal D): em sample_mode, também descobrimos .cpp
    # (implementação C++ real, Canal C) e .bp.json (Blueprints exportados
    # como JSON estruturado via BP_GraphExporter.py, Canal D) — gameplay em
    # projetos reais é híbrido C++/Blueprint, e ambos os canais capturam a
    # parte que .h sozinho não cobre. Para a Engine (sample_mode=False),
    # mantemos só .h como regra geral — mais barato e mais estrito, como
    # já era —, EXCETO para o conjunto restrito de módulos estratégicos de
    # gameplay moderno (Mover, Network Prediction, Chaos Mover/Vehicles,
    # ==========================================================================
    # Descoberta de arquivos por tipo de conteúdo e domínio
    # ==========================================================================
    # UE5: .h (headers), .cpp (implementação), .udn (docs), .bp.json (Blueprints)
    # Godot 4: .gd/.cs/.tscn/.tres/.h/.cpp (código-fonte, via Godot4IngestionStrategy)
    #          .rst/.xml/.md (documentação — .xml é a Class Reference oficial)
    strategic_cpp_count = 0
    if content_type == "source_code":
        if resolved_domain_id == "godot_4":
            # CORREÇÃO (pipeline duplo): antes só descobria .gd/.cs/.tscn —
            # .tres (recursos) e .h/.cpp (engine C++ nativo do Godot, Canal
            # 3 da strategy) nunca eram sequer encontrados. Godot4IngestionStrategy
            # decide a relevância/parsing de cada um internamente.
            extensions = (".gd", ".cs", ".tscn", ".tres", ".h", ".cpp")
            files = await _discover_files(root, extensions)
        elif sample_mode:
            # UE5 sample mode: headers, implementation e Blueprints
            extensions = (".h", ".cpp", ".bp.json")
            files = await _discover_files(root, extensions)
        else:
            # UE5 Engine mode: headers + cpps estratégicos
            extensions = (".h", ".cpp")
            discovered = await _discover_files(root, extensions)
            files = [
                p for p in discovered
                if p.suffix != ".cpp" or _is_strategic_gameplay_path(p)
            ]
            strategic_cpp_count = sum(1 for p in files if p.suffix == ".cpp")
    else:
        # Extensões de documentação por domínio
        if resolved_domain_id == "godot_4":
            # CORREÇÃO (pipeline duplo): .xml (Class Reference oficial) nunca
            # era descoberto — o parser dedicado (godot_class_xml_parser.py)
            # existia mas estava completamente desconectado da ingestão real.
            extensions = (".rst", ".xml", ".md")
        else:
            # UE5 e outros domínios usam .udn (documentação oficial da Epic)
            extensions = (".udn",)
        files = await _discover_files(root, extensions)

    total_files = len(files)

    log.info(
        "%d arquivo(s) '%s' encontrado(s) em '%s' para avaliação"
        "%s.",
        total_files,
        extensions,
        root,
        f" ({strategic_cpp_count} .cpp de módulos estratégicos em modo Engine)"
        if strategic_cpp_count
        else "",
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
        "embedding_failures": 0,
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

        # CORREÇÃO (idempotência/CRÍTICO): o delete antecipado de todos os
        # chunks do arquivo foi removido daqui. Ele rodava ANTES da chamada
        # à LLM documentadora, do embedding e do upsert — qualquer falha
        # nessas etapas (a mais provável do pipeline) deixava o arquivo sem
        # NENHUM chunk no Qdrant até a próxima ingestão bem-sucedida daquele
        # arquivo específico, um buraco de conteúdo no RAG por tempo
        # indefinido. Como o point_id é determinístico
        # (uuid5(source_file::chunk_id), ver _flush_batch), o upsert da nova
        # versão já sobrescreve in-place os chunks 0..N-1 da versão
        # anterior sem precisar de delete prévio. O único resíduo possível
        # (nova versão com MENOS chunks que a antiga) é limpo em
        # _commit_confirmed_files, e só DEPOIS do commit confirmado — nunca
        # há um instante em que o arquivo fica sem cobertura nenhuma.
        if existing_hash is not None:
            stats["modified"] += 1

        # Processamento e Geração de Chunks
        try:
            raw_text = raw_bytes.decode("utf-8", errors="ignore")

            # CORREÇÃO (pipeline duplo — auditoria RAG, Correção 4): Godot 4
            # agora é roteado inteiramente para Godot4IngestionStrategy, que
            # decide doc_type/parsing por extensão internamente (inclusive
            # .xml/.tres/.h/.gen.cpp, que o dispatch legado abaixo nunca
            # cobriu para este domínio). O dispatch legado por extensão
            # (content_type source_code/documentation) passa a servir
            # exclusivamente UE5.
            if resolved_domain_id == "godot_4":
                candidates = await _GODOT4_STRATEGY.process_file(
                    file_path, engine_version_override=engine_version,
                )
                chunks = _chunk_candidates_to_payload_dicts(candidates, project_source)
            elif content_type == "source_code":
                # UE5 - Blueprints (verificado ANTES de .cpp/.h porque
                # Path.suffix não reconhece extensão dupla ".bp.json")
                # NOTA: Blueprints são processados como JSON estruturado.
                # O exportador (BP_GraphExporter.py) já lineariza o grafo,
                # então tratamos como documentação estruturada.
                if file_path.name.endswith(".bp.json"):
                    chunks = await _process_md_or_rst_file(
                        file_path, raw_text, engine_version, project_source,
                        domain_id="ue5", doc_type="ue5_blueprint_graph",
                    )
                elif file_path.suffix == ".cpp":
                    # Canal C — implementação UE5
                    chunks = await _process_script_file(
                        file_path, raw_text, engine_version, project_source,
                        domain_id="ue5",
                    )
                else:
                    # Headers UE5 (.h)
                    chunks = await _process_header_file(
                        file_path, raw_text, engine_version, project_source,
                        sample_mode=sample_mode,
                        domain_id="ue5",
                    )
            else:
                # UE5 .udn
                chunks = await _process_md_or_rst_file(
                    file_path, raw_text, engine_version, project_source,
                    domain_id="ue5", doc_type="ue5_documentation",
                )

            if not chunks:
                stats["skipped"] += 1
                continue

            # --- FILTRAGEM DE CHUNKS VAZIOS / CABEÇALHOS ÓRFÃOS ---
            filtered_chunks = []
            for chunk in chunks:
                # Extrai o texto do chunk tratando de forma segura se ele for dict ou objeto
                text = chunk.get("summary_markdown", "") if isinstance(chunk, dict) else getattr(chunk, "summary_markdown", "")
                
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
                per_file_counts, embed_fails = await _flush_batch(
                    qdrant, collection_name, batch, domain_id=resolved_domain_id,
                )
                stats["chunks_indexed"] += sum(per_file_counts.values())
                stats["embedding_failures"] += embed_fails
                await _commit_confirmed_files(
                    redis, qdrant, collection_name, manifest_key, tracking, per_file_counts,
                    failures_key=failures_key, failures_cache=failures_cache,
                )

        except Exception as exc:
            stats["errors"] += 1
            tracking.pop(source_file_str, None)
            await _record_failure(redis, failures_key, failures_cache, relative_key)
            log.error("Erro processando '%s' — ignorando: %s", file_path, exc, exc_info=True)
            continue

    # Flush de resíduos finais — garante que TODO chunk pendente seja processado
    if pending:
        log.info(
            "Flush final de resíduos | chunks_pendentes=%d arquivos_envolvidos=%d",
            len(pending),
            len(set(c["source_file_root"] for c in pending)),
        )
        per_file_counts, embed_fails = await _flush_batch(
            qdrant, collection_name, pending, domain_id=resolved_domain_id,
        )
        stats["chunks_indexed"] += sum(per_file_counts.values())
        stats["embedding_failures"] += embed_fails
        await _commit_confirmed_files(
            redis, qdrant, collection_name, manifest_key, tracking, per_file_counts,
            failures_key=failures_key, failures_cache=failures_cache,
        )
        # Limpa o buffer após flush bem-sucedido
        pending.clear()

    # Identificação de falhas parciais (chunks perdidos no embedding/upsert limitaram commit)
    # NOTA: arquivos que permanecem em tracking após o flush final têm persistência parcial
    # real — seus chunks não foram confirmados devido a falhas de embedding ou upsert.
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