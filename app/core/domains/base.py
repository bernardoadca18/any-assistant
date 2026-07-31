"""
app/core/domains/base.py
=========================
Contrato base para um "domínio" de engine/framework suportado pelo RAG
(UE5, Godot 4, ou qualquer futuro plugin). Um domínio encapsula TUDO que
hoje está hardcoded e espalhado por rag_service.py / ingestion_service.py /
sparse_vector_service.py para Unreal Engine 5:

    - Prompts (system + decomposição)
    - Extensões de arquivo suportadas na ingestão
    - doc_types válidos (para a coluna doc_types.code)
    - Mapeamento doc_type -> linguagem do code fence markdown
    - Regex de tokenização do canal esparso (identificadores da linguagem)
    - Sanitização/parsing de conteúdo bruto por extensão

Nenhum destes campos deve ser lido diretamente de `settings` fora deste
módulo — settings.active_domain apenas seleciona QUAL profile é resolvido
pelo DomainRegistry (ver registry.py); todo o resto passa a vir do profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BaseDomainProfile:
    # Identidade
    domain_id: str                              # ex: "godot_4", "ue5"
    display_name: str                            # ex: "Godot Engine 4.x"
    default_session_title: str                   # ex: "Conversa Godot Engine 4"

    # Vector store
    default_collection_name: str                 # ex: "godot_4_docs_hybrid"

    # Ingestão
    supported_extensions: tuple[str, ...]         # ex: (".gd", ".cs", ".rst", ".tscn")
    doc_types: dict[str, str]                     # code -> display_name

    # Prompts (Fase 2 — RAG generation / decomposition)
    system_prompt: str
    decomposition_prompt: str

    # Apresentação de contexto
    code_fence_language_map: dict[str, str]       # doc_type -> "gdscript" | "csharp" | "cpp" | "markdown"
    default_code_fence_language: str = "text"

    # Canal esparso (BM25-like) — regex que separa um token bruto em partes
    # semânticas (CamelCase/snake_case/dígitos). Cada domínio tem convenções
    # de nomenclatura próprias (UE5: prefixos A/U/F/E; Godot: PascalCase de
    # nodes + snake_case idiomático de GDScript).
    sparse_camel_split_regex: str = r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+"

    extra: dict[str, Any] = field(default_factory=dict)

    # -- Hooks de ingestão, sobrescritos pelos profiles concretos -----------

    def sanitize_content(self, raw_content: str, extension: str) -> str:
        """
        Sanitização/limpeza específica do domínio antes do chunking
        (ex: remover diretivas Sphinx de um .rst, remover comentários de
        licença de um .gd). Implementação default: no-op.
        """
        return raw_content

    def extract_metadata(self, content: str, filepath: str) -> dict[str, Any]:
        """
        Extrai metadados estruturais específicos do domínio (nome de classe,
        signals, nodes, versão) para enriquecer o payload do Qdrant.
        Implementação default: dict vazio.
        """
        return {}

    def code_fence_for(self, doc_type: str) -> str:
        return self.code_fence_language_map.get(doc_type, self.default_code_fence_language)
