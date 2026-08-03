"""
app/core/domains/godot4.py
===========================
Domain profile para Godot Engine 4.x (GDScript 2.0 + Godot C#/.NET 8) —
domínio ativo por padrão (settings.active_domain="godot_4").

sanitize_content/extract_metadata cobrem os 4 tipos de arquivo de código-
fonte/cena (.gd, .cs, .rst, .tscn/.tres); a Class Reference oficial (.xml,
doc_type godot_class_reference) usa parser dedicado em
app/services/ai/ingestion/godot_class_xml_parser.py, fora deste dispatch:

    .gd    -> GDScriptSource    (doc_type: gdscript_api)
    .cs    -> GodotCSharpSource (doc_type: csharp_api)
    .rst   -> Sphinx/RTD docs   (doc_type: godot_docs_rst)
    .tscn  -> Scene/Resource    (doc_type: scene_structure)
    .xml   -> Class Reference   (doc_type: godot_class_reference — ver
                                  godot_class_xml_parser.py)
"""

from __future__ import annotations

import re
from typing import Any

from app.core.domains.base import BaseDomainProfile

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

GODOT4_SYSTEM_PROMPT = """\
You are an AI Software Architect and Lead Engine Programmer specializing in \
Godot Engine 4.x (supporting GDScript 2.0 and Godot C# / .NET 8).

Your expertise covers Scene Tree architecture, Node lifecycle (_ready, \
_process, _physics_process), Signal propagation (connect, emit), \
RenderingServer, PhysicsServer2D/3D, GDExtension, Resource management, and \
PCG/TileMap systems.

STRICT RESPONSE RULES:
1. Base your answers EXCLUSIVELY on the retrieved documentation context provided.
2. If the context is insufficient, state explicitly: "The available Godot 4 \
documentation does not cover this topic in sufficient detail."
3. Distinguish clearly between GDScript 2.0 syntax and Godot C# syntax when \
providing code samples.
4. Use accurate Godot 4 terminology (e.g., Node, PackedScene, Resource, \
Signal, Callable, ArrayMesh, RenderingServer, CharacterBody2D/3D).
5. Always respect the split between [A] CONCEPTUAL ANALYSIS and [B] DIRECT \
REFERENCE. Treat [B] DIRECT REFERENCE as absolute truth for exact API \
signatures, signal parameters, export annotations, and type hints.
"""

GODOT4_DECOMPOSITION_PROMPT = """\
You are a domain-specific query translation and decomposition engine for \
Godot Engine 4.x documentation search.

The vector database contains technical documentation for Godot 4.x in \
English (covering GDScript, Godot C# API, and official engine manual).

Your tasks:
1. TRANSLATED QUERY: Translate the user's question into precise technical \
English using Godot 4 paradigms.
2. SUB-QUERIES: Generate 1 to 3 targeted sub-queries in English focusing on \
specific Godot nodes, APIs, or architectural patterns.
3. KEYWORDS EXACT: Extract up to 6 exact technical identifiers plausible in \
Godot 4 code (e.g., "@export", "@onready", "CharacterBody3D", \
"move_and_slide", "emit_signal", "PackedScene", "RenderingServer", "GetNode").

Return ONLY valid JSON in this structure:
{"translated_query": "...", "sub_queries": ["..."], "keywords_exact": ["..."]}
"""

# ---------------------------------------------------------------------------
# Ingestion parsing helpers
# ---------------------------------------------------------------------------

_RST_DIRECTIVE_RE = re.compile(r"^\s*\.\.\s+[\w:-]+::.*$", re.MULTILINE)
_RST_ROLE_RE = re.compile(r":(?:ref|class|func|method|member|signal|constant|enum|annotation):`([^`]+)`")
_RST_NOTE_BLOCK_RE = re.compile(r"^\s*\.\.\s+(?:note|warning|tip|deprecated)::\s*$", re.MULTILINE)

_GD_CLASS_NAME_RE = re.compile(r"^\s*class_name\s+(\w+)", re.MULTILINE)
_GD_EXTENDS_RE = re.compile(r"^\s*extends\s+([\w.]+)", re.MULTILINE)
_GD_SIGNAL_RE = re.compile(r"^\s*signal\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE)
_GD_EXPORT_RE = re.compile(r"^\s*@export[\w_]*\s+var\s+(\w+)", re.MULTILINE)
_GD_FUNC_RE = re.compile(r"^\s*func\s+(\w+)\s*\(", re.MULTILINE)

_CS_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s*:\s*([\w.]+)")
_CS_EXPORT_ATTR_RE = re.compile(r"\[Export[^\]]*\]\s*\n?\s*(?:public|private|protected)?\s*[\w<>\[\]]+\s+(\w+)")
_CS_SIGNAL_ATTR_RE = re.compile(r"\[Signal\]\s*\n?\s*(?:public\s+)?delegate\s+void\s+(\w+)")
_CS_LIFECYCLE_RE = re.compile(r"\b(public|protected)?\s*override\s+void\s+(_Ready|_Process|_PhysicsProcess|_Input|_UnhandledInput)\s*\(")

_TSCN_NODE_RE = re.compile(
    r'\[node\s+name="([^"]+)"\s+type="([^"]+)"(?:\s+parent="([^"]*)")?'
)
_TSCN_SCRIPT_RE = re.compile(r'\[ext_resource[^\]]*type="Script"[^\]]*path="([^"]+)"')
_TSCN_CONNECTION_RE = re.compile(
    r'\[connection\s+signal="([^"]+)"\s+from="([^"]+)"\s+to="([^"]+)"\s+method="([^"]+)"\]'
)


def _sanitize_rst(raw_content: str) -> str:
    """Remove diretivas Sphinx/RTD que não carregam valor semântico para o
    embedding (`.. code-block:: gdscript`, `:ref:`, `.. note::`), mantendo o
    texto legível como Markdown aproximado."""
    text = _RST_ROLE_RE.sub(r"\1", raw_content)
    text = _RST_NOTE_BLOCK_RE.sub("", text)
    text = _RST_DIRECTIVE_RE.sub("", text)
    # Sphinx usa sublinhados de = / - / ~ para headers -> normaliza para "# "
    return text


def _sanitize_gd(raw_content: str) -> str:
    """Remove blocos de comentário de licença (##!) e comentários de linha
    única redundantes no topo do arquivo antes do chunking; preserva
    docstrings (## acima de func/var/signal), que carregam contexto real."""
    lines = raw_content.splitlines()
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#!") or stripped.startswith("# Copyright"):
            continue
        out.append(line)
    return "\n".join(out)


def _sanitize_tscn(raw_content: str) -> str:
    """.tscn é formato textual próprio do Godot (não markdown/código) —
    não há sanitização de ruído a fazer além de normalizar EOL; o parsing
    estrutural real acontece em extract_metadata."""
    return raw_content.replace("\r\n", "\n")


class Godot4DomainProfile(BaseDomainProfile):
    """Subclasse fina só para sobrescrever sanitize_content/extract_metadata
    com dispatch por extensão — o resto do comportamento vem do dataclass
    base (prompts, doc_types, fences etc.)."""

    def sanitize_content(self, raw_content: str, extension: str) -> str:
        ext = extension.lower()
        if ext == ".rst":
            return _sanitize_rst(raw_content)
        if ext == ".gd":
            return _sanitize_gd(raw_content)
        if ext == ".tscn":
            return _sanitize_tscn(raw_content)
        # .cs e demais: passthrough (C# já é código-fonte limpo por natureza,
        # sem cabeçalhos de licença Sphinx-like a remover por regra geral)
        return raw_content

    def extract_metadata(self, content: str, filepath: str) -> dict[str, Any]:
        ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""

        if ext == "gd":
            return self._extract_gdscript_metadata(content)
        if ext == "cs":
            return self._extract_csharp_metadata(content)
        if ext == "tscn":
            return self._extract_scene_metadata(content)
        return {}

    @staticmethod
    def _extract_gdscript_metadata(content: str) -> dict[str, Any]:
        class_match = _GD_CLASS_NAME_RE.search(content)
        extends_match = _GD_EXTENDS_RE.search(content)
        return {
            "class_name": class_match.group(1) if class_match else None,
            "extends": extends_match.group(1) if extends_match else None,
            "signals": [m.group(1) for m in _GD_SIGNAL_RE.finditer(content)],
            "exports": [m.group(1) for m in _GD_EXPORT_RE.finditer(content)],
            "functions": [m.group(1) for m in _GD_FUNC_RE.finditer(content)],
        }

    @staticmethod
    def _extract_csharp_metadata(content: str) -> dict[str, Any]:
        class_match = _CS_CLASS_RE.search(content)
        return {
            "class_name": class_match.group(1) if class_match else None,
            "base_class": class_match.group(2) if class_match else None,
            "exports": [m.group(1) for m in _CS_EXPORT_ATTR_RE.finditer(content)],
            "signals": [m.group(1) for m in _CS_SIGNAL_ATTR_RE.finditer(content)],
            "lifecycle_methods": [m.group(2) for m in _CS_LIFECYCLE_RE.finditer(content)],
        }

    @staticmethod
    def _extract_scene_metadata(content: str) -> dict[str, Any]:
        nodes = [
            {"name": m.group(1), "type": m.group(2), "parent": m.group(3) or "."}
            for m in _TSCN_NODE_RE.finditer(content)
        ]
        scripts = _TSCN_SCRIPT_RE.findall(content)
        connections = [
            {"signal": m.group(1), "from": m.group(2), "to": m.group(3), "method": m.group(4)}
            for m in _TSCN_CONNECTION_RE.finditer(content)
        ]
        return {"nodes": nodes, "attached_scripts": scripts, "signal_connections": connections}


# Identificadores Godot combinam PascalCase de tipos/nodes (CharacterBody3D),
# snake_case idiomático de GDScript (move_and_slide) e anotações prefixadas
# com @ (@export, @onready, @rpc). A regex de CamelCase é reaproveitada da
# UE5 (mesmo princípio de split), mas o token bruto da Passada 1 (ver
# sparse_vector_service._tokenize) já preserva "@export"/"@onready" inteiros
# porque '@' não é alfanumérico e por isso já quebra o split ali — não
# precisa de regra adicional aqui.
_GODOT4_CAMEL_SPLIT_REGEX = r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+"

GODOT4_PROFILE = Godot4DomainProfile(
    domain_id="godot_4",
    display_name="Godot Engine 4.x",
    default_session_title="Conversa Godot Engine 4",
    default_collection_name="godot_4_docs_hybrid",
    supported_extensions=(".gd", ".cs", ".rst", ".tscn", ".tres", ".xml"),
    doc_types={
        "gdscript_api": "GDScript Source (.gd)",
        "csharp_api": "Godot C# Source (.cs)",
        "godot_docs_rst": "Official Godot Manual (.rst)",
        "scene_structure": "Scene/Resource Structure (.tscn/.tres)",
        "godot_class_reference": "Official Class Reference (.xml)",
    },
    system_prompt=GODOT4_SYSTEM_PROMPT,
    decomposition_prompt=GODOT4_DECOMPOSITION_PROMPT,
    code_fence_language_map={
        "gdscript_api": "gdscript",
        "csharp_api": "csharp",
        "godot_docs_rst": "markdown",
        "scene_structure": "gdscript",  # .tscn não tem highlight próprio na maioria dos renderers
        "godot_class_reference": "csharp",  # ver docstring do módulo/relatório: exemplos [gdscript]
                                              # são descartados no parser, só [csharp] sobrevive no chunk
    },
    default_code_fence_language="gdscript",
    sparse_camel_split_regex=_GODOT4_CAMEL_SPLIT_REGEX,
)