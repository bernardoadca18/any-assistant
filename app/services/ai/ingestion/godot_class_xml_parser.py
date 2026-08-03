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

Campos adicionais (revisão 2):
    - default de parâmetros (<param default="...">) -> assinaturas com
      overloads opcionais ficam completas (ex.: get_node(path, owned=true)).
    - enum de constantes (<constant enum="...">) -> constantes que em C#
      viram membros de um enum aninhado (Node.ProcessModeEnum.Disabled)
      em vez de constante solta.
    - deprecated/experimental -> evita recomendar API obsoleta como se
      fosse atual.
    - xml_engine_version -> extraído do atributo `version` da própria
      tag <class>, mais confiável que um override externo genérico.
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


def _format_param(p: ET.Element) -> str:
    """type name [= default] — inclui o valor default quando presente no
    XML, essencial para overloads com parâmetros opcionais (comum na API
    de Node/Control/Camera2D)."""
    base = f"{p.get('type')} {p.get('name')}"
    default = p.get("default")
    return f"{base} = {default}" if default is not None else base


def _deprecation_note(el: ET.Element) -> str:
    """Godot marca itens obsoletos/experimentais via atributos
    'deprecated'/'experimental' no próprio elemento (podem conter uma
    mensagem de texto ou vir vazios). Sem isso o assistente pode
    recomendar API obsoleta como se fosse atual."""
    notes = []
    if el.get("deprecated") is not None:
        msg = el.get("deprecated") or "no replacement message provided"
        notes.append(f"⚠️ DEPRECATED: {msg}")
    if el.get("experimental") is not None:
        msg = el.get("experimental") or "API may change"
        notes.append(f"⚠️ EXPERIMENTAL: {msg}")
    return " | ".join(notes)


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

    xml_engine_version = root.get("version", "")

    tutorials = [
        {"title": t.get("title", ""), "url": (t.text or "").strip()}
        for t in root.findall("./tutorials/link")
    ]

    methods = []
    for m in root.findall("./methods/method"):
        name = m.get("name", "")
        ret_el = m.find("return")
        params = [_format_param(p) for p in m.findall("param")]
        methods.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "return_type": ret_el.get("type") if ret_el is not None else "void",
            "params": params,
            "qualifiers": m.get("qualifiers", ""),
            "deprecation_note": _deprecation_note(m),
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
            "deprecation_note": _deprecation_note(mem),
            "description": clean_bbcode(mem.text or ""),
        })

    signals = []
    for sig in root.findall("./signals/signal"):
        name = sig.get("name", "")
        params = [_format_param(p) for p in sig.findall("param")]
        signals.append({
            "name": name,
            "csharp_event_name": f"{snake_to_pascal(name)}EventHandler",
            "params": params,
            "deprecation_note": _deprecation_note(sig),
            "description": clean_bbcode(sig.findtext("description") or ""),
        })

    constants = []
    for const in root.findall("./constants/constant"):
        name = const.get("name", "")
        constants.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "value": const.get("value", ""),
            "enum": const.get("enum", ""),
            "deprecation_note": _deprecation_note(const),
            "description": clean_bbcode(const.text or ""),
        })

    # ---------------------------------------------------------------------------
    # PENDÊNCIA B: Suporte a <constructors>, <operators> e <theme_items>
    # ---------------------------------------------------------------------------
    # Essas tags são cruciais para tipos matemáticos (Vector2, Vector3, Color,
    # Transform2D) e UI (Control). Antes eram ignoradas silenciosamente.

    constructors = []
    for ctor in root.findall("./constructors/constructor"):
        name = ctor.get("name", "")  # Geralmente igual ao nome da classe
        params = [_format_param(p) for p in ctor.findall("param")]
        constructors.append({
            "name": name,
            "csharp_name": snake_to_pascal(name) if name else root.get("name", ""),
            "params": params,
            "qualifiers": ctor.get("qualifiers", ""),
            "deprecation_note": _deprecation_note(ctor),
            "description": clean_bbcode(ctor.findtext("description") or ""),
        })

    operators = []
    for op in root.findall("./operators/operator"):
        name = op.get("name", "")
        return_el = op.find("return")
        params = [_format_param(p) for p in op.findall("param")]
        operators.append({
            "name": name,
            # Operadores C# usam sintaxe específica (ex: operator +)
            "csharp_name": f"operator {snake_to_pascal(name)}" if name else "operator",
            "return_type": return_el.get("type") if return_el is not None else "void",
            "params": params,
            "deprecation_note": _deprecation_note(op),
            "description": clean_bbcode(op.findtext("description") or ""),
        })

    theme_items = []
    for item in root.findall("./theme_items/theme_item"):
        name = item.get("name", "")
        theme_items.append({
            "name": name,
            "csharp_name": snake_to_pascal(name),
            "type": item.get("type", ""),
            "default": item.get("default", ""),
            "deprecation_note": _deprecation_note(item),
            "description": clean_bbcode(item.text or ""),
        })

    return {
        "name": root.get("name", ""),
        "inherits": root.get("inherits", ""),
        "xml_engine_version": xml_engine_version,
        "brief_description": clean_bbcode(root.findtext("brief_description") or ""),
        "description": clean_bbcode(root.findtext("description") or ""),
        "tutorials": tutorials,
        "methods": methods,
        "members": members,
        "signals": signals,
        "constants": constants,
        # PENDÊNCIA B: novos campos
        "constructors": constructors,
        "operators": operators,
        "theme_items": theme_items,
    }