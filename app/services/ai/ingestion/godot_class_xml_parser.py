"""
app/services/ai/ingestion/godot_class_xml_parser.py
======================================================
Parser da Class Reference oficial da Godot (doc/classes/*.xml no repo
godotengine/godot). Sem chamada de LLM — mesmo princípio já aplicado ao
.rst: a doc oficial já é prosa estruturada, só precisa ser limpa do
BBCode-like markup próprio do Godot e normalizada.

Tradução snake_case -> PascalCase: a Class Reference documenta a API
usando a convenção nativa do GDScript (snake_case). A API C# do Godot 4
segue a convenção .NET (PascalCase) automaticamente — get_angle_to()
vira GetAngleTo(), member position vira Position, signal tree_entered
vira TreeEnteredEventHandler. Sem essa tradução, uma pergunta feita em
C# ("como uso GetAngleTo") não teria nenhum match léxico com o texto
indexado ("get_angle_to"), prejudicando o retrieval para o público-alvo
principal do domínio (dev que só programa em C#).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def snake_to_pascal(name: str) -> str:
    """get_angle_to -> GetAngleTo | tree_entered -> TreeEntered.
    Nomes já PascalCase (ex.: constantes como 'FLAG_MAX') passam sem alteração
    incorreta pois o split por '_' + capitalize já produz o resultado esperado."""
    if not name:
        return name
    return "".join(part.capitalize() for part in name.split("_") if part)


# Ordem importa: blocos [gdscript]/[csharp] precisam ser resolvidos ANTES do
# catch-all genérico de tags, senão o conteúdo interno se perde junto com a tag.
_BBCODE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[codeblock\](.*?)\[/codeblock\]", re.S), r"```\n\1\n```"),
    # A doc oficial frequentemente traz exemplo em GDScript E C# lado a lado
    # dentro de [codeblocks][gdscript]...[/gdscript][csharp]...[/csharp][/codeblocks].
    # Como o domínio é C#-first, descartamos o exemplo GDScript e mantemos só o C#.
    (re.compile(r"\[gdscript\].*?\[/gdscript\]", re.S), ""),
    (re.compile(r"\[csharp\](.*?)\[/csharp\]", re.S), r"```csharp\n\1\n```"),
    (re.compile(r"\[/?codeblocks\]"), ""),
    (re.compile(r"\[code\](.*?)\[/code\]", re.S), r"`\1`"),
    (re.compile(r"\[method\s+([\w.]+)\]"), r"\1()"),
    (re.compile(r"\[member\s+([\w.]+)\]"), r"\1"),
    (re.compile(r"\[signal\s+([\w.]+)\]"), r"\1"),
    (re.compile(r"\[constant\s+([\w.]+)\]"), r"\1"),
    (re.compile(r"\[enum\s+([\w.]+)\]"), r"\1"),
    (re.compile(r"\[param\s+(\w+)\]"), r"`\1`"),
    (re.compile(r"\[b\](.*?)\[/b\]", re.S), r"**\1**"),
    (re.compile(r"\[i\](.*?)\[/i\]", re.S), r"*\1*"),
    (re.compile(r"\[kbd\](.*?)\[/kbd\]", re.S), r"`\1`"),
    (re.compile(r"\[url=([^\]]+)\](.*?)\[/url\]", re.S), r"\2 (\1)"),
    (re.compile(r"\[/?[A-Za-z_][^\]]*\]"), ""),  # catch-all: [Node2D], [Vector2] etc.
]


def clean_bbcode(text: str) -> str:
    if not text:
        return ""
    for pattern, repl in _BBCODE_PATTERNS:
        text = pattern.sub(repl, text)
    lines = [ln.strip() for ln in text.strip().splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_class_xml(xml_bytes: bytes) -> dict:
    """Retorna estrutura normalizada de uma classe da Class Reference.
    Levanta ValueError se o XML não for uma classe (guarda contra .xml
    não relacionados encontrados acidentalmente durante o discover_files)."""
    root = ET.fromstring(xml_bytes)
    if root.tag != "class":
        raise ValueError(f"XML raiz '{root.tag}' não é uma <class> da Class Reference.")

    tutorials = [
        {"title": t.get("title", ""), "url": (t.text or "").strip()}
        for t in root.findall("./tutorials/link")
    ]

    methods = []
    for m in root.findall("./methods/method"):
        name = m.get("name", "")
        ret_el = m.find("return")
        params = [f"{p.get('type')} {p.get('name')}" for p in m.findall("param")]
        methods.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "return_type": ret_el.get("type") if ret_el is not None else "void",
            "params": params,
            "qualifiers": m.get("qualifiers", ""),
            "description": clean_bbcode(m.findtext("description") or ""),
        })

    members = []
    for mem in root.findall("./members/member"):
        name = mem.get("name", "")
        members.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "type": mem.get("type", ""),
            "default": mem.get("default", ""),
            "description": clean_bbcode(mem.text or ""),
        })

    signals = []
    for sig in root.findall("./signals/signal"):
        name = sig.get("name", "")
        params = [f"{p.get('type')} {p.get('name')}" for p in sig.findall("param")]
        signals.append({
            "name": name,
            "csharp_event_name": f"{snake_to_pascal(name)}EventHandler",
            "params": params,
            "description": clean_bbcode(sig.findtext("description") or ""),
        })

    constants = []
    for const in root.findall("./constants/constant"):
        name = const.get("name", "")
        constants.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "value": const.get("value", ""),
            "description": clean_bbcode(const.text or ""),
        })

    return {
        "name": root.get("name", ""),
        "inherits": root.get("inherits", ""),
        "brief_description": clean_bbcode(root.findtext("brief_description") or ""),
        "description": clean_bbcode(root.findtext("description") or ""),
        "tutorials": tutorials,
        "methods": methods,
        "members": members,
        "signals": signals,
        "constants": constants,
    }