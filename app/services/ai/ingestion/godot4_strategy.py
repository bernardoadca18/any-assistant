"""
app/services/ai/ingestion/godot4_strategy.py
================================================
Implementação de IngestionStrategy para o domínio Godot Engine 4.x.

Quatro canais, cada um mapeado para um doc_type do Godot4Profile
(ver app/core/domains/godot4.py):

    .gd    -> gdscript_api     (parsing por regex + LLM documentadora)
    .cs    -> csharp_api       (idem, atributos C#)
    .rst   -> godot_docs_rst   (sanitização Sphinx, sem chamada de LLM —
                                 mesmo princípio do Canal B/.udn da UE5:
                                 documentação oficial já é prosa humana,
                                 não precisa de re-explicação por LLM)
    .tscn  -> scene_structure  (parsing estrutural puro, sem LLM — a
                                 hierarquia de nodes já é auto-descritiva)

Zero Raw Code Embedding (spec seção 5): `summary_markdown` (o que vira
embedding) é SEMPRE prosa conceitual gerada pela LLM documentadora ou
extraída da doc oficial — nunca o código/scene bruto. O código/scene bruto
vai inteiro em `raw_content` (Referência Direta), nunca embedado.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from app.core.config import settings
from app.core.domains.godot4 import GODOT4_PROFILE
from app.services.ai.ingestion.base_strategy import ChunkCandidate

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


def _split_gdscript_by_function(content: str) -> list[tuple[str, str]]:
    """Divide um .gd em (header_da_classe, corpo_da_função) por função
    top-level, preservando a declaração de classe/extends/signals/exports
    como contexto prefixado em cada grupo — mesmo princípio de
    ingestion_service._group_cpp_blocks para o Canal C da UE5."""
    lines = content.splitlines()
    header_lines: list[str] = []
    func_starts: list[int] = []

    for idx, line in enumerate(lines):
        if re.match(r"^\s*func\s+\w+\s*\(", line):
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
        if ext in (".tscn", ".tres"):
            return await self._process_scene(path, raw_content, engine_version)

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

        code_block = raw_content[:_MAX_CHUNK_CHARS]
        try:
            summary = await _document_via_llm(_CSHARP_DOCUMENTER_SYSTEM_PROMPT, code_block, self._documenter_model)
        except Exception as exc:  # noqa: BLE001
            log.warning("Godot4/.cs: falha ao documentar '%s' via LLM (%s) — usando fallback textual.", path, exc)
            summary = f"Godot C# class `{class_name}` — documentação automática indisponível."

        return [
            ChunkCandidate(
                title=f"{class_name} ({path.name})",
                summary_markdown=summary,
                raw_content=code_block,
                doc_type="csharp_api",
                source_file=str(path),
                engine_version=engine_version,
                extra_metadata=metadata,
            )
        ]

    # -- .rst ------------------------------------------------------------

    async def _process_rst(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
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

    # -- .tscn / .tres -----------------------------------------------------

    async def _process_scene(self, path: Path, raw_content: str, engine_version: str) -> list[ChunkCandidate]:
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
