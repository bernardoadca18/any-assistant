"""
app/services/ai/ingestion/godot4_strategy.py
================================================
Implementação de IngestionStrategy para o domínio Godot Engine 4.x.

Seis canais, cada um mapeado para um doc_type do Godot4Profile
(ver app/core/domains/godot4.py):

    .gd    -> gdscript_api          (parsing por regex + LLM documentadora)
    .cs    -> csharp_api            (idem, atributos C#; chunking por membro)
    .h/.gen.cpp -> cpp_engine_api   (Godot engine C++ headers/source com
                                      GDCLASS/GDVIRTUAL/ClassDB binds;
                                      enviado à LLM documentadora — Canal 3)
    .rst   -> godot_docs_rst        (sanitização Sphinx, sem chamada de LLM —
                                      mesmo princípio do Canal B/.udn da UE5:
                                      documentação oficial já é prosa humana,
                                      não precisa de re-explicação por LLM)
    .tscn/.tres -> scene_structure  (parsing estrutural puro, sem LLM — a
                                      hierarquia de nodes já é auto-descritiva)
    .xml   -> godot_class_reference (parsing da Class Reference oficial via
                                      godot_class_xml_parser.py, escopo
                                      restrito a doc/classes/*.xml — sem LLM,
                                      doc oficial já é referência estruturada)

Zero Raw Code Embedding (spec seção 5): `summary_markdown` (o que vira
embedding) é SEMPRE prosa conceitual gerada pela LLM documentadora ou
extraída da doc oficial — nunca o código/scene bruto. O código/scene bruto
vai inteiro em `raw_content` (Referência Direta), nunca embedado.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app.core.config import settings
from app.core.domains.godot4 import GODOT4_PROFILE
from app.services.ai.ingestion.base_strategy import ChunkCandidate
from app.services.ai.ingestion.godot_class_xml_parser import parse_class_xml

log = logging.getLogger(__name__)

_MAX_CHUNK_CHARS = settings.ingestion_max_chunk_chars
_MAX_HEADER_CHARS = settings.ingestion_max_header_chars  # reaproveitado como teto p/ conteúdo enviado à LLM

_GDSCRIPT_DOCUMENTER_SYSTEM_PROMPT = """\
You are a senior Godot 4 technical writer. Given a GDScript class body, \
write a short, educational Markdown summary (max ~200 words) explaining:
- What this class/node is responsible for (gameplay intent, not syntax).
- Key exported properties (@export) and what they control.
- Key signals emitted and when.
- Key public functions and their role in the Scene Tree lifecycle.

C# EQUIVALENCE CONTEXT: When documenting GDScript code, highlight conceptual \
terms and patterns that facilitate C# interoperability:
- Mention signals as "events in C#" and show the C# event name pattern \
  (e.g., signal 'health_changed' → C# event 'HealthChanged').
- For async patterns, mention the C# equivalent (e.g., 'await' → 'ToSignal' \
  or 'async/await' in C#).
- For type hints, mention C# types where relevant (e.g., 'NodePath' \
  remains 'NodePath' in C#, 'Array' → 'Godot.Collections.Array<T>').
- Note that GDScript's '@export' maps to '[Export]' attribute in C#, \
  '@onready' has no direct C# equivalent (use GetNode<T>() in _Ready()).

Do not restate the code line-by-line. Do not invent behavior not implied \
by the code. Output plain Markdown only, no code fences.
"""

_CSHARP_DOCUMENTER_SYSTEM_PROMPT = """\
You are a senior Godot 4 C# technical writer. Given a C# class deriving \
from a Godot type, write a short, educational Markdown summary (max ~200 \
words) explaining its gameplay responsibility, [Export] fields, [Signal] \
delegates, and which Godot lifecycle overrides (_Ready/_Process/\
_PhysicsProcess) it implements and why. Output plain Markdown only.
"""

# ---------------------------------------------------------------------------
# C++ Engine Source Filter (Godot 4.x)
# ---------------------------------------------------------------------------

# Godot C++ headers use specific macros and inheritance patterns
_GODOT_CPP_CLASS_RE = re.compile(
    r"class\s+(?:\w+_API\s+)?(\w+)\s*(?::\s*(?:public|private|protected)\s+(\w+))?"
)
_GODOT_CPP_GDCLASS_RE = re.compile(r"GDCLASS\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)")
_GODOT_CPP_GDVIRTUAL_RE = re.compile(r"GDVIRTUAL[0-9]?[A-Z]*\s*\(\s*(\w+)")
_GODOT_CPP_BIND_METHOD_RE = re.compile(
    r"ClassDB::bind_method\s*\(\s*"
    r"(?:D_METHOD\s*\(\s*\"([^\"]+)\"[^)]*\)\s*,|\"([^\"]+)\"\s*,)"
)
_GODOT_CPP_BIND_ENUM_RE = re.compile(r"BIND_ENUM_CONSTANT\s*\(\s*(\w+)\s*\)")
_GODOT_CPP_BIND_BITFIELD_RE = re.compile(r"BIND_BITFIELD_FLAG\s*\(\s*(\w+)\s*\)")
_GODOT_CPP_PROPERTY_RE = re.compile(
    r"ADD_PROPERTY\s*\(\s*PropertyInfo\s*\([^)]+\)\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\)"
)

# Base classes that indicate a Godot gameplay-relevant class
_GODOT_RELEVANT_BASES: frozenset[str] = frozenset({
    "Node", "Resource", "RefCounted", "Object", "Control", "Node3D", "Node2D",
    "Sprite2D", "Sprite3D", "CharacterBody2D", "CharacterBody3D", "RigidBody2D",
    "RigidBody3D", "AnimatableBody2D", "AnimatableBody3D", "StaticBody2D", "StaticBody3D",
    "Area2D", "Area3D", "Camera2D", "Camera3D", "Light2D", "Light3D", "VisualInstance3D",
    "MeshInstance3D", "CollisionShape2D", "CollisionShape3D", "RayCast2D", "RayCast3D",
    "AudioStreamPlayer", "AudioStreamPlayer2D", "AudioStreamPlayer3D",
    "AnimationPlayer", "AnimationTree", "Tween", "Timer", "ShaderMaterial",
    "GDScript", "Script", "PluginScript", "Shader", "Texture", "Mesh", "PackedScene",
    "Viewport", "Window", "CanvasItem", "NodePath", "StringName", "Callable",
    "Signal", "Dictionary", "Array", "Variant", "Vector2", "Vector3", "Transform2D",
    "Transform3D", "Quaternion", "Color", "Rect2", "Rect2i", "AABB", "Basis",
    "Plane", "Projection", "RID",
})

# Directories to ignore when scanning Godot engine source
_GODOT_IGNORED_DIRS: frozenset[str] = frozenset({
    "thirdparty", "platform", "drivers", "tests", "editor", "doc", "misc",
    ".git", "__pycache__", "bin", "obj", "gen", "modules/freetype",
    "modules/jsonrpc", "modules/mono/editor",  # Mono editor is C# tooling
})

_CPP_DOCUMENTER_SYSTEM_PROMPT = """\
You are a senior Godot Engine C++ developer and technical writer at W4 Games. \
Given a C++ header file from the Godot 4.x engine source, write an educational \
Markdown summary (max ~300 words) explaining:

- What this class does and its role in the Godot architecture
- Key GDCLASS inheritance and what base class it extends
- Important GDVIRTUAL methods that can be overridden in GDScript/C#
- Key ClassDB::bind_method exposed methods and their purpose
- Important properties added via ADD_PROPERTY
- BIND_ENUM_CONSTANT enums defined for this class

Focus on what a game developer needs to know to USE this class, not internal \
implementation details. Mention GDScript/C# equivalents where relevant. \
Output plain Markdown only, no code fences for the main content.
"""

# ---------------------------------------------------------------------------
# Regex de chunking estrutural (GDScript / C#)
# ---------------------------------------------------------------------------

_CSHARP_MEMBER_RE = re.compile(
    r"^\s*(?:public|private|protected|internal)\s+"
    r"(?:static\s+|override\s+|virtual\s+|async\s+|sealed\s+|abstract\s+|new\s+|partial\s+)*"
    r"[\w<>\[\],.\?]+\s+\w+\s*\("
)


def _split_gdscript_by_function(content: str) -> list[tuple[str, str]]:
    """Divide um .gd em (header_da_classe, corpo_da_função) por função
    top-level (incluindo `static func`), preservando a declaração de
    classe/extends/signals/exports como contexto prefixado em cada grupo —
    mesmo princípio de ingestion_service._group_cpp_blocks para o Canal C
    da UE5."""
    lines = content.splitlines()
    header_lines: list[str] = []
    func_starts: list[int] = []

    for idx, line in enumerate(lines):
        if re.match(r"^\s*(?:static\s+)?func\s+\w+\s*\(", line):
            func_starts.append(idx)
        elif not func_starts and not re.match(r"^\s*#", line):
            header_lines.append(line)

    if not func_starts:
        return [("\n".join(header_lines), content)]

    header = "\n".join(header_lines).strip()
    groups: list[tuple[str, str]] = []
    for i, start in enumerate(func_starts):
        end = func_starts[i + 1] if i + 1 < len(func_starts) else len(lines)
        groups.append((header, "\n".join(lines[start:end])))
    return groups


def _split_csharp_by_member(content: str) -> list[tuple[str, str]]:
    """Divide um .cs por método/propriedade top-level, preservando
    usings/campos/[Export]/[Signal] anteriores como header — mesmo
    princípio de _split_gdscript_by_function. Heurística por regex, não
    parser completo de C#: suficiente para os padrões idiomáticos de
    scripts Godot (classe única por arquivo, sem nesting profundo).

    CORREÇÃO (ALTO): antes, _process_csharp não fazia nenhum chunking —
    o arquivo inteiro virava um único ChunkCandidate truncado em
    _MAX_CHUNK_CHARS, perdendo silenciosamente todo método além do
    truncamento. Esta função espelha o comportamento já existente para
    GDScript."""
    lines = content.splitlines()
    header_lines: list[str] = []
    member_starts: list[int] = []
    pending_attr_start: int | None = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not member_starts and stripped.startswith("[") and pending_attr_start is None:
            pending_attr_start = idx
            continue
        if _CSHARP_MEMBER_RE.match(line):
            member_starts.append(pending_attr_start if pending_attr_start is not None else idx)
            pending_attr_start = None
            continue
        if not member_starts:
            header_lines.append(line)
        pending_attr_start = None

    if not member_starts:
        return [("\n".join(header_lines), content)]

    header = "\n".join(header_lines).strip()
    groups: list[tuple[str, str]] = []
    for i, start in enumerate(member_starts):
        end = member_starts[i + 1] if i + 1 < len(member_starts) else len(lines)
        groups.append((header, "\n".join(lines[start:end])))
    return groups


def _split_rst_by_headers(content: str, root_title: str) -> list[tuple[str, str]]:
    """Split simples por linhas de sublinhado RST (===, ---, ~~~) — mesmo
    princípio de ingestion_service._hierarchical_markdown_chunker, adaptado
    para o estilo de header do RST em vez de Markdown '#'."""
    lines = content.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = root_title
    current_body: list[str] = []

    underline_re = re.compile(r"^[=\-~^\"]{3,}\s*$")

    i = 0
    while i < len(lines):
        line = lines[i]
        if i + 1 < len(lines) and underline_re.match(lines[i + 1]) and line.strip():
            if current_body:
                sections.append((current_title, current_body))
            current_title = line.strip()
            current_body = []
            i += 2
            continue
        current_body.append(line)
        i += 1

    if current_body:
        sections.append((current_title, current_body))

    return [(title, "\n".join(body).strip()) for title, body in sections if "\n".join(body).strip()]


async def _document_via_llm(system_prompt: str, code_block: str, model: str) -> str:
    """Chama o LiteLLM (mesmo client/retry já usado pelo Canal A da UE5 —
    ver rag_service.call_litellm) para gerar a Análise Semântica. Isolado
    aqui via import tardio para evitar import circular com rag_service."""
    from app.services.ai.rag_service import call_litellm  # import tardio — ver docstring do módulo

    truncated = code_block[:_MAX_HEADER_CHARS]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": truncated},
    ]
    answer, _prompt_tok, _completion_tok = await call_litellm(messages, model=model)
    return answer


# ---------------------------------------------------------------------------
# C++ Engine Source Filter
# ---------------------------------------------------------------------------

def _is_godot_relevant_cpp_header(content: str, filename: str) -> tuple[bool, str]:
    """
    Filter for Godot engine C++ headers that define gameplay-relevant classes.

    Similar to ingestion_service._is_gameplay_relevant_header for UE5, but
    adapted for Godot's C++ patterns:
    - Classes inheriting from Node, Resource, RefCounted, Object, etc.
    - GDCLASS macro usage (Godot's class binding system)
    - GDVIRTUAL methods (virtual methods exposed to scripting)
    - ClassDB::bind_method calls (methods exposed to GDScript/C#)
    - BIND_ENUM_CONSTANT (enums exposed to scripting)

    Args:
        content: Raw C++ header content
        filename: Header filename (for fallback class name extraction)

    Returns:
        Tuple of (is_relevant: bool, class_name: str)
        class_name is empty string if not relevant.
    """
    # Check for GDCLASS macro first (most reliable indicator)
    gdclass_match = _GODOT_CPP_GDCLASS_RE.search(content)
    if gdclass_match:
        class_name = gdclass_match.group(1)
        base_class = gdclass_match.group(2)
        # Verify base class is in our relevance list
        if base_class in _GODOT_RELEVANT_BASES or base_class.startswith(("Node", "Resource", "Control", "Viewport")):
            return True, class_name
        # GDCLASS with any base is still relevant (might be custom base)
        return True, class_name

    # Check for class declaration with relevant base
    class_match = _GODOT_CPP_CLASS_RE.search(content)
    if class_match:
        class_name = class_match.group(1)
        base_class = class_match.group(2)
        if base_class and base_class in _GODOT_RELEVANT_BASES:
            return True, class_name

    # Check for GDVIRTUAL methods (indicates script-exposed virtuals)
    if _GODOT_CPP_GDVIRTUAL_RE.search(content):
        # Extract class name from filename as fallback
        class_name = Path(filename).stem
        return True, class_name

    # Check for ClassDB::bind_method (indicates exposed API)
    if _GODOT_CPP_BIND_METHOD_RE.search(content):
        class_name = Path(filename).stem
        return True, class_name

    # Check for BIND_ENUM_CONSTANT (exposed enums)
    if _GODOT_CPP_BIND_ENUM_RE.search(content):
        class_name = Path(filename).stem
        return True, class_name

    return False, ""


def _clean_cpp_header(content: str) -> str:
    """
    Clean C++ header for LLM processing by removing:
    - #include directives (implementation detail)
    - Excessive blank lines
    - Copyright/license comments (usually at top)

    Preserves:
    - Class declarations
    - GDCLASS/GDVIRTUAL macros
    - Method signatures
    - Property declarations
    """
    lines = content.splitlines()
    cleaned_lines: list[str] = []
    in_copyright = True

    for line in lines:
        stripped = line.strip()

        # Skip include directives
        if stripped.startswith("#include"):
            continue

        # Skip copyright header at the beginning
        if in_copyright:
            if stripped.startswith("//") and any(
                kw in stripped.lower()
                for kw in ["copyright", "license", "permission", "spdx", "found"]
            ):
                continue
            if stripped.startswith("/*") or stripped.startswith("*"):
                continue
            in_copyright = False

        cleaned_lines.append(line)

    # Remove excessive blank lines
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _extract_cpp_metadata(content: str) -> dict[str, Any]:
    """
    Extract structured metadata from Godot C++ header.

    Returns dict with:
    - gdclass: tuple (class_name, base_class) if GDCLASS present
    - virtual_methods: list of GDVIRTUAL method names
    - bound_methods: list of method names bound via ClassDB::bind_method
    - enums: list of enum names bound via BIND_ENUM_CONSTANT
    - properties: list of property names added via ADD_PROPERTY
    """
    metadata: dict[str, Any] = {}

    # GDCLASS
    gdclass_match = _GODOT_CPP_GDCLASS_RE.search(content)
    if gdclass_match:
        metadata["gdclass"] = (gdclass_match.group(1), gdclass_match.group(2))

    # GDVIRTUAL methods
    metadata["virtual_methods"] = _GODOT_CPP_GDVIRTUAL_RE.findall(content)

    # ClassDB::bind_method
    metadata["bound_methods"] = _GODOT_CPP_BIND_METHOD_RE.findall(content)

    # BIND_ENUM_CONSTANT
    metadata["enums"] = _GODOT_CPP_BIND_ENUM_RE.findall(content)

    # BIND_BITFIELD_FLAG
    metadata["bitfields"] = _GODOT_CPP_BIND_BITFIELD_RE.findall(content)

    # ADD_PROPERTY
    metadata["properties"] = [
        (m.group(1), m.group(2)) for m in _GODOT_CPP_PROPERTY_RE.finditer(content)
    ]

    return metadata


class Godot4IngestionStrategy:
    domain_id = GODOT4_PROFILE.domain_id
    supported_extensions = GODOT4_PROFILE.supported_extensions

    def __init__(self, documenter_model: str | None = None) -> None:
        self._documenter_model = documenter_model or settings.ingestion_documenter_model

    async def discover_files(self, root: Path) -> list[Path]:
        def _walk() -> list[Path]:
            found: list[Path] = []
            for ext in self.supported_extensions:
                found.extend(root.rglob(f"*{ext}"))
            return found

        return await asyncio.to_thread(_walk)

    async def process_file(
        self, path: Path, *, engine_version_override: str | None = None
    ) -> list[ChunkCandidate]:
        ext = path.suffix.lower()
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        raw_content = raw_bytes.decode("utf-8", errors="replace")
        engine_version = engine_version_override or "4.x"

        if ext == ".gd":
            return await self._process_gdscript(path, raw_content, engine_version)
        if ext == ".cs":
            return await self._process_csharp(path, raw_content, engine_version)
        if ext == ".rst":
            return await self._process_rst(path, raw_content, engine_version)
        # PENDÊNCIA A: Separação de doc_type para .tres vs .tscn
        if ext == ".tscn":
            return await self._process_scene(path, raw_content, engine_version)
        if ext == ".tres":
            return await self._process_resource(path, raw_content, engine_version)
        if ext == ".xml":
            # Escopo restrito a doc/classes/ — não varrer qualquer .xml do
            # repositório (presets de export, manifests Android, etc.) caso
            # o root aponte para o clone completo do godotengine/godot.
            if path.parent.name != "classes":
                log.debug("Godot4IngestionStrategy: .xml fora de doc/classes/, ignorando '%s'.", path)
                return []
            return await self._process_class_xml(path, raw_bytes, engine_version)
        # C++ Engine Source (Channel 3) — .h and .gen.cpp files
        if ext == ".h":
            return await self._process_cpp_header(path, raw_content, engine_version)
        if ext == ".cpp":
            # Only process .gen.cpp files (generated bindings)
            if path.stem.endswith(".gen"):
                return await self._process_cpp_gen(path, raw_content, engine_version)
            log.debug("Godot4IngestionStrategy: .cpp não-.gen ignorado '%s'.", path)
            return []

        log.debug("Godot4IngestionStrategy: extensão '%s' sem canal dedicado, ignorando '%s'.", ext, path)
        return []

    # -- .gd -----------------------------------------------------------

    async def _process_gdscript(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
        sanitized = await asyncio.to_thread(GODOT4_PROFILE.sanitize_content, raw_content, ".gd")
        metadata = await asyncio.to_thread(GODOT4_PROFILE.extract_metadata, sanitized, str(path))
        class_name = metadata.get("class_name") or path.stem

        groups = await asyncio.to_thread(_split_gdscript_by_function, sanitized)
        candidates: list[ChunkCandidate] = []

        for header, body in groups:
            code_block = f"{header}\n\n{body}".strip()
            if len(code_block) > _MAX_CHUNK_CHARS:
                code_block = code_block[:_MAX_CHUNK_CHARS]
            try:
                summary = await _document_via_llm(
                    _GDSCRIPT_DOCUMENTER_SYSTEM_PROMPT, code_block, self._documenter_model
                )
            except Exception as exc:  # noqa: BLE001 — canal degrada, não derruba o arquivo inteiro
                log.warning("Godot4/.gd: falha ao documentar '%s' via LLM (%s) — usando fallback textual.", path, exc)
                summary = f"GDScript class `{class_name}` — documentação automática indisponível."

            candidates.append(
                ChunkCandidate(
                    title=f"{class_name} ({path.name})",
                    summary_markdown=summary,
                    raw_content=code_block,
                    doc_type="gdscript_api",
                    source_file=str(path),
                    engine_version=engine_version,
                    extra_metadata=metadata,
                )
            )
        return candidates

    # -- .cs -----------------------------------------------------------

    async def _process_csharp(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
        metadata = await asyncio.to_thread(GODOT4_PROFILE.extract_metadata, raw_content, str(path))
        class_name = metadata.get("class_name") or path.stem

        groups = await asyncio.to_thread(_split_csharp_by_member, raw_content)
        candidates: list[ChunkCandidate] = []

        for header, body in groups:
            code_block = f"{header}\n\n{body}".strip()
            if len(code_block) > _MAX_CHUNK_CHARS:
                code_block = code_block[:_MAX_CHUNK_CHARS]
            try:
                summary = await _document_via_llm(
                    _CSHARP_DOCUMENTER_SYSTEM_PROMPT, code_block, self._documenter_model
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Godot4/.cs: falha ao documentar '%s' via LLM (%s) — usando fallback textual.", path, exc)
                summary = f"Godot C# class `{class_name}` — documentação automática indisponível."

            candidates.append(
                ChunkCandidate(
                    title=f"{class_name} ({path.name})",
                    summary_markdown=summary,
                    raw_content=code_block,
                    doc_type="csharp_api",
                    source_file=str(path),
                    engine_version=engine_version,
                    extra_metadata=metadata,
                )
            )
        return candidates

    # -- .rst (Documentation) ----------------------------------------------

    async def _process_rst(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
        """PENDÊNCIA C: Exclui arquivos em **/classes/** para evitar redundância.
        A fonte da verdade para classes DEVE ser exclusivamente o canal XML parsed."""
        # PENDÊNCIA C: Exclusão de diretório classes/ (redundância com XML)
        path_str = path.as_posix()
        if "/classes/" in path_str or "\\classes\\" in path_str:
            log.debug(
                "Ignorando .rst em classes/ (redundante com XML): %s", path
            )
            return []

        sanitized = await asyncio.to_thread(GODOT4_PROFILE.sanitize_content, raw_content, ".rst")
        # Sem LLM (mesma decisão do Canal B/.udn da UE5): doc oficial já é
        # prosa humana. summary_markdown == raw_content sanitizado.
        sections = _split_rst_by_headers(sanitized, path.stem)
        return [
            ChunkCandidate(
                title=title,
                summary_markdown=section,
                raw_content=section,
                doc_type="godot_docs_rst",
                source_file=str(path),
                engine_version=engine_version,
            )
            for title, section in sections
        ]

    # -- .tscn (Scene Structure) -------------------------------------------

    async def _process_scene(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
        """PENDÊNCIA A: .tscn exclusivamente para scene_structure.
        Focado na árvore de nós, hierarquia e conexões de sinais."""
        sanitized = await asyncio.to_thread(GODOT4_PROFILE.sanitize_content, raw_content, ".tscn")
        metadata = await asyncio.to_thread(GODOT4_PROFILE.extract_metadata, sanitized, str(path))

        node_lines = "\n".join(
            f"- {n['name']} ({n['type']}), parent={n['parent']}" for n in metadata.get("nodes", [])
        )
        conn_lines = "\n".join(
            f"- {c['from']}::{c['signal']} -> {c['to']}.{c['method']}()"
            for c in metadata.get("signal_connections", [])
        )
        summary = (
            f"# Scene: {path.name}\n\n## Node hierarchy\n{node_lines or '(none parsed)'}\n\n"
            f"## Signal connections\n{conn_lines or '(none)'}\n\n"
            f"## Attached scripts\n" + "\n".join(f"- {s}" for s in metadata.get("attached_scripts", []))
        )

        return [
            ChunkCandidate(
                title=f"Scene: {path.name}",
                summary_markdown=summary,
                raw_content=sanitized[:_MAX_CHUNK_CHARS],
                doc_type="scene_structure",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata=metadata,
            )
        ]

    # -- .tres (Resource Structure) ----------------------------------------
    # PENDÊNCIA A: Novo processador dedicado para arquivos de recurso.

    async def _process_resource(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
        """.tres exclusivamente para resource_structure.
        Focado em propriedades de recursos salvos, shaders, temas, sub-recursos."""
        sanitized = await asyncio.to_thread(GODOT4_PROFILE.sanitize_content, raw_content, ".tscn")  # mesmo formato
        metadata = await asyncio.to_thread(GODOT4_PROFILE.extract_metadata, sanitized, str(path))

        # Extrair informações específicas de recursos
        sub_resources = []
        resource_type = "Unknown"
        for line in sanitized.splitlines():
            if line.startswith("[sub_resource "):
                # Extrair tipo e ID do sub-recurso
                type_match = re.search(r'type="([^"]+)"', line)
                id_match = re.search(r'id="([^"]+)"', line)
                if type_match and id_match:
                    sub_resources.append({"type": type_match.group(1), "id": id_match.group(1)})
            elif line.startswith("[gd_resource "):
                type_match = re.search(r'type="([^"]+)"', line)
                if type_match:
                    resource_type = type_match.group(1)

        # Extrair propriedades do recurso principal
        properties = []
        in_properties = False
        for line in sanitized.splitlines():
            if line.strip() == "[resource]":
                in_properties = True
                continue
            if in_properties and "=" in line and not line.startswith("["):
                prop_match = re.match(r'^(\w+)\s*=\s*(.+)$', line.strip())
                if prop_match:
                    properties.append({"name": prop_match.group(1), "value": prop_match.group(2)})

        sub_resource_lines = "\n".join(
            f"- {sr['type']} (id: {sr['id']})" for sr in sub_resources
        )
        prop_lines = "\n".join(
            f"- {p['name']}: {p['value'][:100]}{'...' if len(p['value']) > 100 else ''}"
            for p in properties[:20]  # Limitar a 20 propriedades principais
        )

        summary = (
            f"# Resource: {path.name}\n\n"
            f"**Type:** {resource_type}\n\n"
            f"## Sub-resources\n{sub_resource_lines or '(none)'}\n\n"
            f"## Properties\n{prop_lines or '(none parsed)'}"
        )

        resource_metadata = {
            **metadata,
            "resource_type": resource_type,
            "sub_resource_count": len(sub_resources),
            "property_count": len(properties),
        }

        return [
            ChunkCandidate(
                title=f"Resource: {path.name}",
                summary_markdown=summary,
                raw_content=sanitized[:_MAX_CHUNK_CHARS],
                doc_type="resource_structure",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata=resource_metadata,
            )
        ]

    # -- .xml (Class Reference) -----------------------------------------

    async def _process_class_xml(
        self, path: Path, raw_bytes: bytes, engine_version: str
    ) -> list[ChunkCandidate]:
        try:
            parsed = await asyncio.to_thread(parse_class_xml, raw_bytes)
        except (ET.ParseError, ValueError) as exc:
            log.debug("Godot4/.xml: '%s' não é uma <class> válida (%s) — ignorando.", path, exc)
            return []

        class_name = parsed["name"] or path.stem
        # engine_version do próprio XML tem prioridade sobre o override
        # genérico "4.x" — cada arquivo de doc/classes/ carrega a versão
        # exata do build que o gerou (atributo `version` da tag <class>).
        resolved_version = parsed.get("xml_engine_version") or engine_version
        metadata_base = {
            "class_name": class_name,
            "inherits": parsed["inherits"],
            "tutorials": parsed["tutorials"],
        }
        candidates: list[ChunkCandidate] = []

        # Overview: brief + description + inherits + tutoriais oficiais
        overview_parts = [f"# {class_name}" + (f" (inherits {parsed['inherits']})" if parsed["inherits"] else "")]
        if parsed["brief_description"]:
            overview_parts.append(parsed["brief_description"])
        if parsed["description"]:
            overview_parts.append(parsed["description"])
        if parsed["tutorials"]:
            overview_parts.append(
                "## Tutorials\n" + "\n".join(f"- {t['title'] or t['url']}: {t['url']}" for t in parsed["tutorials"])
            )
        overview_text = "\n\n".join(overview_parts).strip()
        if overview_text:
            candidates.append(ChunkCandidate(
                title=f"{class_name} — Overview",
                summary_markdown=overview_text[:_MAX_CHUNK_CHARS],
                raw_content=overview_text[:_MAX_CHUNK_CHARS],
                doc_type="godot_class_reference",
                source_file=str(path),
                engine_version=resolved_version,
                extra_metadata={**metadata_base, "section": "overview"},
            ))

        def _method_text(m: dict) -> str:
            qualifier_tag = f" [{m['qualifiers']}]" if m.get("qualifiers") else ""
            note = f"\n{m['deprecation_note']}" if m.get("deprecation_note") else ""
            return (
                f"### {m['csharp_name']}({', '.join(m['params'])}) -> {m['return_type']}{qualifier_tag}\n"
                f"GDScript: `{m['name']}({', '.join(m['params'])})`{note}\n\n{m['description']}"
            )

        def _member_text(m: dict) -> str:
            note = f"\n{m['deprecation_note']}" if m.get("deprecation_note") else ""
            return (
                f"### {m['csharp_name']} : {m['type']} (default: {m['default']})\n"
                f"GDScript: `{m['name']}`{note}\n\n{m['description']}"
            )

        def _signal_text(s: dict) -> str:
            note = f"\n{s['deprecation_note']}" if s.get("deprecation_note") else ""
            return (
                f"### {s['csharp_event_name']}({', '.join(s['params'])})\n"
                f"GDScript signal: `{s['name']}`{note}\n\n{s['description']}"
            )

        def _constant_text(c: dict) -> str:
            enum_tag = f" (enum: {c['enum']})" if c.get("enum") else ""
            note = f"\n{c['deprecation_note']}" if c.get("deprecation_note") else ""
            return (
                f"### {c['csharp_name']} = {c['value']}{enum_tag}\n"
                f"GDScript: `{c['name']}`{note}\n\n{c['description']}"
            )

        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "methods",
            [_method_text(m) for m in parsed["methods"]],
        ))
        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "members",
            [_member_text(m) for m in parsed["members"]],
        ))
        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "signals",
            [_signal_text(s) for s in parsed["signals"]],
        ))
        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "constants",
            [_constant_text(c) for c in parsed["constants"]],
        ))

        # PENDÊNCIA B: Novas seções para constructors, operators, theme_items
        def _constructor_text(c: dict) -> str:
            note = f"\n{c['deprecation_note']}" if c.get("deprecation_note") else ""
            params_str = ", ".join(c["params"]) if c["params"] else ""
            return (
                f"### {c['csharp_name']}({params_str})\n"
                f"GDScript: `{c['name']}({params_str})`{note}\n\n{c['description']}"
            )

        def _operator_text(o: dict) -> str:
            note = f"\n{o['deprecation_note']}" if o.get("deprecation_note") else ""
            params_str = ", ".join(o["params"]) if o["params"] else ""
            return (
                f"### {o['csharp_name']}({params_str}) -> {o['return_type']}\n"
                f"GDScript operator: `{o['name']}`{note}\n\n{o['description']}"
            )

        def _theme_item_text(t: dict) -> str:
            note = f"\n{t['deprecation_note']}" if t.get("deprecation_note") else ""
            default_tag = f" (default: {t['default']})" if t.get("default") else ""
            return (
                f"### {t['csharp_name']} : {t['type']}{default_tag}\n"
                f"GDScript: `{t['name']}`{note}\n\n{t['description']}"
            )

        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "constructors",
            [_constructor_text(c) for c in parsed.get("constructors", [])],
        ))
        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "operators",
            [_operator_text(o) for o in parsed.get("operators", [])],
        ))
        candidates.extend(self._batch_class_section(
            class_name, path, resolved_version, metadata_base, "theme_items",
            [_theme_item_text(t) for t in parsed.get("theme_items", [])],
        ))

        return candidates

    @staticmethod
    def _batch_class_section(
        class_name: str, path: Path, engine_version: str, metadata_base: dict,
        section_name: str, items: list[str],
    ) -> list[ChunkCandidate]:
        """Agrupa itens (métodos/membros/signals/constants) em lotes que
        cabem em _MAX_CHUNK_CHARS, sem quebrar um item no meio — para
        classes grandes (Node, Control) isso gera N chunks em vez de 1
        chunk truncado perdendo membros inteiros."""
        if not items:
            return []
        candidates: list[ChunkCandidate] = []
        buffer: list[str] = []
        buffer_len = 0
        batch_idx = 1

        def _flush() -> None:
            nonlocal buffer, buffer_len, batch_idx
            if not buffer:
                return
            text = "\n\n".join(buffer)
            candidates.append(ChunkCandidate(
                title=f"{class_name} — {section_name.capitalize()} ({batch_idx})",
                summary_markdown=text,
                raw_content=text,
                doc_type="godot_class_reference",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata={**metadata_base, "section": section_name},
            ))
            buffer, buffer_len = [], 0
            batch_idx += 1

        for item in items:
            if buffer and buffer_len + len(item) > _MAX_CHUNK_CHARS:
                _flush()
            buffer.append(item)
            buffer_len += len(item)
        _flush()
        return candidates

    # -- .h (C++ Engine Headers) -------------------------------------------

    async def _process_cpp_header(
        self, path: Path, raw_content: str, engine_version: str
    ) -> list[ChunkCandidate]:
        """
        Process Godot engine C++ headers with GDCLASS/GDVIRTUAL bindings.

        Channel 3: Headers are filtered for gameplay-relevant classes and
        sent to the LLM documenter to generate Markdown summaries.

        Args:
            path: Path to the .h file
            raw_content: Raw C++ header content
            engine_version: Godot version string

        Returns:
            List of ChunkCandidate objects with documented C++ API
        """
        # Filter for gameplay-relevant headers
        is_relevant, class_name = _is_godot_relevant_cpp_header(raw_content, path.name)
        if not is_relevant:
            log.debug("Godot4/.h: header não-relevante ignorado '%s'.", path)
            return []

        # Clean the header for LLM processing
        cleaned = await asyncio.to_thread(_clean_cpp_header, raw_content)

        # Truncate if too large
        if len(cleaned) > _MAX_HEADER_CHARS:
            log.debug(
                "Godot4/.h: '%s' truncado de %d para %d chars.",
                path, len(cleaned), _MAX_HEADER_CHARS
            )
            cleaned = cleaned[:_MAX_HEADER_CHARS]

        # Extract metadata for enrichment
        metadata = _extract_cpp_metadata(raw_content)

        # Document via LLM
        try:
            summary = await _document_via_llm(
                _CPP_DOCUMENTER_SYSTEM_PROMPT,
                cleaned,
                self._documenter_model,
            )
        except Exception as exc:
            log.warning("Godot4/.h: falha na LLM para '%s': %s", path, exc)
            # Fallback: use cleaned code as summary
            summary = f"# {class_name}\n\nC++ header from Godot engine source.\n\n```cpp\n{cleaned[:1000]}\n```"

        return [
            ChunkCandidate(
                title=f"{class_name} (C++ Engine API)",
                summary_markdown=summary,
                raw_content=cleaned,
                doc_type="cpp_engine_api",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata=metadata,
            )
        ]

    # -- .gen.cpp (Generated C++ Bindings) ---------------------------------

    async def _process_cpp_gen(
        self, path: Path, raw_content: str, engine_version: str
    ) -> list[ChunkCandidate]:
        """
        Process Godot generated C++ binding files (.gen.cpp).

        These files contain the ClassDB bindings that expose C++ classes to
        GDScript/C#. They are valuable for understanding the exposed API but
        typically contain repetitive binding code, so we extract just the
        key information.

        Args:
            path: Path to the .gen.cpp file
            raw_content: Raw generated C++ content
            engine_version: Godot version string

        Returns:
            List of ChunkCandidate objects with binding information
        """
        # Extract class name from filename (e.g., "node.gen.cpp" -> "node")
        # or from the file content
        class_name = path.stem.replace(".gen", "")

        # Extract metadata about bindings
        metadata = _extract_cpp_metadata(raw_content)

        if not metadata.get("bound_methods") and not metadata.get("properties"):
            log.debug("Godot4/.gen.cpp: sem bindings expostos em '%s', ignorando.", path)
            return []

        # Build a summary of exposed API
        summary_parts = [f"# {class_name} — ClassDB Bindings"]

        if metadata.get("bound_methods"):
            summary_parts.append(
                "## Exposed Methods\n" +
                "\n".join(f"- `{m}()`" for m in metadata["bound_methods"][:20])
            )
            if len(metadata["bound_methods"]) > 20:
                summary_parts.append(f"  ... and {len(metadata['bound_methods']) - 20} more")

        if metadata.get("properties"):
            summary_parts.append(
                "## Properties\n" +
                "\n".join(f"- `{getter}` / `{setter}`" for getter, setter in metadata["properties"][:10])
            )

        if metadata.get("enums"):
            summary_parts.append(
                "## Exposed Enums\n" +
                "\n".join(f"- `{e}`" for e in metadata["enums"][:15])
            )

        summary = "\n\n".join(summary_parts)

        return [
            ChunkCandidate(
                title=f"{class_name} — Generated Bindings",
                summary_markdown=summary,
                raw_content=raw_content[:_MAX_CHUNK_CHARS],
                doc_type="cpp_engine_bindings",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata=metadata,
            )
        ]