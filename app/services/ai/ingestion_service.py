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
    # CORREÇÃO (Requisito 3 — expansão de keywords de segurança): termos
    # modernos do framework de gameplay da UE5 (Mover, Network Prediction,
    # Motion Warping, Parametric/Procedural Motion), para capturar esses
    # arquivos caso apareçam FORA dos caminhos estratégicos listados em
    # _STRATEGIC_ENGINE_MODULE_MARKERS (ex: um projeto que vendoriza esse
    # código em outro diretório, ou uma classe helper com o termo no nome
    # mas fora da pasta canônica do plugin).
    "Mover",
    "Prediction",
    "Simulation",
    "Warping",
    "Parametric",
    "Vehicle",
    "Procedural",
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

# Detecta assinaturas de definição de método/função estilo UE5 no .cpp:
# "RetType UClassName::MethodName(args) [const] [override] { ... }"
# Heurística deliberadamente simples (não é um parser C++ completo) — ver
# docstring de _split_cpp_into_functions para as limitações conhecidas.
_CPP_FUNCTION_SIG_RE = re.compile(
    r"^[ \t]*(?:[\w:\<\>,\*&~]+[ \t]+)+?"       # tipo de retorno (best-effort)
    r"(\w+)::(\w+)\s*\([^;{]*\)"                 # Classe::Metodo(args)
    r"(?:\s*const)?(?:\s*override)?\s*\{",       # abre o corpo direto na linha
    re.MULTILINE,
)

# Tamanho mínimo de função isolada para valer a pena documentar sozinha;
# funções menores que isso (getters/setters triviais) são agrupadas.
CPP_MIN_FUNCTION_CHARS: int = 80

# Limite de chars por grupo de funções enviado à LLM documentadora do Canal A
# em modo sample — evita reintroduzir o mesmo problema de truncamento que
# headers grandes já tinham, agora para .cpp (tipicamente mais verboso).
CPP_MAX_GROUP_CHARS: int = 6000

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
BP_JSON_MAX_CHARS: int = 8000


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

def _is_gameplay_relevant_header(
    content: str, filename: str, sample_mode: bool = False, strategic_override: bool = False
) -> tuple[bool, str]:
    """
    Decide se um header vale a pena documentar.

    Modo padrão (sample_mode=False, usado para a Engine): allowlist estrita
    por palavra-chave no nome da classe/base. Necessário para a Engine
    porque o volume de headers é ordens de magnitude maior — sem filtro,
    o custo de LLM documentadora explode e o corpus vira ruído (ver
    docstring do módulo).

    Modo sample (sample_mode=True, usado para projetos/samples de mecânica
    como Lyra, City Sample, Vehicle Sample): a allowlist é DESLIGADA. Esses
    projetos usam convenções de nome próprias (ex: ALyraWeapon, ULyraHealthSet)
    que não batem em nenhuma keyword genérica e seriam descartados
    silenciosamente pela allowlist — exatamente o conteúdo de mecânica real
    que se quer capturar. Em vez de manter uma lista de keywords sempre
    defasada, delega-se a decisão de relevância à LLM documentadora, que já
    tem instrução explícita de ignorar código puramente estrutural
    (construtores vazios, boilerplate GENERATED_BODY(), forward declarations).
    Volume de um projeto de sample é limitado (não é a Engine inteira),
    então o custo de deixar a LLM decidir é administrável.

    strategic_override=True (CORREÇÃO — ponto cego de indexação, ver
    _STRATEGIC_ENGINE_MODULE_MARKERS): mesmo em modo Engine
    (sample_mode=False), headers que pertencem a um módulo estratégico de
    gameplay moderno (Mover, Network Prediction, Chaos Mover/Vehicles,
    Motion Warping) também desligam a allowlist. Esses subsistemas usam
    structs auxiliares, controllers de retargeting e definições de
    simulação de baixo nível que frequentemente não herdam de
    UActorComponent/AActor e não carregam as keywords legadas no nome —
    exatamente o mesmo motivo pelo qual a allowlist é desligada em
    sample_mode, só que agora escopado a um conjunto restrito de caminhos
    da Engine em vez do projeto inteiro, para não reabrir a explosão de
    escopo que a allowlist existe para conter.
    """
    if sample_mode or strategic_override:
        stem = Path(filename).stem
        return True, stem

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

def _split_cpp_into_functions(content: str) -> list[tuple[str, str]]:
    """
    Divide um .cpp em blocos (assinatura_legível, corpo_completo) por função,
    usando a posição das assinaturas detectadas + balanceamento de chaves
    para encontrar o fim de cada corpo.

    Funções muito pequenas (getters/setters triviais, < CPP_MIN_FUNCTION_CHARS)
    são mantidas mas não geram grupo próprio — são agrupadas com a função
    vizinha para não gerar chamadas de LLM desperdiçadas em trechos triviais.

    Returns:
        Lista de tuplas (nome_legivel, texto_do_bloco). Pode incluir um
        bloco final "resto do arquivo" com qualquer código fora de função
        reconhecida (includes já removidos a montante, código de namespace
        solto, etc.) — nunca descarta conteúdo silenciosamente.
    """
    matches = list(_CPP_FUNCTION_SIG_RE.finditer(content))
    if not matches:
        return [("arquivo completo (nenhuma função reconhecida)", content)]

    blocks: list[tuple[str, str]] = []
    cursor = 0

    for i, match in enumerate(matches):
        class_name, method_name = match.group(1), match.group(2)
        body_start = match.end() - 1  # posição do '{' de abertura

        # Balanceamento de chaves para achar o fim real do corpo da função —
        # necessário porque regex sozinho não sabe "contar" aninhamento.
        depth = 0
        end = body_start
        for pos in range(body_start, len(content)):
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break
        else:
            # Chave nunca fechou (arquivo truncado/malformado) — pega até o
            # fim do arquivo em vez de travar ou perder o conteúdo.
            end = len(content)

        func_text = content[match.start():end]
        blocks.append((f"{class_name}::{method_name}", func_text))
        cursor = end

    # Conteúdo antes da primeira função e depois da última (namespace-level
    # code, definições fora de classe) — preservado, nunca descartado.
    leading = content[: matches[0].start()].strip()
    trailing = content[cursor:].strip()
    if leading:
        blocks.insert(0, ("código de topo do arquivo", leading))
    if trailing:
        blocks.append(("código remanescente do arquivo", trailing))

    return blocks


def _group_cpp_blocks(
    blocks: list[tuple[str, str]], max_group_chars: int
) -> list[tuple[list[str], str]]:
    """
    Agrupa blocos de função pequenos entre si (até max_group_chars) para
    evitar uma chamada de LLM por getter trivial, mas nunca junta um bloco
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


_CPP_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Gameplay Programmer at Epic Games, specialized in \
explaining HOW Unreal Engine 5 gameplay mechanics are actually implemented, \
based on real .cpp implementation code from a shipped project or official \
sample (e.g. Lyra, City Sample, Vehicle Sample, Game Animation Sample).

Your task: given one or more C++ function implementations from a real \
project, produce a Markdown guide that explains the MECHANIC or SYSTEM \
being implemented — not a line-by-line restatement of the code.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title describing the MECHANIC (e.g. \
"# Double Jump Implementation", "# PCG Procedural Foliage Scattering") — \
not just the raw function name.
3. Explain the ACTUAL LOGIC: what triggers this code, what decisions it \
makes, what state it reads/writes, and how it connects to other systems \
(e.g. Input, Animation Blueprint, Gameplay Ability System, PCG Graph).
4. Use H2 sections (`## `) to separate distinct concerns when the input \
covers more than one function, e.g. `## How it's triggered`, \
`## Core logic`, `## Integration points`.
5. Never invent behavior not present in the provided code. If the logic is \
unclear or incomplete from what's given, say so explicitly rather than \
guessing.
6. Be concise but complete — prioritize helping a developer replicate or \
adapt this mechanic in their own project.\
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


async def _process_cpp_file(
    path: Path, raw: str, ue_version: str, project_source: str
) -> list[dict]:
    """
    Processa um .cpp em modo sample: divide por função, agrupa blocos
    pequenos, e documenta cada grupo via LLM com foco em MECÂNICA (não API).

    CORREÇÃO — Injeção de contexto do header: o .cpp sozinho só mostra a
    IMPLEMENTAÇÃO; as macros UCLASS/UPROPERTY/UFUNCTION e as assinaturas
    completas (incluindo tipos de retorno e parâmetros com nomes
    descritivos, que no .cpp às vezes vêm abreviados) vivem no .h. Sem
    esse contexto, a LLM documentadora pode interpretar mal o propósito de
    uma variável ou sinalizador (ex: não sabe se uma UPROPERTY é
    replicada, BlueprintReadOnly, EditAnywhere, etc.) e produzir
    documentação tecnicamente correta mas rasa. O header é lido e limpo
    UMA VEZ por arquivo .cpp (não uma vez por grupo) para não multiplicar
    o custo de tokens quando o .cpp gera múltiplos grupos.

    Chamado em dois cenários (ver _run_ingestion): (1) sample_mode=True,
    onde TODOS os .cpp descobertos passam por aqui; e (2) modo Engine
    (sample_mode=False) para o subconjunto restrito de .cpp que pertence a
    um módulo estratégico de gameplay moderno listado em
    _STRATEGIC_ENGINE_MODULE_MARKERS (Mover, Network Prediction, Chaos
    Mover/Vehicles) — a lógica de predição de rede e simulação física
    desses subsistemas vive na implementação, não só na API pública do
    header, e por isso precisa do mesmo tratamento de Canal C que os
    projetos-sample já recebiam. Para o resto da Engine, .cpp continua
    fora do escopo de ingestão.
    """
    cleaned = _clean_cpp_header(raw)  # remove #includes, normaliza blank lines
    if not cleaned:
        log.debug(".cpp vazio após limpeza, ignorado: %s", path)
        return []

    header_context = await _load_header_context(path)

    # CORREÇÃO (bloqueio de event loop): o balanceamento de chaves em
    # _split_cpp_into_functions é CPU-bound (scan caractere-a-caractere)
    # e pode ser custoso em .cpp grandes e densos de funções — despachado
    # para thread pelo mesmo motivo que a leitura de disco é. _group_cpp_blocks
    # é barato (concatenação) e permanece síncrono.
    blocks = await asyncio.to_thread(_split_cpp_into_functions, cleaned)
    groups = _group_cpp_blocks(blocks, CPP_MAX_GROUP_CHARS)

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

        title = names[0] if names else path.stem

        # Contexto do header (se disponível) entra ANTES do bloco .cpp no
        # prompt do usuário, claramente rotulado como "declaração" para a
        # LLM não confundir com o corpo de implementação que é o foco real.
        header_section = (
            f"Header declarations for context (from {path.with_suffix('.h').name}):\n"
            f"```cpp\n{header_context}\n```\n\n"
            if header_context
            else ""
        )

        user_message = (
            f"Source file: {path.name}\n"
            f"Functions in this group: {', '.join(names)}\n\n"
            f"{header_section}"
            f"Implementation ({path.name}):\n"
            f"```cpp\n{group_text}\n```"
        )
        messages = [
            {"role": "system", "content": _CPP_DOCUMENTER_SYSTEM_PROMPT},
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
                doc_type="cpp_implementation",
                title=title,
                ue_version=ue_version,
                raw_content=group_text,
                project_source=project_source,
                source_file_root=str(path),
            )
        )

    return all_chunks


async def _process_header_file(
    path: Path, raw: str, ue_version: str, project_source: str, sample_mode: bool = False
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
        ue_version=ue_version,
        raw_content=cleaned,
        project_source=project_source,
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


async def _process_udn_file(
    path: Path, raw: str, ue_version: str, project_source: str
) -> list[dict]:
    clean_text, title = _sanitize_udn(raw)
    if not clean_text:
        log.debug("Documento .udn vazio após sanitização, ignorado: %s", path)
        return []

    return _build_chunks_with_metadata(
        markdown_text=clean_text,
        source_file=str(path),
        doc_type="udn_documentation",
        title=title or path.stem,
        ue_version=ue_version,
        raw_content=clean_text,
        project_source=project_source,
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

_BP_DOCUMENTER_SYSTEM_PROMPT = """\
You are a Senior Gameplay Programmer at Epic Games, specialized in \
explaining Unreal Engine 5 Blueprint mechanics from a STRUCTURED JSON \
representation of the Blueprint's graph, extracted via the Unreal Python \
API (not raw exported text).

The JSON contains:
- "ParentClass": the C++ or Blueprint class this Blueprint extends.
- "Variables": a list of {"name", "default_value"} for each meaningful \
Blueprint variable.
- "ExecutionFlow": a list of entry points, each with an "entry_point" \
(the triggering event — e.g. BeginPlay, a Custom Event, an input binding) \
and "steps" — a linearized sequence of the logic that entry point \
triggers. A step is either:
  - {"type": "step", "node": {...}} — a single action (e.g. a function \
call, identified by "function_name" or "event_name" when available).
  - {"type": "branch", "node": {...}, "branches": {label: [steps...]}} — \
a control-flow node (Branch, Sequence, ForEachLoop, Switch, etc.) whose \
execution splits into multiple named outcomes, each with its own \
sub-sequence of steps.
  - {"type": "loop_reference"} — the flow returned to an already-visited \
node in this branch (a loop was detected and the trace stopped there to \
avoid infinite recursion).

Your task: produce a Markdown guide explaining WHAT this Blueprint does as \
a gameplay mechanic — not a transcription of the JSON structure itself.

STRICT RULES:
1. Output ONLY Markdown. No preamble, no "Here is the documentation" filler.
2. Start with a single H1 title naming the mechanic or system (e.g. \
"# Double Jump Blueprint Logic", "# Interactable Door Trigger") — not just \
the raw Blueprint asset name.
3. State the PARENT CLASS this Blueprint extends.
4. Under `## Key Variables`, list the meaningful variables and their \
default values, explaining what each controls in gameplay terms.
5. Under `## Main Events`, cover each entry point in "ExecutionFlow": \
name the trigger, then describe IN YOUR OWN WORDS the sequence of logic \
it runs. When you encounter a "branch" step, explain the condition/split \
and summarize what happens in each named outcome. Do not just restate \
node names — explain what the sequence accomplishes as gameplay behavior.
6. Under `## Integration Points`, note any calls to other systems (e.g. \
Gameplay Ability System, Animation Blueprint, C++ base class functions, \
other Blueprints) identifiable from function_name/event_name values.
7. Never invent behavior not evidenced in the provided JSON. If
"ExecutionFlow" is empty or a referenced function is external and its \
behavior isn't shown, say so explicitly rather than guessing. If you see \
a "loop_reference" step, mention that the flow loops back rather than \
inventing what the loop does.
8. Be concise but complete — prioritize helping a developer understand or \
replicate this mechanic in their own project.\
"""


def _extract_blueprint_asset_name(path: Path) -> str:
    """
    Extrai o nome do asset a partir do path de um arquivo '<Nome>.bp.json'.

    path.stem sozinho NÃO resolve isso corretamente: para uma extensão
    dupla como '.bp.json', path.stem remove só o último componente
    ('.json'), retornando 'NomeDoAsset.bp' — com o sufixo '.bp' residual
    ainda grudado no nome. Removemos explicitamente o sufixo '.bp' restante
    quando presente.
    """
    stem = path.stem  # "BP_DoubleJump.bp" para "BP_DoubleJump.bp.json"
    if stem.endswith(".bp"):
        stem = stem[: -len(".bp")]
    return stem


async def _process_blueprint_file(
    path: Path, raw: str, ue_version: str, project_source: str
) -> list[dict]:
    """
    Processa um Blueprint exportado como JSON estruturado (.bp.json):
    parseia e valida o JSON, formata de volta como texto legível para o
    prompt, documenta via LLM com foco em mecânica de gameplay, e constrói
    os chunks finais.

    Só deve ser chamado em sample_mode=True — Blueprints exportados só
    fazem sentido no contexto de projetos-sample de mecânica, nunca para a
    Engine base (ver _run_ingestion: .bp.json só entra nas extensões
    descobertas quando sample_mode=True).
    """
    asset_name = _extract_blueprint_asset_name(path)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            "Falha ao parsear '%s' como JSON — arquivo malformado ou "
            "exportação incompleta do BP_GraphExporter.py: %s. Arquivo ignorado.",
            path, exc,
        )
        return []

    if not isinstance(payload, dict):
        log.error(
            "'%s' não contém um objeto JSON no nível raiz (esperado dict, "
            "recebido %s) — arquivo ignorado.",
            path, type(payload).__name__,
        )
        return []

    # Validação defensiva e tolerante: campos ausentes viram valores vazios
    # em vez de derrubar a exportação inteira do Blueprint. O próprio
    # BP_GraphExporter.py já documenta casos em que ExecutionFlow pode vir
    # vazio (ex: Blueprint sem EventGraph acessível) — isso não é erro
    # fatal, só uma documentação mais pobre para esse asset específico.
    parent_class = payload.get("ParentClass", "Unknown")
    variables = payload.get("Variables", [])
    execution_flow = payload.get("ExecutionFlow", [])

    if not execution_flow:
        log.warning(
            "Blueprint '%s' tem ExecutionFlow vazio — documentação será "
            "limitada a ParentClass/Variables.",
            asset_name,
        )

    normalized_payload = {
        "AssetName": asset_name,
        "ParentClass": parent_class,
        "Variables": variables,
        "ExecutionFlow": execution_flow,
    }

    formatted_json = json.dumps(normalized_payload, indent=2, ensure_ascii=False)

    if len(formatted_json) > BP_JSON_MAX_CHARS:
        log.warning(
            "JSON de '%s' tem %d chars formatado (limite=%d) — truncando "
            "antes de enviar à LLM documentadora. Fluxos de execução mais "
            "profundos podem ser cortados.",
            path, len(formatted_json), BP_JSON_MAX_CHARS,
        )
        formatted_json = formatted_json[:BP_JSON_MAX_CHARS]

    user_message = (
        f"Blueprint asset: {asset_name}\n\n"
        f"```json\n{formatted_json}\n```"
    )
    messages = [
        {"role": "system", "content": _BP_DOCUMENTER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    try:
        markdown, _pt, _ct = await call_litellm(
            messages=messages,
            model=settings.ingestion_documenter_model,
        )
    except Exception as exc:
        log.error(
            "Falha ao documentar Blueprint '%s': %s — arquivo ignorado.",
            path, exc,
        )
        return []

    return _build_chunks_with_metadata(
        markdown_text=markdown,
        source_file=str(path),
        doc_type="blueprint_logic",
        title=asset_name,
        ue_version=ue_version,
        raw_content=formatted_json,
        project_source=project_source,
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
    project_source: str,
    source_file_root: str | None = None,
) -> list[dict]:
    sections = _hierarchical_markdown_chunker(markdown_text, title)
    sections = _enforce_max_chunk_size(sections, settings.ingestion_max_chunk_chars)

    chunks: list[dict] = []
    for chunk_id, section_text in enumerate(sections):
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
                "ue_version": ue_version,
                # CORREÇÃO (custo/payload): raw_content é o mesmo conteúdo
                # integral do arquivo-fonte para TODOS os chunks gerados a
                # partir dele. Gravá-lo em cada chunk faz uma busca que
                # recupera N chunks do mesmo arquivo carregar N cópias
                # idênticas no prompt do rag_service.py (custo de tokens sem
                # ganho de informação), além de inflar o payload no Qdrant.
                # Guardamos o valor completo só no chunk_id==0; o
                # rag_service.py deve, ao montar a "Referência Direta", cair
                # de volta no raw_content do chunk_id==0 daquele
                # source_file_root quando o chunk recuperado tiver None.
                "raw_content": raw_content if chunk_id == 0 else None,
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
    """
    Remove TODOS os chunks de um arquivo-fonte, incondicionalmente.

    NÃO é mais chamada automaticamente pelo fluxo de `_run_ingestion` (ver
    correção de idempotência: `_delete_orphaned_chunks`, que remove só o
    resíduo de versões anteriores e apenas após o commit confirmado).
    Mantida aqui como utilitário para limpeza manual/administrativa
    completa de um arquivo específico (ex: ferramenta de CLI/admin que
    queira forçar a remoção total antes de uma reingestão manual).

    CORREÇÃO (Canal C / .cpp): chunks gerados a partir de .cpp usam um
    `source_file` SINTÉTICO com sufixo "#groupN" (ver _process_cpp_file),
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
) -> dict[str, int]:
    if not batch:
        return {}

    semaphore = asyncio.Semaphore(settings.ingestion_embedding_concurrency)

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
    project_source: str = "UE5_Engine",
    sample_mode: bool = False,
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
            ue_version_override=ue_version_override,
            project_source=project_source,
            sample_mode=sample_mode,
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
    ue_version_override: str | None = None,
    project_source: str = "UE5_Engine",
    sample_mode: bool = False,
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

    manifest_cache = await _load_manifest_cache(redis, manifest_key)
    failures_cache = await _load_failures_cache(redis, failures_key)
    ue_version = ue_version_override or _extract_ue_version(str(root))

    # CORREÇÃO (Canal C + Canal D): em sample_mode, também descobrimos .cpp
    # (implementação C++ real, Canal C) e .bp.json (Blueprints exportados
    # como JSON estruturado via BP_GraphExporter.py, Canal D) — gameplay em
    # projetos reais é híbrido C++/Blueprint, e ambos os canais capturam a
    # parte que .h sozinho não cobre. Para a Engine (sample_mode=False),
    # mantemos só .h como regra geral — mais barato e mais estrito, como
    # já era —, EXCETO para o conjunto restrito de módulos estratégicos de
    # gameplay moderno (Mover, Network Prediction, Chaos Mover/Vehicles,
    # Motion Warping) listado em _STRATEGIC_ENGINE_MODULE_MARKERS: ali o
    # .cpp também é descoberto e roteado ao Canal C, porque a lógica de
    # predição de rede e simulação física desses subsistemas específicos
    # vive na implementação, não só na API pública do header (ver ponto
    # cego descrito no comentário da constante). Isso NÃO reabre a
    # ingestão de .cpp para a Engine inteira — o restante permanece só .h.
    #
    # NOTA: ".bp.json" é uma extensão dupla. `fname.endswith(".bp.json")`
    # (usado dentro de _walk_files) funciona corretamente por comparação de
    # string — mas `Path.suffix` NÃO reconhece extensões duplas (retorna
    # só ".json"). Por isso o dispatch abaixo usa `file_path.name.endswith(...)`
    # em vez de `file_path.suffix ==` para este canal especificamente.
    strategic_cpp_count = 0
    if content_type == "source_code":
        if sample_mode:
            extensions = (".h", ".cpp", ".bp.json")
            files = await _discover_files(root, extensions)
        else:
            extensions = (".h", ".cpp")
            discovered = await _discover_files(root, extensions)
            # Segunda camada de segurança contra explosão de escopo: mesmo
            # que _walk_files já restrinja onde 'Private' é destravado, um
            # .cpp poderia teoricamente existir fora de Private/ (raro,
            # mas possível em alguns layouts de módulo) — este filtro
            # garante que NENHUM .cpp fora de um módulo estratégico chegue
            # ao pipeline em modo Engine, independentemente de onde foi
            # encontrado.
            files = [
                p for p in discovered
                if p.suffix != ".cpp" or _is_strategic_gameplay_path(p)
            ]
            strategic_cpp_count = sum(1 for p in files if p.suffix == ".cpp")
    else:
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

            if content_type == "source_code":
                if file_path.name.endswith(".bp.json"):
                    # Canal D — verificado ANTES de .cpp/.h porque
                    # Path.suffix não reconhece extensão dupla (".bp.json"
                    # tem suffix == ".json", que cairia no ramo Canal A por
                    # engano se checássemos .suffix primeiro). Só existe em
                    # sample_mode (extensions só inclui .bp.json quando
                    # sample_mode=True, ver acima).
                    chunks = await _process_blueprint_file(
                        file_path, raw_text, ue_version, project_source
                    )
                elif file_path.suffix == ".cpp":
                    # Canal C — existe em sample_mode (todo .cpp descoberto)
                    # E, desde a correção do ponto cego de indexação, em
                    # modo Engine para o subconjunto restrito de módulos
                    # estratégicos de gameplay moderno (Mover, Network
                    # Prediction, Chaos Mover/Vehicles) — ver o filtro de
                    # descoberta de arquivos acima e
                    # _STRATEGIC_ENGINE_MODULE_MARKERS.
                    chunks = await _process_cpp_file(
                        file_path, raw_text, ue_version, project_source
                    )
                else:
                    chunks = await _process_header_file(
                        file_path, raw_text, ue_version, project_source,
                        sample_mode=sample_mode,
                    )
            else:
                chunks = await _process_udn_file(
                    file_path, raw_text, ue_version, project_source
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
                per_file_counts = await _flush_batch(qdrant, collection_name, batch)
                stats["chunks_indexed"] += sum(per_file_counts.values())
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

    # Flush de resíduos finais
    if pending:
        per_file_counts = await _flush_batch(qdrant, collection_name, pending)
        stats["chunks_indexed"] += sum(per_file_counts.values())
        await _commit_confirmed_files(
            redis, qdrant, collection_name, manifest_key, tracking, per_file_counts,
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