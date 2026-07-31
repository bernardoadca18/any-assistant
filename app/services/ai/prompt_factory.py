"""
app/services/ai/prompt_factory.py
===================================
Fábrica fina que resolve (system_prompt, decomposition_prompt, code fence
language) a partir do domain_id ativo — substitui as constantes
_SYSTEM_PROMPT / _DECOMPOSITION_SYSTEM_PROMPT que hoje são hardcoded para
UE5 dentro de rag_service.py.

Uso em rag_service.py (ver PATCH_NOTES.md para os pontos exatos de
integração):

    from app.services.ai.prompt_factory import PromptFactory

    prompts = PromptFactory.get_prompts(domain_id)
    ... system_prompt=prompts.system_prompt ...
    ... {"role": "system", "content": prompts.decomposition_prompt} ...
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.domains.base import BaseDomainProfile
from app.core.domains.registry import domain_registry


@dataclass(frozen=True)
class ResolvedPrompts:
    domain_id: str
    system_prompt: str
    decomposition_prompt: str


class PromptFactory:
    """Sem estado — apenas encapsula o acesso ao domain_registry para que
    rag_service.py não precise conhecer a existência do registry
    diretamente (facilita mockar em teste unitário do pipeline RAG)."""

    @staticmethod
    def get_profile(domain_id: str) -> BaseDomainProfile:
        return domain_registry.get(domain_id)

    @staticmethod
    def get_prompts(domain_id: str) -> ResolvedPrompts:
        profile = domain_registry.get(domain_id)
        return ResolvedPrompts(
            domain_id=domain_id,
            system_prompt=profile.system_prompt,
            decomposition_prompt=profile.decomposition_prompt,
        )

    @staticmethod
    def get_code_fence(domain_id: str, doc_type: str) -> str:
        profile = domain_registry.get(domain_id)
        return profile.code_fence_for(doc_type)
