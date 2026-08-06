"""
app/schemas/chat.py
===================
Schemas Pydantic para os endpoints de chat e RAG.

---------------------------------------------------------------------------
NOTA DE PROVENIÊNCIA DESTE ARQUIVO — leia antes de editar
---------------------------------------------------------------------------
Este arquivo é uma RECONCILIAÇÃO de duas versões que divergiram entre si:

    (A) A versão original do projeto (pré multi-domain/multi-tenant),
        completa e com Field(...) devidamente documentados, mas sem
        `domain_id`/`tenant_id`/`collection_name`.
    (B) Uma reconstrução best-effort feita numa rodada anterior, inferida
        apenas pelos campos que rag_service.py/chat.py acessavam na época
        — por ser uma reconstrução parcial, ela OMITIA campos que (A) tinha
        e que o código já dependia (`ChatResponse.session_id`,
        `ChatResponse.created_at`, `RetrievedChunk.description`), o que
        teria quebrado run_rag_pipeline() em runtime (TypeError por kwarg
        inesperado/ausente) se tivesse sido usada como está.

A base estrutural usada aqui é (A) — é a versão mais completa e correta.
Dela vieram: ChatMessage, todos os Field(...) com description=, o default
factory de session_id (UUID4 automático), created_at em ChatResponse e
timestamp em HealthResponse. De (B) veio apenas o campo `domain_id`
(genuinamente novo, adicionado durante a migração multi-domain). Dois
campos novos (`tenant_id`, `collection_name`) foram adicionados nesta
correção para fechar a integração multi-tenant feita em rag_service.py e
chat.py (endpoint) — ver `ResolvedCollectionConfig`/`TenantConfigService`
em app/services/tenant_config_service.py e o bloco de comentário no topo
de app/services/ai/rag_service.py para o fluxo completo.

CORREÇÃO (herdada de A): substituído datetime.utcnow() (deprecated Python
3.12) por datetime.now(timezone.utc) com aware datetime — ver _utcnow().
---------------------------------------------------------------------------
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Retorna o datetime atual em UTC com timezone-aware (Python 3.12 safe)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Payloads de entrada (Request)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """Mensagem individual no histórico de conversa."""
    role: Literal["user", "assistant", "system"]
    content: str = Field(..., min_length=1, max_length=32_000)


class ChatRequest(BaseModel):
    """
    Corpo da requisição POST /v1/chat.
    `session_id` permite manter histórico entre requisições.
    Se omitido, uma sessão efêmera é criada automaticamente.
    """
    message: str = Field(..., min_length=1, max_length=8_000, description="Pergunta do usuário")
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="ID de sessão para manutenção de contexto. Gere um UUID v4 e reutilize.",
    )
    model: str = Field(
        default="ollama/qwen2.5-coder:7b",
        description="Modelo LiteLLM a usar (ex: 'ollama/qwen2.5-coder:7b', 'gpt-4o')",
    )
    top_k: int = Field(default=5, ge=1, le=20, description="Chunks recuperados do Qdrant")
    stream: bool = Field(default=False, description="Retornar resposta como SSE stream")
    project_source_filter: str | None = Field(
        default=None,
        description=(
            "Filtra a busca vetorial pelo campo 'project_source' gravado no "
            "payload do Qdrant durante a ingestão (ex: 'engine' para código "
            "nativo da Unreal Engine, ou o nome de um projeto de amostra como "
            "'Lyra'). None (padrão) não aplica nenhum filtro — busca em todo "
            "o corpus, misturando todas as procedências."
        ),
    )

    # ---------------------------------------------------------------------
    # MULTI-DOMAIN (herdado da reconstrução B — já em uso ativo)
    # ---------------------------------------------------------------------
    domain_id: str | None = Field(
        default=None,
        description=(
            "domain_id explícito (ex: 'godot_4', 'ue5'). Se omitido, o "
            "servidor resolve via header X-Domain-ID ou settings.active_domain "
            "— ver app.core.domains.registry.domain_registry.resolve(). "
            "Quando um tenant é resolvido (ver tenant_id abaixo) E a coleção "
            "daquele tenant tem um domain_id próprio, "
            "ResolvedCollectionConfig.domain_id tem prioridade sobre este "
            "campo — ver run_rag_pipeline() em rag_service.py."
        ),
    )

    # ---------------------------------------------------------------------
    # MULTI-TENANT (novo nesta correção — fecha a integração feita em
    # rag_service.py/chat.py, que já lê estes dois campos via getattr desde
    # a rodada anterior; agora eles existem formalmente no schema)
    # ---------------------------------------------------------------------
    tenant_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "ID do tenant (multi-tenancy) — quando informado, o pipeline RAG "
            "resolve configuração (parâmetros de RAG, system_prompt, coleção "
            "Qdrant) via TenantConfigService.resolve_collection_config em vez "
            "de usar settings global. Ver "
            "app/api/v1/endpoints/chat.py::_resolve_tenant_config e o bloco "
            "de comentário multi-tenant no topo de "
            "app/services/ai/rag_service.py para o fluxo completo. None "
            "(padrão) mantém o comportamento legado — settings global, sem "
            "isolamento de tenant — usado durante o desenvolvimento atual, "
            "focado em Godot 4 single-tenant. Falhas na resolução (tenant "
            "inexistente, Postgres indisponível) NUNCA derrubam a "
            "requisição — o pipeline cai de volta para settings global "
            "automaticamente, com um warning no log do servidor."
        ),
    )
    collection_name: str | None = Field(
        default=None,
        description=(
            "Nome da coleção dentro do tenant (opcional). Só tem efeito se "
            "`tenant_id` também for informado. Quando omitido, "
            "TenantConfigService.resolve_collection_config cai para a "
            "coleção default do tenant (Tenant.default_collection), e se "
            "essa também não existir, tenta casar por domain_id. Permite que "
            "um tenant com múltiplas coleções (ex.: 'godot_4_stable' e "
            "'godot_4_beta') escolha qual usar por requisição."
        ),
    )


# ---------------------------------------------------------------------------
# Payloads de saída (Response)
# ---------------------------------------------------------------------------

class RetrievedChunk(BaseModel):
    """
    Chunk de documentação recuperado do Qdrant.
    Campos alinhados com o payload gravado na ingestão (ver
    ingestion_service.py) e consumidos por rag_service.py ao reconstruir
    RetrievedChunk a partir de hit.payload (ver retrieve_chunks_for_query
    e retrieve_chunks em rag_service.py).
    """
    score: float = Field(..., description="Score de similaridade coseno [0, 1] (busca densa/naive) ou score do Cross-Encoder pós-reranking (Advanced RAG — ver rag_service.py::rerank_with_flashrank)")
    summary_markdown: str = ""
    title: str = "Sem título"
    # CORREÇÃO (restaurado): este campo estava COMENTADO numa versão
    # anterior deste schema. rag_service.py constrói RetrievedChunk(...,
    # description=payload.get("description", ""), ...) em DOIS pontos
    # (retrieve_chunks_for_query e retrieve_chunks/fallback naive) — remover
    # ou comentar este campo quebra as duas construções em runtime
    # (TypeError: unexpected keyword argument 'description'). Mantido ativo.
    description: str = ""
    source_file: str = ""
    chunk_id: int = 0
    # NOTA: nome de campo legado — carrega a versão da engine (UE5 OU
    # Godot 4). rag_service.py resolve qual chave do payload do Qdrant ler
    # (ue_version vs godot_version) via
    # _engine_version_payload_key(domain_id) antes de popular este campo —
    # o NOME do campo Pydantic continua "ue_version" por compatibilidade
    # retroativa com o frontend/clientes já integrados, mesmo para chunks
    # Godot 4. Renomear este campo é uma mudança de contrato de API que
    # exigiria coordenar com o frontend (lib/api.ts) — não faça isso sem
    # avaliar esse impacto primeiro.
    ue_version: str = ""
    doc_type: str = ""
    project_source: str = Field(
        default="",
        description=(
            "Procedência do chunk, gravada no payload do Qdrant durante a "
            "ingestão — distingue código-fonte nativo da Engine de projetos "
            "de amostra (ex: 'Lyra'). Default '' garante compatibilidade "
            "retroativa com pontos antigos do Qdrant que não possuem esse "
            "campo no payload."
        ),
    )
    raw_content: str = Field(
        default="",
        description=(
            "HYBRID RAG — Conteúdo original bruto (código-fonte limpo ou "
            "documentação limpa, dependendo do domínio/canal de ingestão) "
            "armazenado no payload enriquecido do Qdrant. Usado por "
            "rag_service.py como 'Referência Direta' — verdade soberana "
            "para assinaturas, tipos e nomes exatos (ver "
            "_backfill_raw_content em rag_service.py, que preenche este "
            "campo quando ausente em chunks com chunk_id != 0). Default "
            "'' garante compatibilidade retroativa com pontos antigos do "
            "Qdrant, ingeridos antes desta mudança, que não possuem esse "
            "campo no payload."
        ),
    )


class ChatResponse(BaseModel):
    """
    Resposta completa do endpoint de chat.

    CORREÇÃO: `session_id` é OBRIGATÓRIO neste schema — run_rag_pipeline()
    em rag_service.py sempre popula este campo
    (ChatResponse(session_id=request.session_id, ...)), e uma versão deste
    schema que omitisse `session_id` quebraria essa construção em runtime.
    """
    session_id: str
    answer: str
    model_used: str
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list,
        description="Chunks da documentação utilizados para gerar a resposta",
    )
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = Field(
        default=None,
        description=(
            "CORREÇÃO (Bug 1): Razão de término da geração. "
            "'stop' = completou naturalmente, 'length' = truncado por max_tokens, "
            "'content_filter' = filtrado por política de conteúdo. "
            "NULL quando indisponível (ex: modelos locais sem suporte)."
        ),
    )
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class DependencyStatus(BaseModel):
    """Status de uma dependência individual."""
    status: Literal["ok", "degraded", "error"]
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """Resposta do endpoint GET /health."""
    status: Literal["ok", "degraded", "error"]
    version: str
    environment: str
    dependencies: dict[str, DependencyStatus]
    timestamp: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# Modelos disponíveis (LiteLLM) — herdado da reconstrução B. Não é
# consumido por rag_service.py/chat.py nos arquivos revisados até agora,
# mas mantido caso algum endpoint de listagem de modelos (não incluído
# nesta auditoria) dependa dele. Se nenhum endpoint do projeto real usar
# esta classe, é seguro remover — apenas confirme antes via busca por
# "ModelInfo" em todo o backend.
# ---------------------------------------------------------------------------

class ModelInfo(BaseModel):
    """Model information from LiteLLM."""
    id: str
    object: str = "model"
    created: int | None = None
    owned_by: str | None = None