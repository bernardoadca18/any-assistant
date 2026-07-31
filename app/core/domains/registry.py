"""
app/core/domains/registry.py
=============================
Ponto único de resolução de qual BaseDomainProfile está ativo para uma
dada requisição. Prioridade de resolução (primeira que resolver vence):

    1. domain_id explícito no payload da requisição (ChatRequest.domain_id /
       IngestRequest.domain_id) — permite multi-tenant real por chamada.
    2. Header HTTP `X-Domain-ID` (ver diagrama da spec — usado por
       scripts/validate_stack.py e por clientes que preferem header a payload).
    3. settings.active_domain (fallback de configuração — "godot_4" por
       padrão, ver app/core/config.py).

Nenhum destes profiles deve ser mutado em runtime — são singletons
imutáveis (BaseDomainProfile é frozen dataclass); "registrar" um domínio
novo significa adicionar seu profile ao dict abaixo, não editar um existente.
"""

from __future__ import annotations

from app.core.domains.base import BaseDomainProfile
from app.core.domains.godot4 import GODOT4_PROFILE
from app.core.domains.ue5 import UE5_PROFILE


class DomainRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, BaseDomainProfile] = {}

    def register(self, profile: BaseDomainProfile) -> None:
        if profile.domain_id in self._profiles:
            raise ValueError(f"Domain '{profile.domain_id}' já registrado.")
        self._profiles[profile.domain_id] = profile

    def get(self, domain_id: str) -> BaseDomainProfile:
        try:
            return self._profiles[domain_id]
        except KeyError as exc:
            raise ValueError(
                f"Domain '{domain_id}' não registrado. Domínios disponíveis: "
                f"{sorted(self._profiles)}"
            ) from exc

    def resolve(
        self,
        *,
        explicit_domain_id: str | None = None,
        header_domain_id: str | None = None,
        default_domain_id: str,
    ) -> BaseDomainProfile:
        """Resolve o profile ativo seguindo a prioridade documentada acima."""
        for candidate in (explicit_domain_id, header_domain_id, default_domain_id):
            if candidate:
                return self.get(candidate)
        raise ValueError("Nenhum domain_id resolvido e nenhum default_domain_id informado.")

    def list_domains(self) -> list[str]:
        return sorted(self._profiles)


# Singleton do processo — registrado no import deste módulo. Godot 4 é o
# domínio primário/default (Fase 4 da spec); UE5 é mantido como plugin
# opcional para compatibilidade retroativa com a coleção já existente.
domain_registry = DomainRegistry()
domain_registry.register(GODOT4_PROFILE)
domain_registry.register(UE5_PROFILE)
