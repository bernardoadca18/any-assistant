"""
app/services/ai/rag_service.py
================================
Advanced RAG Pipeline — Multi-Domain (Godot 4.x default, UE5 plugin)

CORREÇÃO ARQUITETURAL (multi-domain): este módulo era hardcoded para UE5
(prompts, coleção Qdrant fixa, regex de tokenização do canal esparso).
Toda especificidade de domínio foi extraída para app/core/domains/ — ver
BaseDomainProfile, UE5_PROFILE, GODOT4_PROFILE, domain_registry — e é
resolvida em runtime via `domain_id`, propagado explicitamente por toda a
cadeia de chamadas (decompose_query → _run_advanced_retrieval →
retrieve_chunks_for_query/_hybrid_search → _format_context →
run_rag_pipeline/run_rag_pipeline_stream). Nenhuma lógica de retrieval/
reranking/dedupe foi alterada — só o que estava acoplado a UE5.

Pipeline de duas fases para maximizar cobertura e precisão em perguntas
multi-tópico (ex: "como fazer pulo em C++ E integrar com Animation Blueprint"):

    FASE 1 — QUERY DECOMPOSITION (LLM-Driven)
    ─────────────────────────────────────────
    1.1  decompose_query()        → até 3 sub-queries otimizadas para busca vetorial
         (chamada assíncrona leve ao Gemini 3.1 Flash Lite via LiteLLM — ver
         _DECOMPOSITION_MODEL; CORREÇÃO: docstring citava "Gemini 2.0 Flash
         Lite", desatualizada em relação à constante real)

    FASE 2 — EXPANDED RETRIEVAL + RERANKING
    ────────────────────────────────────────
    2.1  retrieve_chunks_for_query()   → busca expandida (limit=15 por query)
    2.2  _deduplicate_chunks()         → deduplicação in-memory por (source_file, chunk_id)
    2.3  rerank_with_flashrank()       → Cross-Encoder CPU (FlashRank), sem GPU, sem API
    2.4  Seleção top-k pós-reranking

    FASE 3 — GERAÇÃO
    ────────────────
    3.1  build_messages_for_llm()      → histórico + contexto RAG no prompt
    3.2  call_litellm()                → resposta via LiteLLM proxy
    3.3  save_turn()                   → persiste turno no Redis

RESILIÊNCIA:
    Cada fase crítica tem try/except com fallback para o RAG ingênuo original,
    garantindo que a API nunca retorne HTTP 500 por falha do pipeline avançado.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from redis.asyncio import Redis
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.core.domains.registry import domain_registry
from app.schemas.chat import ChatRequest, ChatResponse, RetrievedChunk
from app.services.ai.embedding_service import embed_text
from app.services.ai.prompt_factory import PromptFactory
from app.services.ai.session_service import build_messages_for_llm, save_turn
from app.services.ai.sparse_vector_service import SPARSE_VECTOR_NAME, build_sparse_vector
from app.services.tenant_config_service import ResolvedCollectionConfig

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# INTEGRAÇÃO MULTI-TENANT (ResolvedCollectionConfig) — LEIA ANTES DE MEXER
# ---------------------------------------------------------------------------
# CONTEXTO: até esta correção, `tenant_config_service.py` existia como um
# serviço completo (resolução em 3 camadas: override de coleção → default de
# tenant → default de sistema, com cache L1 processo + L2 Redis + L3 Postgres)
# mas NUNCA era chamado por este módulo — toda leitura de configuração vinha
# direto de `settings` (processo inteiro, idêntico para todos os tenants).
# Ou seja: um tenant podia ter `reranker_enabled=False` ou um
# `system_prompt_override` customizado gravado no Postgres via
# CollectionConfig e isso não tinha NENHUM efeito em nenhuma requisição real.
#
# ESTRATÉGIA DE INTEGRAÇÃO ESCOLHIDA (parâmetro opcional, não big-bang):
#   Cada função do pipeline que hoje lê `settings.rag_*` passa a aceitar um
#   parâmetro adicional `resolved_config: ResolvedCollectionConfig | None`.
#     - Se `resolved_config` for fornecido (tenant resolvido com sucesso no
#       endpoint — ver chat.py), os parâmetros de RAG vêm de
#       `resolved_config.rag` (ResolvedRAGConfig), respeitando os overrides
#       de tenant/coleção gravados no Postgres.
#     - Se `resolved_config` for None (nenhum tenant_id na requisição, ou
#       resolução falhou, ou é uma chamada legada/de teste sem essa camada),
#       o comportamento é EXATAMENTE o mesmo de antes desta mudança: leitura
#       direta de `settings.rag_*` — o processo inteiro compartilha a mesma
#       config, como sempre funcionou.
#   Essa escolha foi deliberada: como o app ainda está em desenvolvimento e
#   a prioridade imediata é o domínio Godot 4 funcionando perfeitamente,
#   o parâmetro é OPCIONAL em toda a cadeia — nenhuma chamada existente
#   quebra, e o pipeline nunca fica bloqueado esperando por infraestrutura
#   de tenant (Postgres/Redis) que pode ainda não estar populada em dev.
#
# ONDE A RESOLUÇÃO ACONTECE: não aqui. `resolved_config` é resolvido UMA
# ÚNICA VEZ no endpoint (`app/api/v1/endpoints/chat.py`), via
# `TenantConfigService.resolve_collection_config(...)` ou
# `resolve_default_collection_for_domain(...)`, e propagado para
# `run_rag_pipeline`/`run_rag_pipeline_stream` já resolvido — este módulo
# nunca deve importar `AsyncSession`/tocar o Postgres diretamente, para não
# duplicar a lógica de cache/fallback que já vive em tenant_config_service.py.
#
# PENDÊNCIA CONHECIDA (documentada, não bloqueante): `ChatRequest` ainda não
# tem um campo `tenant_id` estável neste snapshot do schema (não foi
# incluído nos arquivos fornecidos para esta auditoria) — `chat.py` resolve
# isso hoje via `getattr(request, "tenant_id", None)`, no mesmo espírito do
# `getattr(request, "domain_id", None)` já usado neste arquivo. Quando o
# schema for atualizado para tornar `tenant_id` um campo real (ver relatório
# de auditoria — seção "schema ChatRequest"), o `getattr` pode ser trocado
# por acesso direto sem nenhuma outra mudança necessária aqui.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Constantes de configuração do pipeline avançado
# Valores padrão — sobrescritos por settings.rag_* (comportamento legado,
# sem tenant resolvido) OU por resolved_config.rag.* (comportamento novo,
# com tenant resolvido) em runtime quando relevante. Ver bloco acima.
# ---------------------------------------------------------------------------

# Limite expandido de chunks por query (fallback final se nem settings nem
# resolved_config trouxerem um valor — não deveria ocorrer em produção)
_RETRIEVAL_LIMIT_PER_QUERY: int = 15

# Modelo de decomposição padrão (fallback final — ver acima)
_DECOMPOSITION_MODEL: str = "gemini-3.5-flash-lite"

# Fallback: se o modelo de decomposição principal falhar (HTTP 4xx/5xx,
# timeout ou erro de conexão — ver decompose_query).
# CORREÇÃO (ALTO): este valor estava idêntico a _DECOMPOSITION_MODEL, o que
# tornava o fallback um no-op — uma indisponibilidade real do modelo
# principal falhava da mesma forma nas duas tentativas. Precisa ser um
# modelo genuinamente diferente (outro provider/tier no LiteLLM) para
# fornecer redundância de verdade. Ajuste este valor para o modelo de
# fallback real disponível no seu LiteLLM (ex.: outro provider hospedado).
#
# NOTA MULTI-TENANT: este fallback é intencionalmente global (não vem de
# resolved_config) — é uma rede de segurança de infraestrutura, não uma
# preferência de tenant. Um tenant pode escolher seu decomposition_model
# principal via ResolvedRAGConfig.decomposition_model, mas o fallback de
# emergência quando TUDO falha continua sendo o mesmo para todo o sistema.
_DECOMPOSITION_MODEL_FALLBACK: str = "qwen-local"

# Timeout dedicado para a chamada de decomposição (deve ser rápida)
_DECOMPOSITION_TIMEOUT: float = 12.0

# FlashRank: modelo Cross-Encoder embutido (baixa latência, sem GPU)
# Fallback final — ver resolved_config.rag.reranker_model / settings.rag_reranker_model
_FLASHRANK_MODEL: str = "ms-marco-MiniLM-L-12-v2"


def _rag_setting(resolved_config: ResolvedCollectionConfig | None, field_name: str, settings_attr: str, default: Any) -> Any:
    """
    Helper central de resolução de configuração de RAG — PONTO ÚNICO onde a
    prioridade "resolved_config.rag.<field> > settings.<attr> > default" é
    aplicada, para as ~12 leituras de parâmetro de RAG espalhadas por este
    módulo. Extrair isso evitou repetir o mesmo `if resolved_config: ... else
    getattr(settings, ...)` doze vezes com chance de divergir sutilmente
    entre um ponto e outro.

    Prioridade (maior para menor):
        1. resolved_config.rag.<field_name>   — override de tenant/coleção
           (Postgres, via TenantConfigService), SE resolved_config foi
           passado (ou seja, SE um tenant foi resolvido no endpoint).
        2. settings.<settings_attr>            — comportamento legado
           (processo inteiro, sem tenant), usado quando resolved_config é
           None. Este é o caminho que TODA requisição usava antes desta
           integração, e continua sendo o caminho usado por qualquer
           chamador que não resolva tenant (testes, scripts internos,
           requisições sem tenant_id).
        3. default                             — rede de segurança final,
           só acionada se nem settings tiver o atributo (ex.: settings
           desatualizado em um ambiente de dev que ainda não tem a env var).

    NOTA IMPORTANTE: `ResolvedRAGConfig` (ver tenant_config_service.py) é um
    dataclass FROZEN com todos os campos obrigatórios e não-None — ou seja,
    se `resolved_config` não é None, `resolved_config.rag.<field_name>`
    SEMPRE existe e nunca é None (TenantConfigService já aplicou seus
    próprios fallbacks para settings ao construir o objeto). Por isso este
    helper usa `getattr` (não `.get`) no resolved_config — um AttributeError
    aqui indicaria um bug real de nome de campo, não uma ausência esperada.
    """
    if resolved_config is not None:
        return getattr(resolved_config.rag, field_name)
    return getattr(settings, settings_attr, default)


def _resolve_qdrant_collection_name(domain_id: str, resolved_config: ResolvedCollectionConfig | None) -> str:
    """
    Resolve o nome da coleção Qdrant a consultar — PONTO ÚNICO usado por
    TODOS os call sites deste módulo que antes chamavam
    `settings.qdrant_collection_for(domain_id)` diretamente (busca híbrida,
    busca densa pura, fallback naive, backfill de raw_content).

    POR QUE ISSO IMPORTA PARA MULTI-TENANCY (mais do que os outros
    parâmetros de RAG): `settings.qdrant_collection_for(domain_id)` resolve
    para UMA coleção fixa por domínio, IGUAL para todo o processo — ou seja,
    dois tenants diferentes rodando o mesmo domain_id="godot_4" cairiam,
    sem esta correção, na MESMA coleção Qdrant. Isso não é só uma questão
    de configuração ignorada (como os parâmetros resolvidos via
    `_rag_setting`) — é um vazamento real de isolamento de dados entre
    tenants: o tenant B veria (ou poderia contaminar via reranking/RRF) os
    chunks ingeridos pelo tenant A. `ResolvedCollectionConfig.
    qdrant_collection_name` (ver tenant_config_service.py, campo populado a
    partir de `Collection.qdrant_collection_name` no Postgres) é o nome
    real e isolado por tenant/coleção — é o que deve ser usado sempre que
    um tenant foi resolvido.

    Prioridade:
        1. resolved_config.qdrant_collection_name — tenant resolvido, nome
           real da coleção isolada daquele tenant no Postgres.
        2. settings.qdrant_collection_for(domain_id) — comportamento
           legado (sem tenant resolvido), idêntico ao pré-integração. Em
           desenvolvimento single-tenant (prioridade atual: Godot 4
           funcionando), este é o caminho que continua sendo exercitado.

    Args:
        domain_id: Domínio ativo, usado apenas no caminho de fallback
            (settings.qdrant_collection_for exige domain_id).
        resolved_config: Configuração de tenant/coleção já resolvida, ou
            None se nenhum tenant foi resolvido para esta requisição.

    Returns:
        Nome da coleção Qdrant a ser consultada nesta requisição.
    """
    if resolved_config is not None:
        return resolved_config.qdrant_collection_name
    return settings.qdrant_collection_for(domain_id)


# ---------------------------------------------------------------------------
# Estrutura de dados — saída tipada de decompose_query
# ---------------------------------------------------------------------------

@dataclass
class DecomposedQuery:
    """
    Saída tipada da Fase 1 (Query Decomposition).

    Attributes:
        translated_query: Tradução literal em inglês técnico da pergunta
            original do usuário — a âncora usada como substituta da query
            original em todas as buscas (nunca vazia; cai para a pergunta
            original em PT-BR apenas no caso extremo de falha total de
            tradução, ver decompose_query).
        sub_queries: Até 3 sub-queries técnicas adicionais, cada uma
            otimizada para recuperar um conjunto específico de chunks via
            similaridade semântica (pode ser vazia).
        keywords_exact: Até `settings.rag_decomposition_max_keywords`
            identificadores técnicos plausíveis em C++/UE5 (ex:
            "ACharacter", "AddMovementInput", "UPROPERTY"), usados como
            âncora do canal esparso (BM25-like) na busca híbrida — ver
            retrieve_chunks_for_query/_hybrid_search. Pode ser vazia, caso
            em que o retrieval cai automaticamente para busca densa pura.
    """

    translated_query: str
    sub_queries: list[str] = field(default_factory=list)
    keywords_exact: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CORREÇÃO (ALTO — hardcoded para UE5): _SYSTEM_PROMPT e
# _DECOMPOSITION_SYSTEM_PROMPT eram strings fixas neste módulo. Ambas foram
# movidas para app/core/domains/ue5.py (texto preservado 1:1, zero mudança
# de conteúdo) e passam a ser resolvidas em runtime por domain_id via
# PromptFactory — ver app/services/ai/prompt_factory.py e
# app/core/domains/{base,ue5,godot4,registry}.py. Todo ponto do pipeline
# abaixo que antes referenciava _SYSTEM_PROMPT/_DECOMPOSITION_SYSTEM_PROMPT
# diretamente agora recebe/propaga `domain_id` e chama
# `PromptFactory.get_prompts(domain_id)`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Client HTTP compartilhado — decomposição/tradução de queries
# ---------------------------------------------------------------------------
# CORREÇÃO (ALTO — connection pooling ausente): decompose_query costumava
# abrir um httpx.AsyncClient NOVO a cada chamada (dentro de
# _call_decomposition_model), pagando handshake TCP/TLS do zero em toda
# requisição de chat — o mesmo problema que já tinha sido corrigido para
# call_litellm (ver _litellm_client/init_litellm_client acima). Agora
# reutilizamos um client singleton com connection pooling, inicializado no
# lifespan do FastAPI (ver main.py) junto dos demais clients HTTP da app.
_decomposition_client: httpx.AsyncClient | None = None


async def init_decomposition_client() -> httpx.AsyncClient:
    """
    Cria o client HTTP compartilhado para chamadas de decomposição/tradução
    de query ao LiteLLM. Deve ser chamado no lifespan startup do FastAPI.

    MULTI-TENANT — EXCEÇÃO INTENCIONAL: `decomp_timeout` continua lendo
    apenas de `settings.rag_decomposition_timeout` (não de `_rag_setting`/
    `resolved_config`), mesmo `ResolvedRAGConfig.decomposition_timeout`
    existindo como campo configurável por tenant. Isso é proposital, não
    um ponto esquecido: esta função roda UMA VEZ no lifespan startup do
    FastAPI, antes de qualquer requisição existir — não há tenant, não há
    `resolved_config` possível neste momento, só um client HTTP compartilhado
    sendo construído para o processo inteiro. Um timeout por tenant
    exigiria um client HTTP por tenant (ou por config de timeout distinta),
    o que é uma mudança de arquitetura bem maior do que esta integração se
    propôs a fazer — fica registrado aqui como possível trabalho futuro se
    algum tenant precisar de um timeout de decomposição sensivelmente
    diferente do padrão do sistema.
    """
    global _decomposition_client
    decomp_timeout = getattr(settings, "rag_decomposition_timeout", _DECOMPOSITION_TIMEOUT)
    _decomposition_client = httpx.AsyncClient(
        base_url=settings.litellm_base_url,
        headers=settings.litellm_headers,
        timeout=httpx.Timeout(connect=5.0, read=decomp_timeout, write=5.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    log.info("Client HTTP do LiteLLM (decomposição de queries) inicializado.")
    return _decomposition_client


async def close_decomposition_client() -> None:
    """Fecha o client HTTP compartilhado de decomposição. Chamado no shutdown."""
    global _decomposition_client
    if _decomposition_client:
        await _decomposition_client.aclose()
        _decomposition_client = None
        log.info("Client HTTP do LiteLLM (decomposição de queries) encerrado.")


def _get_decomposition_client() -> httpx.AsyncClient:
    if _decomposition_client is None:
        raise RuntimeError(
            "Client HTTP de decomposição não inicializado. Verifique o lifespan."
        )
    return _decomposition_client


# ---------------------------------------------------------------------------
# FASE 1 — Query Decomposition
# ---------------------------------------------------------------------------

async def decompose_query(
    user_question: str,
    domain_id: str,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> DecomposedQuery:
    """
    Traduz a query do usuário para inglês técnico e a decompõe em sub-queries
    técnicas para busca vetorial.

    CORREÇÃO (ALTO — prompt de decomposição hardcoded para UE5): o parâmetro
    `domain_id` foi adicionado para resolver `PromptFactory.get_prompts(domain_id)
    .decomposition_prompt` — antes, o prompt de decomposição era sempre o de
    UE5, então qualquer requisição com domain_id="godot_4" extrairia
    keywords_exact plausíveis em C++/UE5 (ex: "ACharacter", "UPROPERTY") em
    vez de identificadores Godot (ex: "@export", "signal"), quebrando
    silenciosamente a qualidade do canal esparso da busca híbrida para
    qualquer domínio que não fosse UE5.

    CORREÇÃO CRÍTICA (mismatch cross-lingual): a base vetorial é 100% inglês
    (ver ingestion_service.py). Anteriormente, a query original em PT-BR era
    SEMPRE incluída na lista de busca (ver histórico), garantindo que toda
    requisição tivesse pelo menos uma busca vetorial em desvantagem
    cross-lingual — a causa raiz de scores baixos e chunks existentes não
    sendo recuperados. Agora a primeira posição da lista retornada é sempre
    a TRADUÇÃO LITERAL em inglês (nunca o texto original em português),
    e ela é o que deve ser usado para gerar o embedding "principal" no
    chamador (ver _run_advanced_retrieval).

    Faz uma chamada assíncrona leve ao LLM de decomposição e parseia
    o JSON de saída. Em caso de falha do modelo principal E do fallback,
    a query original em português É usada como último recurso (com aviso
    alto no log) para nunca deixar a requisição sem resposta — mas isso
    deve ser tratado como incidente operacional, não como caminho normal.

    INTEGRAÇÃO MULTI-TENANT (ver bloco de comentário no topo do módulo):
    `resolved_config`, quando fornecido, sobrescreve DOIS parâmetros
    independentes desta função — vale registrar a distinção, pois eles vêm
    de fontes diferentes dentro do mesmo objeto:

        1. `decomposition_prompt`: normalmente resolvido via
           `PromptFactory.get_prompts(domain_id).decomposition_prompt`, que
           lê SEMPRE do BaseDomainProfile (godot4.py/ue5.py) — não tem
           conceito de override por tenant. `ResolvedCollectionConfig.
           decomposition_prompt` (ver tenant_config_service.py,
           `_build_resolved_config`) hoje é documentado como "always from
           domain profile" também — ou seja, NESTA VERSÃO os dois caminhos
           produzem o mesmo valor. A troca para `resolved_config.
           decomposition_prompt` já deixa o código pronto para o dia em que
           `tenant_config_service.py` ganhar um `decomposition_prompt_override`
           por coleção (hoje só `system_prompt_override` existe) — nesse
           momento, este ponto já vai captar o override automaticamente,
           sem precisar de nenhuma mudança adicional aqui.
        2. `decomp_model`: vem de `resolved_config.rag.decomposition_model`
           (ResolvedRAGConfig) — ESTE sim já é configurável por
           tenant/coleção hoje (ex.: um tenant pode preferir um modelo de
           decomposição mais barato ou mais caro que o default do sistema).

    Quando `resolved_config` é None (nenhum tenant resolvido — ver
    `chat.py`), o comportamento é idêntico ao pré-integração: `domain_id`
    resolve o prompt via PromptFactory, e `settings.rag_decomposition_model`
    resolve o modelo.

    Args:
        user_question: Pergunta original do usuário.
        domain_id: Domínio ativo (usado para resolver o prompt via
            PromptFactory quando resolved_config não está disponível).
        resolved_config: Configuração de tenant/coleção já resolvida pelo
            endpoint (ver chat.py), ou None se nenhum tenant foi resolvido
            para esta requisição — nesse caso cai para settings/domain_id.

    Returns:
        DecomposedQuery com translated_query (nunca vazia — só contém texto
        em português no caso extremo de falha total de tradução, ver NOTA
        acima), sub_queries (sem duplicatas da tradução) e keywords_exact
        (identificadores técnicos exatos para o canal esparso da busca
        híbrida — pode ser vazia).
    """
    # Lê configurações em runtime para permitir override sem recompilação.
    # Prioridade: resolved_config (tenant/coleção) > settings (global) >
    # constante local — ver _rag_setting() e o bloco de comentário no topo
    # do módulo para a justificativa completa desta cadeia de fallback.
    decomp_model = _rag_setting(resolved_config, "decomposition_model", "rag_decomposition_model", _DECOMPOSITION_MODEL)

    # decomposition_prompt: resolved_config.decomposition_prompt já teria o
    # mesmo valor de PromptFactory.get_prompts(domain_id).decomposition_prompt
    # nesta versão do tenant_config_service (ver docstring acima) — usamos
    # resolved_config quando disponível para já deixar este ponto correto
    # no dia em que um override por coleção for adicionado ao serviço.
    decomposition_prompt = (
        resolved_config.decomposition_prompt
        if resolved_config is not None
        else PromptFactory.get_prompts(domain_id).decomposition_prompt
    )

    payload = {
        "model": decomp_model,
        "messages": [
            {"role": "system", "content": decomposition_prompt},
            {"role": "user", "content": user_question},
        ],
        "temperature": 0.0,   # determinístico — queremos output estruturado
        "max_tokens": 256,    # sub-queries são curtas; 256 é mais que suficiente
        "response_format": {"type": "json_object"},  # força JSON no LiteLLM
    }

    # CORREÇÃO (ALTO — fallback pulado em falhas não-HTTP): antes, só
    # httpx.HTTPStatusError acionava a tentativa com o modelo de fallback;
    # qualquer outra falha (timeout, ConnectError, JSON malformado na
    # resposta) ia direto para o caminho degradado em PT-BR, pulando o
    # fallback por completo — o oposto do que faz sentido, já que timeout
    # e erro de conexão são justamente os casos mais comuns de falha
    # transitória. Agora QUALQUER falha do modelo principal tenta o
    # fallback antes de degradar.
    #
    # CORREÇÃO (connection pooling): usa o client HTTP singleton
    # (_decomposition_client, ver init_decomposition_client) em vez de
    # abrir um httpx.AsyncClient novo a cada chamada.
    async def _call_decomposition_model(model_name: str) -> str:
        request_payload = {**payload, "model": model_name}
        client = _get_decomposition_client()
        resp = await client.post("/chat/completions", json=request_payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    try:
        raw_content: str = await _call_decomposition_model(decomp_model)
    except Exception as primary_exc:
        log.warning(
            "Decomposição: modelo principal '%s' falhou (%s). Tentando "
            "fallback '%s'.",
            decomp_model,
            primary_exc,
            _DECOMPOSITION_MODEL_FALLBACK,
        )
        try:
            raw_content = await _call_decomposition_model(_DECOMPOSITION_MODEL_FALLBACK)
        except Exception as fallback_exc:
            log.error(
                "TRADUÇÃO INDISPONÍVEL: decomposição falhou no modelo principal "
                "('%s': %s) e no fallback ('%s': %s). Usando a query original em "
                "PT-BR para embedding — isso é um caminho DEGRADADO que sofre "
                "de mismatch cross-lingual contra o corpus (100%% inglês) e "
                "deve ser tratado como incidente, não como comportamento normal.",
                decomp_model,
                primary_exc,
                _DECOMPOSITION_MODEL_FALLBACK,
                fallback_exc,
            )
            return DecomposedQuery(translated_query=user_question, sub_queries=[], keywords_exact=[])

    # --- Parse robusto da resposta JSON ---
    translated_query, sub_queries, keywords_exact = _parse_decomposition_response(
        raw_content, user_question
    )

    # A tradução literal em inglês é a âncora da busca — NUNCA a query
    # original em português (ver docstring desta função para o porquê).
    deduped_sub_queries: list[str] = []
    for sq in sub_queries:
        sq_stripped = sq.strip()
        # Evita duplicar a tradução se o modelo a devolveu idêntica
        if sq_stripped and sq_stripped.lower() != translated_query.lower():
            deduped_sub_queries.append(sq_stripped)

    # MULTI-TENANT: decomposition_max_keywords também é configurável por
    # tenant/coleção (ex.: um tenant Godot pode querer mais keywords_exact
    # que um tenant UE5, dada a densidade de identificadores prefixados
    # com @ como @export/@onready) — ver _rag_setting().
    max_keywords = _rag_setting(resolved_config, "decomposition_max_keywords", "rag_decomposition_max_keywords", 6)
    clean_keywords = [kw.strip() for kw in keywords_exact if isinstance(kw, str) and kw.strip()]
    clean_keywords = clean_keywords[:max_keywords]

    log.info(
        "Query traduzida e decomposta | tradução + %d sub-queries + %d keywords_exact",
        len(deduped_sub_queries),
        len(clean_keywords),
    )
    log.debug("  Tradução: %s", translated_query)
    for i, q in enumerate(deduped_sub_queries):
        log.debug("  Sub-query[%d]: %s", i, q)
    if clean_keywords:
        log.debug("  Keywords exatas: %s", clean_keywords)

    return DecomposedQuery(
        translated_query=translated_query,
        sub_queries=deduped_sub_queries,
        keywords_exact=clean_keywords,
    )


def _parse_decomposition_response(
    raw: str, fallback_query: str
) -> tuple[str, list[str], list[str]]:
    """
    Parser ultra-robusto para a saída do modelo de decomposição/tradução.

    Estratégias (em ordem de preferência), aplicadas ao dict parseado:
    1. JSON direto (caminho feliz — json_object mode do LiteLLM)
    2. Extração de bloco JSON via regex (se o modelo envolveu em markdown)
    3. Extração de JSON embutido sem markdown
    4. Extração de lista simples via regex de aspas (sem 'translated_query'
       confiável — usa a primeira string extraída como tradução aproximada;
       sem keywords_exact confiáveis nesta estratégia — retorna vazio)
    5. Fallback total: 'fallback_query' (PT-BR) como tradução — só ocorre se
       TODAS as estratégias de parse falharem, e é logado como erro alto,
       nunca como comportamento silencioso esperado.

    Args:
        raw: String bruta retornada pelo LLM.
        fallback_query: Query original do usuário, usada em ÚLTIMO CASO se
            nenhuma tradução puder ser extraída da resposta do modelo.

    Returns:
        Tupla (translated_query, sub_queries, keywords_exact).
        translated_query nunca é vazia — cai para fallback_query apenas se
        o parse falhar por completo. keywords_exact pode ser vazia em
        qualquer estratégia — ausência de identificadores exatos é um
        resultado válido, não uma falha de parse.
    """
    raw = raw.strip()
    if not raw:
        log.warning(
            "Decomposição: resposta vazia do LLM. Usando query original como "
            "tradução — sem tradução real disponível para esta requisição."
        )
        return fallback_query, [], []

    def _extract(parsed: dict[str, Any]) -> tuple[str, list[str], list[str]] | None:
        translated = parsed.get("translated_query")
        queries = parsed.get("sub_queries", [])
        if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
            queries = []
        clean_queries = [q for q in queries if isinstance(q, str) and q.strip()]

        keywords = parsed.get("keywords_exact", [])
        if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
            keywords = []
        clean_keywords = [k for k in keywords if isinstance(k, str) and k.strip()]

        if isinstance(translated, str) and translated.strip():
            return translated.strip(), clean_queries, clean_keywords

        # Campo 'translated_query' ausente/inválido: modelo pode ter usado
        # formato antigo (só sub_queries). Reaproveita a primeira sub-query
        # como âncora de tradução aproximada em vez de cair para PT-BR.
        if clean_queries:
            log.warning(
                "Decomposição: 'translated_query' ausente na resposta do "
                "modelo. Reaproveitando primeira sub-query como tradução "
                "aproximada."
            )
            return clean_queries[0], clean_queries[1:], clean_keywords

        return None

    # --- Estratégia 1: JSON direto ---
    try:
        parsed: dict[str, Any] = json.loads(raw)
        result = _extract(parsed)
        if result:
            return result
    except json.JSONDecodeError:
        pass

    # --- Estratégia 2: Bloco JSON em markdown (```json ... ```) ---
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            result = _extract(parsed)
            if result:
                return result
        except json.JSONDecodeError:
            pass

    # --- Estratégia 3: JSON embutido sem markdown ---
    json_match2 = re.search(r"\{.*?\}", raw, re.DOTALL)
    if json_match2:
        try:
            parsed = json.loads(json_match2.group(0))
            result = _extract(parsed)
            if result:
                return result
        except json.JSONDecodeError:
            pass

    # --- Estratégia 4: Lista de strings entre aspas (sem chave confiável) ---
    quoted_queries = [q for q in re.findall(r'"([^"]{10,200})"', raw) if q.strip()]
    if quoted_queries:
        log.warning(
            "Decomposição: JSON inválido, extraindo via regex de aspas. "
            "Primeira string extraída assumida como tradução aproximada. raw=%r",
            raw[:120],
        )
        return quoted_queries[0], quoted_queries[1:], []

    log.error(
        "Decomposição: impossível parsear resposta do LLM em nenhum formato. "
        "Usando query original em PT-BR como último recurso — mismatch "
        "cross-lingual esperado para esta requisição. raw=%r",
        raw[:200],
    )
    return fallback_query, [], []


# ---------------------------------------------------------------------------
# CORREÇÃO (ALTO — filtro de project_source ignorado): a ingestão grava
# `project_source` no payload de cada ponto do Qdrant para distinguir
# código-fonte nativo da Engine de projetos de amostra (ex: Lyra), mas até
# aqui esse campo nunca era usado na busca — toda query misturava as duas
# procedências livremente no mesmo pool de reranking. Este helper constrói
# o Filter do Qdrant que restringe a busca a uma única procedência quando
# o chamador (via ChatRequest.project_source_filter) pedir isso.
# ---------------------------------------------------------------------------

def _build_project_source_filter(
    project_source: str | None,
) -> qdrant_models.Filter | None:
    """
    Constrói o Filter do Qdrant para restringir a busca por `project_source`.

    Args:
        project_source: Valor exato do campo `project_source` a filtrar
            (ex: 'engine', 'Lyra'). None ou string vazia não aplica nenhum
            filtro — a busca segue irrestrita em todo o corpus.

    Returns:
        qdrant_models.Filter pronto para uso em `query_filter=`, ou None
        se nenhum filtro deve ser aplicado.
    """
    if not project_source or not project_source.strip():
        return None

    return qdrant_models.Filter(
        must=[
            qdrant_models.FieldCondition(
                key="project_source",
                match=qdrant_models.MatchValue(value=project_source.strip()),
            )
        ]
    )


def _engine_version_payload_key(domain_id: str) -> str:
    """
    Resolve qual chave do payload do Qdrant carrega a versão da engine para
    o domínio ativo.

    CORREÇÃO (CRÍTICO — leitura hardcoded de 'ue_version'): os dois pontos
    que constroem RetrievedChunk a partir do payload (ver
    `retrieve_chunks_for_query` e `retrieve_chunks`) liam sempre
    `payload.get("ue_version", "")`, independentemente do domínio. Chunks
    Godot corretamente gravados com a chave "godot_version" (ver
    ingestion_service._build_chunks_with_metadata) voltavam SEMPRE com
    versão vazia — mesmo depois de corrigida a ingestão, o retrieval
    continuaria não expondo essa informação. Espelha exatamente a mesma
    regra usada na escrita, para as duas pontas do pipeline nunca
    divergirem de novo: se um domínio novo for registrado no futuro com uma
    convenção de campo de versão diferente, ambos os lados precisam ser
    atualizados juntos.
    """
    return "godot_version" if domain_id == "godot_4" else "ue_version"


# ---------------------------------------------------------------------------
# FASE 2.1 — Busca Expandida no Qdrant
# ---------------------------------------------------------------------------

async def _hybrid_search(
    qdrant: AsyncQdrantClient,
    query_vector: list[float],
    keywords_text: str,
    limit: int,
    query_filter: qdrant_models.Filter | None,
    domain_id: str,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[qdrant_models.ScoredPoint]:
    """
    Busca híbrida (Dense + Sparse/BM25-like) fundida via Reciprocal Rank
    Fusion (RRF), tudo resolvido na própria camada do Qdrant via
    `query_points` com dois `Prefetch`:

    1. Denso — usa o vetor denso não-nomeado (chave "", mesmo esquema já
       usado antes da migração híbrida).
    2. Esparso — usa `sparse_vector_service.build_sparse_vector(keywords_text)`
       no espaço nomeado `keywords_sparse` (ver
       ingestion_service.ensure_sparse_vectors_enabled).

    Propaga o mesmo `query_filter` (procedência via project_source) para os
    dois Prefetches, para que a restrição valha igualmente nos dois canais
    antes da fusão.

    Não trata exceções aqui — deixa o chamador (retrieve_chunks_for_query)
    decidir o fallback (busca densa pura) quando esta função falhar, por
    exemplo numa coleção legada ainda não migrada para o espaço esparso.

    CORREÇÃO (CRÍTICO — coleção e tokenizer hardcoded): `domain_id` foi
    adicionado para resolver (a) `settings.qdrant_collection_for(domain_id)`
    em vez do `settings.qdrant_collection` fixo (sem isso, Godot 4 e UE5
    colidiriam na MESMA coleção, com dimensões de embedding e schemas de
    payload incompatíveis) e (b) a regex de tokenização do canal esparso
    (`BaseDomainProfile.sparse_camel_split_regex`) — sem isso, keywords
    Godot (@export, snake_case) seriam tokenizadas com a heurística de
    prefixo UE5 (A/U/F/E), degradando o recall do canal esparso.

    INTEGRAÇÃO MULTI-TENANT (ver _resolve_qdrant_collection_name): quando
    `resolved_config` é fornecido, a coleção usada é
    `resolved_config.qdrant_collection_name` (isolada por tenant/coleção no
    Postgres) em vez de `settings.qdrant_collection_for(domain_id)` (fixa
    por domínio, compartilhada por todo o processo). Sem esta troca, dois
    tenants no mesmo domain_id="godot_4" fariam RRF na MESMA coleção Qdrant
    — um vazamento de isolamento de dados entre tenants, não apenas uma
    config ignorada. domain_id continua sendo necessário aqui de qualquer
    forma, pois resolve a regex de tokenização do canal esparso
    (sparse_camel_split_regex), que é uma propriedade do DOMÍNIO
    (convenção de nomenclatura da linguagem), não do tenant.
    """
    profile = domain_registry.get(domain_id)
    sparse_vector = build_sparse_vector(keywords_text, camel_split_regex=profile.sparse_camel_split_regex)

    prefetch = [
        qdrant_models.Prefetch(
            query=query_vector,
            using="",
            limit=limit,
            filter=query_filter,
        ),
        qdrant_models.Prefetch(
            query=sparse_vector,
            using=SPARSE_VECTOR_NAME,
            limit=limit,
            filter=query_filter,
        ),
    ]

    response = await qdrant.query_points(
        collection_name=_resolve_qdrant_collection_name(domain_id, resolved_config),
        prefetch=prefetch,
        query=qdrant_models.FusionQuery(fusion=qdrant_models.Fusion.RRF),
        limit=limit,
        with_payload=True,
        query_filter=query_filter,
    )
    return response.points


async def retrieve_chunks_for_query(
    qdrant: AsyncQdrantClient,
    query_vector: list[float],
    limit: int,
    domain_id: str,
    project_source_filter: str | None = None,
    keywords_exact: list[str] | None = None,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Executa uma única busca no Qdrant com limite expandido.

    CORREÇÃO (CRÍTICO — coleção hardcoded): `domain_id` foi adicionado e é
    obrigatório (sem default) de propósito — este é o ponto que resolve
    a coleção Qdrant tanto no caminho híbrido (`_hybrid_search`) quanto no
    fallback denso puro abaixo (ver `_resolve_qdrant_collection_name`).
    Tornar opcional aqui reintroduziria silenciosamente o acoplamento
    single-tenant que motivou toda a refatoração multi-domain.

    BUSCA HÍBRIDA (Dense + Sparse via RRF): quando `keywords_exact` traz ao
    menos um identificador utilizável E o kill-switch de busca híbrida
    (`resolved_config.rag.hybrid_search_enabled` se um tenant foi resolvido,
    senão `settings.rag_hybrid_search_enabled` — ver `_rag_setting`) está
    ligado, a busca combina o vetor denso com um vetor esparso BM25-like
    construído a partir dos identificadores exatos (ver `_hybrid_search`),
    fundindo os dois rankings via Reciprocal Rank Fusion direto no Qdrant.
    Sem keywords utilizáveis (ou com o kill-switch desligado, ou se a busca
    híbrida falhar — ex: coleção legada ainda sem o espaço esparso
    migrado), cai transparentemente para a busca densa pura de sempre — o
    comportamento é idêntico ao pré-hybrid para quem não passa
    `keywords_exact`.

    Não aplica score_threshold aqui — o reranker vai cuidar da qualidade.
    Threshold é aplicado apenas se o reranker não estiver disponível.

    CORREÇÃO (project_source ignorado): quando `project_source_filter` é
    informado, a busca é restrita via `query_filter` a pontos cujo payload
    tenha exatamente esse valor no campo `project_source` — permite separar
    código-fonte nativo da Engine de projetos de amostra (ex: Lyra) direto
    na busca vetorial, em vez de filtrar depois em memória (mais caro e
    reduz o recall efetivo, já que o `limit` do Qdrant conta candidatos
    antes do filtro).

    INTEGRAÇÃO MULTI-TENANT: `resolved_config`, quando fornecido, afeta
    esta função em DOIS pontos independentes — vale a distinção:
        1. Qual COLEÇÃO é consultada (isolamento de dados entre tenants —
           ver `_resolve_qdrant_collection_name`, propagado tanto para
           `_hybrid_search` quanto para os dois `qdrant.search` diretos
           abaixo).
        2. Se o CANAL ESPARSO está ligado (preferência de tenant, não
           isolamento de dados — ver `_rag_setting`).
    Quando `resolved_config` é None, ambos os comportamentos são idênticos
    ao pré-integração (coleção fixa por domínio via settings, kill-switch
    global via settings).

    Args:
        qdrant: Cliente Qdrant assíncrono.
        query_vector: Vetor de embedding da query.
        limit: Número máximo de resultados a recuperar.
        project_source_filter: Se informado, restringe a busca a pontos com
            esse valor exato no campo `project_source` do payload. None
            (padrão) busca em todo o corpus, sem restrição de procedência.
        keywords_exact: Identificadores técnicos exatos extraídos pelo
            decompositor (ver DecomposedQuery.keywords_exact). Quando
            presentes, ativam o canal esparso da busca híbrida.
        resolved_config: Configuração de tenant/coleção já resolvida pelo
            endpoint, ou None se nenhum tenant foi resolvido (comportamento
            legado — settings global, sem isolamento por tenant).

    Returns:
        Lista de RetrievedChunk, possivelmente vazia se Qdrant falhar.
    """
    query_filter = _build_project_source_filter(project_source_filter)

    hybrid_enabled = _rag_setting(resolved_config, "hybrid_search_enabled", "rag_hybrid_search_enabled", True)
    usable_keywords = [kw.strip() for kw in (keywords_exact or []) if kw and kw.strip()]
    collection_name = _resolve_qdrant_collection_name(domain_id, resolved_config)

    try:
        if hybrid_enabled and usable_keywords:
            try:
                results = await _hybrid_search(
                    qdrant=qdrant,
                    query_vector=query_vector,
                    keywords_text=" ".join(usable_keywords),
                    limit=limit,
                    query_filter=query_filter,
                    domain_id=domain_id,
                    resolved_config=resolved_config,
                )
                log.debug(
                    "Busca híbrida (RRF) OK | domain=%s | %d keywords_exact | %d resultados",
                    domain_id,
                    len(usable_keywords),
                    len(results),
                )
            except Exception as hybrid_exc:
                log.warning(
                    "Busca híbrida (RRF) falhou (%s). Fallback transparente "
                    "para busca densa pura nesta query.",
                    hybrid_exc,
                )
                results = await qdrant.search(
                    collection_name=collection_name,
                    query_vector=query_vector,
                    limit=limit,
                    with_payload=True,
                    query_filter=query_filter,
                )
        else:
            results = await qdrant.search(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
                with_payload=True,
                query_filter=query_filter,
                # Sem score_threshold aqui — deixamos o reranker filtrar a qualidade
            )
        log.debug("Recebidos %d resultados do Qdrant.", len(results))
        if results:
            log.debug("Payload de exemplo (1º resultado): %s", results[0].payload)
    except Exception as exc:
        log.error("Falha na busca vetorial (Qdrant): %s", exc)
        return []

    version_field = _engine_version_payload_key(domain_id)

    chunks: list[RetrievedChunk] = []
    for hit in results:
        payload = hit.payload or {}
        try:
            chunks.append(
                RetrievedChunk(
                    score=hit.score,
                    summary_markdown=payload.get("summary_markdown", ""),
                    title=payload.get("title", "Sem título"),
                    description=payload.get("description", ""),
                    source_file=payload.get("source_file", ""),
                    chunk_id=payload.get("chunk_id", 0),
                    # CORREÇÃO: lia sempre "ue_version" — chunks Godot
                    # (gravados com "godot_version") sempre voltavam vazios.
                    # Ver _engine_version_payload_key.
                    ue_version=payload.get(version_field, ""),
                    doc_type=payload.get("doc_type", ""),
                    # CORREÇÃO (Correção 1): project_source agora é extraído
                    # do payload — antes era descartado silenciosamente, e o
                    # RetrievedChunk resultante não tinha como sinalizar a
                    # procedência (Engine nativa vs projeto de amostra) para
                    # o restante do pipeline (_format_context, filtros, etc).
                    project_source=payload.get("project_source", ""),
                    # HYBRID RAG: payload enriquecido — "" para pontos antigos
                    # ingeridos antes desta mudança (compatibilidade retroativa).
                    raw_content=payload.get("raw_content", ""),
                )
            )
        except Exception as exc:
            log.warning("Chunk ignorado por payload inválido: %s | %s", payload, exc)

    return chunks


# ---------------------------------------------------------------------------
# FASE 2.2 — Deduplicação Atômica
# ---------------------------------------------------------------------------

def _deduplicate_chunks(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """
    Remove chunks duplicados usando a chave composta (source_file, chunk_id).

    Em caso de duplicata (mesmo chunk recuperado por queries diferentes),
    mantém a instância com o MAIOR score de similaridade original do Qdrant.

    Preserva a ordem do primeiro aparecimento para chunks únicos.

    Args:
        chunks: Lista não-deduplicada (pode ter duplicatas entre sub-queries).

    Returns:
        Lista deduplicada, ordenada pelo primeiro aparecimento.
    """
    seen: dict[tuple[str, int], RetrievedChunk] = {}
    order: list[tuple[str, int]] = []

    for chunk in chunks:
        key = (chunk.source_file, chunk.chunk_id)
        if key not in seen:
            seen[key] = chunk
            order.append(key)
        else:
            # Mantém o chunk com maior score de similaridade coseno
            if chunk.score > seen[key].score:
                seen[key] = chunk

    deduplicated = [seen[k] for k in order]
    log.debug(
        "Deduplicação: %d → %d chunks únicos", len(chunks), len(deduplicated)
    )
    return deduplicated


# ---------------------------------------------------------------------------
# FASE 2.3 — Reranking com FlashRank (Cross-Encoder CPU)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cache do Ranker (FlashRank) — POR MODELO, não mais um singleton único
# ---------------------------------------------------------------------------
# CORREÇÃO (ALTO — bloqueio de event loop): rerank_with_flashrank criava um
# `Ranker(...)` novo A CADA REQUISIÇÃO, dentro de uma função síncrona chamada
# diretamente do event loop assíncrono (sem asyncio.to_thread). O comentário
# original ("FlashRank faz cache interno — chamadas subsequentes são
# instantâneas") se refere ao cache do ARQUIVO de modelo em disco, não à
# instância Python/sessão ONNX — recriar o objeto tem overhead real, e mesmo
# que fosse grátis, a inferência do Cross-Encoder é CPU-bound e síncrona: para
# N usuários concorrentes, cada rerank bloqueava o loop inteiro pelo tempo de
# execução, serializando todas as outras requisições HTTP do FastAPI nesse
# intervalo. O Ranker é construído uma única vez POR MODELO (fora do event
# loop, via to_thread) e reaproveitado; a inferência também roda em thread
# separada.
#
# MULTI-TENANT (ALTO — motivo da mudança de singleton único para cache por
# modelo): `ResolvedRAGConfig.reranker_model` é configurável por
# tenant/coleção — um tenant pode escolher um cross-encoder diferente do
# default do sistema. Com um único `_ranker_instance` global (implementação
# original), o PRIMEIRO tenant a disparar uma requisição no processo
# "vencia": seu `reranker_model` inicializava a instância global, e todo
# tenant seguinte (mesmo pedindo um modelo diferente via
# ResolvedRAGConfig.reranker_model) silenciosamente reutilizava o modelo do
# primeiro tenant — sem erro, sem log, só uma reordenação de chunks feita
# pelo cross-encoder "errado" para aquele tenant. Trocado para um dict
# `{model_name: Ranker}` — cada modelo distinto usado por QUALQUER tenant é
# carregado e cacheado uma única vez (mesmo custo de memória por modelo de
# antes), e cada requisição usa o Ranker correto para o `reranker_model`
# que foi resolvido para ela.
#
# LIMITAÇÃO CONHECIDA (não resolvida aqui, documentando para o futuro):
# `cache_dir` (diretório de cache em disco dos pesos ONNX) continua sendo
# lido de settings/resolved_config.rag.reranker_cache_dir POR CHAMADA, mas
# a CHAVE do dict de cache é só `model_name` — se dois tenants configurarem
# o MESMO `reranker_model` mas `reranker_cache_dir` DIFERENTES (cenário
# improvável, mas possível dado que o campo existe por coleção), o segundo
# tenant reaproveita silenciosamente a instância já carregada com o
# cache_dir do primeiro. Na prática isso quase nunca importa (cache_dir é
# tipicamente uma preocupação de infraestrutura/deploy, não de tenant), mas
# fica registrado: se isso vier a importar, a chave do cache precisaria ser
# `(model_name, cache_dir)` em vez de só `model_name`.
_ranker_instances: dict[str, Any] = {}
_ranker_init_lock = asyncio.Lock()


async def _get_ranker(resolved_config: ResolvedCollectionConfig | None = None) -> Any:
    """
    Retorna o Ranker cacheado para o `reranker_model` resolvido (tenant ou
    settings global — ver `_rag_setting`), construindo-o em thread na 1ª vez
    que aquele modelo específico é solicitado por qualquer tenant.

    Args:
        resolved_config: Configuração de tenant/coleção já resolvida, ou
            None se nenhum tenant foi resolvido — nesse caso o modelo vem
            de `settings.rag_reranker_model` (comportamento legado).
    """
    reranker_model = _rag_setting(resolved_config, "reranker_model", "rag_reranker_model", _FLASHRANK_MODEL)

    # Fast path: modelo já carregado por uma chamada anterior (de qualquer
    # tenant) — não precisa do lock para o caso comum.
    cached = _ranker_instances.get(reranker_model)
    if cached is not None:
        return cached

    async with _ranker_init_lock:
        # Re-checa dentro do lock: outra corrotina pode ter inicializado
        # ESTE MESMO modelo enquanto esperávamos o lock.
        if reranker_model in _ranker_instances:
            return _ranker_instances[reranker_model]

        try:
            from flashrank import Ranker  # importação lazy — graceful degradation
        except ImportError:
            log.warning(
                "FlashRank não está instalado. Instale com: pip install flashrank. "
                "Usando ordem original do Qdrant como fallback."
            )
            return None

        cache_dir = _rag_setting(resolved_config, "reranker_cache_dir", "rag_reranker_cache_dir", "/tmp/flashrank_cache")
        try:
            ranker_instance = await asyncio.to_thread(
                Ranker, model_name=reranker_model, cache_dir=cache_dir
            )
            _ranker_instances[reranker_model] = ranker_instance
            log.info(
                "FlashRank Ranker inicializado e cacheado | model=%s | "
                "total de modelos cacheados no processo=%d",
                reranker_model,
                len(_ranker_instances),
            )
        except Exception as exc:
            log.warning(
                "Falha ao inicializar o FlashRank Ranker (model=%s): %s. "
                "Reranking ficará desabilitado (fallback: ordem original) "
                "para requisições que usem este modelo.",
                reranker_model,
                exc,
            )
            return None

    return _ranker_instances[reranker_model]


async def rerank_with_flashrank(
    chunks: list[RetrievedChunk],
    query_text_en: str,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Reordena os chunks usando um Cross-Encoder leve via FlashRank.

    FlashRank usa modelos ONNX otimizados que rodam em CPU em milissegundos,
    sem necessidade de GPU, sem chamada de API externa. Compara o texto de
    cada chunk diretamente contra `query_text_en` para maximizar relevância
    final — a comparação usa a query como um todo (não as sub-queries
    decompostas individualmente), para julgar cada chunk contra a intenção
    completa da pergunta.

    CORREÇÃO CRÍTICA (mismatch cross-lingual): `query_text_en` DEVE ser texto
    em INGLÊS (a `translated_query` retornada por decompose_query() — nunca
    a pergunta original em PT-BR). O modelo Cross-Encoder do FlashRank
    (ms-marco-MiniLM, ver _FLASHRANK_MODEL) é treinado e opera em inglês;
    comparar texto em português contra os chunks (também em inglês) faz o
    score de relevância desabar para praticamente zero em todos os
    candidatos, mesmo nos genuinamente relevantes — ver correção aplicada em
    _run_advanced_retrieval, que antes chamava esta função com a pergunta
    original em PT-BR por engano.

    O Ranker é cacheado por modelo (ver _get_ranker) e a inferência roda
    em thread separada (asyncio.to_thread) para nunca bloquear o event loop.

    Em caso de falha (biblioteca não instalada, modelo não disponível, etc.),
    retorna os chunks na ordem original do Qdrant como fallback.

    MULTI-TENANT — DOIS pontos afetados por `resolved_config` aqui, e vale
    distingui-los:
        1. KILL-SWITCH (`resolved_config.rag.reranker_enabled`): esta é a
           integração NOVA nesta correção — antes desta mudança, o campo
           `ResolvedRAGConfig.reranker_enabled` existia no schema mas
           NENHUM ponto do pipeline o lia; o reranking sempre rodava
           (exceto se o FlashRank falhasse ao inicializar). Um tenant que
           quisesse desabilitar o reranking (ex.: para reduzir latência,
           aceitando qualidade um pouco menor) não tinha como. Agora, se
           `resolved_config.rag.reranker_enabled` é False, esta função
           retorna os chunks na ordem original do Qdrant SEM sequer tentar
           carregar/usar o Ranker — mesmo comportamento de quando o
           FlashRank falha ao inicializar, mas por escolha explícita do
           tenant, não por erro.
        2. QUAL MODELO (`resolved_config.rag.reranker_model`, via
           `_get_ranker`): já discutido em `_get_ranker` — agora cacheado
           por modelo, não mais um singleton único, para não vazar a
           escolha do primeiro tenant do processo para os demais.

    Args:
        chunks: Lista deduplicada de chunks para reranking.
        query_text_en: Texto em INGLÊS usado como âncora do reranking
            (tipicamente a translated_query de decompose_query()).
        resolved_config: Configuração de tenant/coleção já resolvida, ou
            None se nenhum tenant foi resolvido (comportamento legado:
            reranking sempre tentado, modelo vem de settings).

    Returns:
        Lista reordenada pelo score do Cross-Encoder (mais relevante primeiro).
    """
    if not chunks:
        return chunks

    reranker_enabled = _rag_setting(resolved_config, "reranker_enabled", "rag_reranker_enabled", True)
    if not reranker_enabled:
        log.debug(
            "Reranking desabilitado via configuração (tenant/coleção ou "
            "settings.rag_reranker_enabled=False) — mantendo ordem original "
            "do Qdrant para %d chunks.",
            len(chunks),
        )
        return chunks

    ranker = await _get_ranker(resolved_config)
    if ranker is None:
        return chunks

    try:
        from flashrank import RerankRequest

        # Formata os passages no formato esperado pelo FlashRank
        passages = [
            {
                "id": f"{chunk.source_file}|{chunk.chunk_id}",
                "text": chunk.summary_markdown,
                "meta": {
                    "source_file": chunk.source_file,
                    "chunk_id": chunk.chunk_id,
                    "original_score": chunk.score,
                    "title": chunk.title,
                },
            }
            for chunk in chunks
        ]

        rerank_request = RerankRequest(query=query_text_en, passages=passages)
        # CORREÇÃO (bloqueio de event loop): inferência do Cross-Encoder é
        # CPU-bound e síncrona — despachada para thread para não travar o
        # loop enquanto outras requisições HTTP estão em andamento.
        reranked_results = await asyncio.to_thread(ranker.rerank, rerank_request)

        # Reconstrói a lista de RetrievedChunk na nova ordem do Cross-Encoder
        # Cria índice de busca rápida por (source_file, chunk_id)
        chunk_index: dict[tuple[str, int], RetrievedChunk] = {
            (c.source_file, c.chunk_id): c for c in chunks
        }

        reranked_chunks: list[RetrievedChunk] = []
        for result in reranked_results:
            passage_id: str = result.get("id", "")
            rerank_score: float = float(result.get("score", 0.0))

            # Reconstrói a chave a partir do ID composto "source_file|chunk_id"
            if "|" in passage_id:
                # rsplit para lidar com source_files que contenham "|"
                parts = passage_id.rsplit("|", 1)
                source_file = parts[0]
                try:
                    chunk_id = int(parts[1])
                except (ValueError, IndexError):
                    log.warning("ID de chunk inválido no resultado do FlashRank: %s", passage_id)
                    continue

                chunk = chunk_index.get((source_file, chunk_id))
                if chunk:
                    # Atualiza o score com o valor do Cross-Encoder para rastreabilidade
                    reranked_chunk = chunk.model_copy(
                        update={"score": rerank_score}
                    )
                    reranked_chunks.append(reranked_chunk)
            else:
                log.warning("Formato de ID inesperado do FlashRank: %s", passage_id)

        if not reranked_chunks:
            log.warning("FlashRank retornou lista vazia. Usando ordem original.")
            return chunks

        log.info(
            "Reranking concluído | %d chunks reordenados pelo Cross-Encoder",
            len(reranked_chunks),
        )
        log.debug(
            "Top-3 pós-reranking: %s",
            [(c.title[:40], round(c.score, 4)) for c in reranked_chunks[:3]],
        )
        return reranked_chunks

    except Exception as exc:
        log.warning(
            "FlashRank falhou durante o reranking: %s. "
            "Usando ordem original do Qdrant como fallback.",
            exc,
        )
        return chunks


# ---------------------------------------------------------------------------
# Função de retrieval original (Naive RAG — usado como fallback)
# ---------------------------------------------------------------------------

async def retrieve_chunks(
    qdrant: AsyncQdrantClient,
    query_vector: list[float],
    top_k: int,
    domain_id: str,
    project_source_filter: str | None = None,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Busca naive original: busca direta no Qdrant com top_k fixo e score_threshold.
    Mantida como fallback para o caso do pipeline avançado falhar completamente.

    CORREÇÃO (CRÍTICO — coleção hardcoded): `domain_id` obrigatório, usado
    para resolver a coleção Qdrant (ver `_resolve_qdrant_collection_name`)
    — sem isso o fallback naive continuaria batendo sempre na coleção UE5
    mesmo quando o pipeline avançado (já corrigido) estivesse operando em
    outro domínio.

    CORREÇÃO (project_source ignorado): assim como em retrieve_chunks_for_query,
    aplica `query_filter` quando `project_source_filter` é informado — o
    fallback naive não deve reintroduzir procedências que o chamador pediu
    para excluir.

    MULTI-TENANT: `resolved_config`, quando fornecido, também afeta este
    caminho de fallback — o naive RAG existe justamente para responder
    mesmo quando o pipeline avançado falhou catastroficamente, então ele
    PRECISA continuar respeitando isolamento de tenant (coleção correta) e
    `score_threshold` do tenant, não só o pipeline feliz. Ver
    `_resolve_qdrant_collection_name` e `_rag_setting`.

    Args:
        qdrant: Cliente Qdrant assíncrono.
        query_vector: Vetor de embedding da query original.
        top_k: Número de resultados desejados.
        project_source_filter: Se informado, restringe a busca a pontos com
            esse valor exato no campo `project_source` do payload.
        resolved_config: Configuração de tenant/coleção já resolvida, ou
            None se nenhum tenant foi resolvido (comportamento legado).

    Returns:
        Lista de RetrievedChunk.

    Raises:
        RuntimeError: se a busca no Qdrant falhar.
    """
    query_filter = _build_project_source_filter(project_source_filter)
    score_threshold = _rag_setting(resolved_config, "score_threshold", "rag_score_threshold", 0.6)
    try:
        results = await qdrant.search(
            collection_name=_resolve_qdrant_collection_name(domain_id, resolved_config),
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
            score_threshold=score_threshold,
            query_filter=query_filter,
        )
        log.debug("Recebidos %d resultados do Qdrant.", len(results))
        if results:
            log.debug("Payload de exemplo (1º resultado): %s", results[0].payload)
    except Exception as exc:
        log.error("Erro ao buscar no Qdrant (fallback naive): %s", exc)
        raise RuntimeError(f"Falha na busca vetorial: {exc}") from exc

    version_field = _engine_version_payload_key(domain_id)

    chunks: list[RetrievedChunk] = []
    for hit in results:
        payload = hit.payload or {}
        try:
            chunks.append(
                RetrievedChunk(
                    score=hit.score,
                    summary_markdown=payload.get("summary_markdown", ""),
                    title=payload.get("title", "Sem título"),
                    description=payload.get("description", ""),
                    source_file=payload.get("source_file", ""),
                    chunk_id=payload.get("chunk_id", 0),
                    # CORREÇÃO: mesma correção do caminho híbrido acima —
                    # ver _engine_version_payload_key.
                    ue_version=payload.get(version_field, ""),
                    doc_type=payload.get("doc_type", ""),
                    # CORREÇÃO (Correção 1): project_source agora é extraído
                    # do payload também no caminho naive.
                    project_source=payload.get("project_source", ""),
                    # HYBRID RAG: payload enriquecido — "" para pontos antigos
                    # ingeridos antes desta mudança (compatibilidade retroativa).
                    raw_content=payload.get("raw_content", ""),
                )
            )
        except Exception as exc:
            log.warning("Chunk ignorado por payload inválido: %s | %s", payload, exc)

    log.debug(
        "Retrieval naive: %d/%d chunks acima do threshold (%.2f)",
        len(chunks),
        top_k,
        score_threshold,
    )
    return chunks


# ---------------------------------------------------------------------------
# CORREÇÃO CRÍTICA — Backfill do raw_content (Referência Direta ausente)
# ---------------------------------------------------------------------------
# ingestion_service.py grava o `raw_content` completo apenas no chunk_id==0
# de cada `source_file` (ver _build_chunks_with_metadata), para não duplicar
# o conteúdo integral do arquivo em N pontos do Qdrant. O comentário lá é
# explícito: "o rag_service.py deve, ao montar a Referência Direta, cair de
# volta no raw_content do chunk_id==0 daquele source_file quando o chunk
# recuperado tiver None" — mas esse fallback nunca tinha sido implementado
# aqui. Como o retrieval é por CHUNK (não por arquivo), a maioria dos chunks
# retornados numa consulta normal tem chunk_id != 0, e _format_context
# descartava silenciosamente o bloco "B) REFERÊNCIA DIRETA" para eles — sem
# log, sem erro. Na prática, a garantia de sintaxe exata (assinaturas, tipos,
# UPROPERTY/UFUNCTION) que é a proposta central do Hybrid RAG só aparecia
# quando por acaso o chunk 0 do arquivo entrava no pool retornado.
#
# NOTA (Canal C / .cpp): o fallback usa `chunk.source_file`, NÃO
# `source_file_root`. Cada grupo de funções de um .cpp tem seu próprio
# source_file sintético ("path#groupN") e seu próprio raw_content isolado
# (o texto daquele grupo específico, não o arquivo .cpp inteiro) — ver
# _process_cpp_file. O chunk 0 "certo" para o fallback é sempre o do MESMO
# source_file do chunk que está com raw_content ausente.
async def _backfill_raw_content(
    qdrant: AsyncQdrantClient,
    chunks: list[RetrievedChunk],
    domain_id: str,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Preenche `raw_content` ausente (chunks com chunk_id != 0) buscando o
    chunk_id==0 do mesmo `source_file` diretamente por point_id
    determinístico (mesmo esquema uuid5 usado no upsert — ver
    ingestion_service.py::_flush_batch), sem custo de embedding e sem
    filtro por payload.

    Falhas aqui NUNCA devem derrubar a resposta — na pior hipótese, os
    chunks afetados simplesmente ficam sem a seção B) no contexto, como
    acontecia antes desta correção.

    CORREÇÃO (CRÍTICO — coleção hardcoded): `domain_id` obrigatório — o
    `qdrant.retrieve` por point_id precisa mirar a MESMA coleção de onde os
    chunks vieram (ver `_resolve_qdrant_collection_name`); buscar na
    coleção errada retornaria 404/vazio silenciosamente para todo domínio
    diferente de UE5, e o backfill nunca preencheria nada sem nenhum erro
    visível no log.

    MULTI-TENANT (mesmo motivo do resto do pipeline): sem `resolved_config`
    propagado até aqui, o backfill de um tenant B consultaria a coleção
    fixa por domínio (settings) em vez da coleção isolada do tenant B —
    na melhor hipótese um 404 silencioso (backfill vira no-op, chunks ficam
    sem "Referência Direta" mas a resposta não quebra), na pior hipótese
    (nomes de point_id colidindo entre coleções distintas — improvável dado
    o uuid5 determinístico por source_file, mas não impossível) um backfill
    com conteúdo de outro tenant. Propagar `resolved_config` até aqui
    fecha esse último ponto do pipeline de retrieval que ainda dependia de
    `settings.qdrant_collection_for`.
    """
    missing_sources = {
        chunk.source_file
        for chunk in chunks
        if chunk.chunk_id != 0
        and not (chunk.raw_content or "").strip()
        and chunk.source_file
    }
    if not missing_sources:
        return chunks

    point_ids = [
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}::0"))
        for source in missing_sources
    ]

    try:
        points = await qdrant.retrieve(
            collection_name=_resolve_qdrant_collection_name(domain_id, resolved_config),
            ids=point_ids,
            with_payload=["source_file", "raw_content"],
        )
    except Exception as exc:
        log.warning(
            "Backfill de raw_content falhou (%d arquivo(s) afetado(s)): %s. "
            "Referência Direta ficará ausente para os chunks correspondentes "
            "nesta resposta — não bloqueante.",
            len(missing_sources),
            exc,
        )
        return chunks

    raw_by_source: dict[str, str] = {
        p.payload["source_file"]: p.payload.get("raw_content", "")
        for p in points
        if p.payload and p.payload.get("source_file")
    }

    if not raw_by_source:
        return chunks

    backfilled = 0
    result: list[RetrievedChunk] = []
    for chunk in chunks:
        if (
            chunk.chunk_id != 0
            and not (chunk.raw_content or "").strip()
            and chunk.source_file in raw_by_source
        ):
            result.append(chunk.model_copy(update={"raw_content": raw_by_source[chunk.source_file]}))
            backfilled += 1
        else:
            result.append(chunk)

    log.info(
        "Backfill de raw_content: %d/%d chunk(s) sem Referência Direta "
        "preenchidos a partir do chunk 0 do respectivo arquivo.",
        backfilled,
        len(missing_sources),
    )
    return result


# ---------------------------------------------------------------------------
# Funções auxiliares de formatação de prompt
# ---------------------------------------------------------------------------

def _get_chunk_field(chunk: RetrievedChunk | dict, field: str, default: Any = "") -> Any:
    """
    Extração defensiva de um campo de chunk, aceitando tanto instâncias
    Pydantic de RetrievedChunk quanto dicts brutos (ex: payload cru vindo
    diretamente do Qdrant em algum caminho alternativo/debug/legado).

    Isolar essa lógica aqui evita duplicar o `isinstance` em todo ponto de
    _format_context e mantém a função principal legível.
    """
    if isinstance(chunk, dict):
        return chunk.get(field, default)
    return getattr(chunk, field, default)


def _format_context(chunks: list, domain_id: str) -> str:
    """
    Formata os chunks recuperados em um bloco de contexto legível para o LLM.

    CORREÇÃO (ALTO — fence de código hardcoded para cpp/markdown): o fence
    da seção [B) REFERÊNCIA DIRETA] era decidido por um ternário fixo
    (`"cpp" if doc_type == "cpp_header_api" else "markdown"`), que não faz
    sentido nenhum para doc_types de outro domínio (ex: "gdscript_api"
    cairia no ramo "markdown", perdendo o highlight de GDScript). Agora
    resolvido via `PromptFactory.get_code_fence(domain_id, doc_type)`, que
    consulta `BaseDomainProfile.code_fence_language_map` do domínio ativo.

    HYBRID RAG COM PAYLOAD ENRIQUECIDO:
        Cada documento de contexto agora é dividido em duas seções
        claramente isoladas, para permitir dupla validação pela LLM final:

        A) ANÁLISE SEMÂNTICA — o texto (`chunk.text`) gerado/sanitizado na
           ingestão, que é o que efetivamente foi embedado e trouxe este
           chunk para o topo do reranking. Ajuda o modelo a entender a
           INTENÇÃO de uso da classe/framework.
        B) REFERÊNCIA DIRETA — o `raw_content` armazenado no payload (código
           C++ limpo no Canal A, ou Markdown .udn limpo no Canal B),
           envelopado num bloco de código com a linguagem correta. Só é
           incluído quando o campo existe e não está vazio (chunks antigos,
           ingeridos antes desta mudança, simplesmente não têm essa seção).

    Aceita chunks como instâncias de RetrievedChunk (Pydantic) OU como dicts
    brutos — extração feita via `_get_chunk_field` para tolerar os dois
    formatos sem levantar AttributeError/KeyError.
    """
    if not chunks:
        return "Nenhum contexto de documentação relevante encontrado."

    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        text = _get_chunk_field(chunk, "summary_markdown", "")
        title = _get_chunk_field(chunk, "title", "Sem título")
        source_file = _get_chunk_field(chunk, "source_file", "")
        score = _get_chunk_field(chunk, "score", 0.0)
        doc_type = _get_chunk_field(chunk, "doc_type", "")
        raw_content = (_get_chunk_field(chunk, "raw_content", "") or "").strip()
        # CORREÇÃO (Correção 1): procedência do chunk (Engine nativa vs
        # projeto de amostra) agora é exibida explicitamente no bloco de
        # metadados — antes era recuperada do payload mas nunca chegava ao
        # prompt do LLM, que não tinha como saber se um trecho vinha da API
        # bruta da Engine ou de um projeto de amostra como Lyra.
        project_source = (_get_chunk_field(chunk, "project_source", "") or "").strip()

        source = source_file.split("/")[-1] if "/" in source_file else source_file

        block = (
            f"--- DOCUMENTO {i} ---\n"
            f"Título: {title}\n"
            f"Arquivo: {source}\n"
            f"Fonte do Projeto: {project_source or 'não especificada'}\n"
            f"Score de relevância: {score:.4f}\n\n"
            f"[A) ANÁLISE SEMÂNTICA]\n"
            f"{text}"
        )

        if raw_content:
            fence_lang = PromptFactory.get_code_fence(domain_id, doc_type)
            block += (
                f"\n\n[B) REFERÊNCIA DIRETA — verdade absoluta para sintaxe, "
                f"assinaturas e tipos]\n"
                f"```{fence_lang}\n{raw_content}\n```"
            )

        parts.append(block)

    return "\n\n".join(parts)


def _build_augmented_message(user_question: str, context: str, domain_id: str) -> str:
    """
    Monta a mensagem do usuário com o contexto RAG injetado.
    O contexto é apresentado antes da pergunta para maximizar a atenção do LLM.

    CORREÇÃO (ALTO — label hardcoded para UE5): o cabeçalho da seção de
    contexto era sempre "## Contexto da Documentação UE5", independente do
    domain_id resolvido para a requisição — o único ponto deste módulo que
    não seguia o padrão já aplicado em PromptFactory (system_prompt,
    decomposition_prompt, code_fence) e em domain_registry (coleção Qdrant,
    regex de tokenização). Toda requisição Godot 4 injetava esse texto
    literal no prompt do LLM, o que (a) rotulava incorretamente o contexto
    e (b) podia enviesar o modelo em direção a terminologia/APIs de UE5 ao
    responder perguntas sobre Godot — indo diretamente contra a REGRA 4 do
    GODOT4_SYSTEM_PROMPT ("use accurate Godot 4 terminology"). Agora resolve
    `display_name` do profile ativo via domain_registry, mesmo padrão usado
    no restante do arquivo.
    """
    profile = domain_registry.get(domain_id)
    return (
        f"## Contexto da Documentação — {profile.display_name}\n\n"
        f"{context}\n\n"
        f"---\n\n"
        f"## Pergunta\n\n"
        f"{user_question}"
    )


# ---------------------------------------------------------------------------
# Client HTTP compartilhado — chat/completions do LiteLLM
# ---------------------------------------------------------------------------
# NOTA DE PERFORMANCE: `call_litellm()` costumava abrir um httpx.AsyncClient
# novo a cada chamada (handshake TCP/TLS do zero a cada request). Isso é
# caro quando a função é chamada em volume alto — por exemplo, uma vez por
# header C++ durante a ingestão Code-to-Doc (potencialmente milhares de
# chamadas numa única execução de `process_folder_ingestion`). Agora
# reutilizamos um client singleton com connection pooling, no mesmo padrão
# já usado pelo EmbeddingService (ver embedding_service.py).

_litellm_client: httpx.AsyncClient | None = None


async def init_litellm_client() -> httpx.AsyncClient:
    """
    Cria o client HTTP compartilhado para chamadas de chat/completions ao
    LiteLLM. Deve ser chamado no lifespan startup do FastAPI (ver main.py).
    """
    global _litellm_client
    _litellm_client = httpx.AsyncClient(
        base_url=settings.litellm_base_url,
        headers=settings.litellm_headers,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    log.info("Client HTTP do LiteLLM (chat/completions) inicializado.")
    return _litellm_client


async def close_litellm_client() -> None:
    """Fecha o client HTTP compartilhado do LiteLLM. Chamado no shutdown."""
    global _litellm_client
    if _litellm_client:
        await _litellm_client.aclose()
        _litellm_client = None
        log.info("Client HTTP do LiteLLM (chat/completions) encerrado.")


def _get_litellm_client() -> httpx.AsyncClient:
    if _litellm_client is None:
        raise RuntimeError(
            "Client HTTP do LiteLLM não inicializado. Verifique o lifespan."
        )
    return _litellm_client


def _is_retryable_litellm_error(exc: BaseException) -> bool:
    """
    Define quais falhas justificam retry com backoff exponencial: timeouts,
    erros de conexão, e erros HTTP tipicamente transitórios (rate limit e
    5xx). Erros 4xx que não sejam 429 (payload malformado, modelo
    inexistente, etc.) NÃO são retentados — insistir não resolve um erro de
    requisição malformada, só atrasa a falha e desperdiça tentativas.

    CORREÇÃO (ALTO — retry não cobria erros de conexão): antes só
    httpx.TimeoutException era tratado como transitório. httpx.ConnectError
    (conexão recusada, DNS falhando, container do LiteLLM ainda subindo) é
    subclasse de httpx.TransportError, NÃO de TimeoutException — ficava de
    fora e desistia na primeira tentativa mesmo com retries configurados.
    httpx.RemoteProtocolError (conexão encerrada abruptamente pelo servidor,
    comum em restarts de proxy) tem o mesmo problema e foi incluído também.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


# CORREÇÃO (ALTO — @retry engessado em tempo de importação): antes,
# `stop=stop_after_attempt(settings.litellm_max_retries)` era avaliado UMA
# ÚNICA VEZ quando o módulo era importado — `stop_after_attempt(n)` captura
# `n` por valor naquele instante e devolve um objeto imutável. Qualquer
# alteração de `settings.litellm_max_retries` em runtime (hot-reload do
# Pydantic Settings, override via variável de ambiente recarregada, etc.)
# era completamente ignorada até o processo ser reiniciado. `tenacity`
# aceita qualquer callable `(RetryCallState) -> bool` em `stop=`, não só as
# fábricas prontas (stop_after_attempt, stop_after_delay, ...) — então em
# vez de reimplementar a lógica de "parar após N tentativas" à mão,
# delegamos para uma instância de stop_after_attempt construída NA HORA,
# dentro do callable, lendo settings.litellm_max_retries a cada chamada.
def _stop_after_dynamic_attempts(retry_state) -> bool:
    """Lê settings.litellm_max_retries em runtime a cada avaliação de
    retry, em vez de congelar o valor em tempo de importação do módulo."""
    current_max_retries = settings.litellm_max_retries
    return stop_after_attempt(current_max_retries)(retry_state)


@retry(
    retry=retry_if_exception(_is_retryable_litellm_error),
    wait=wait_exponential_jitter(initial=2, max=30),
    stop=_stop_after_dynamic_attempts,
    reraise=True,
)
async def _post_chat_completion(
    client: httpx.AsyncClient, path: str, payload: dict
) -> httpx.Response:
    resp = await client.post(path, json=payload)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Chamada ao LiteLLM
# ---------------------------------------------------------------------------

async def call_litellm(
    messages: list[dict],
    model: str,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> tuple[str, int | None, int | None, str | None]:
    """
    Chama o LiteLLM proxy com o histórico de mensagens e retorna a resposta.

    Reutiliza o client HTTP compartilhado (ver init_litellm_client) e tenta
    novamente com backoff exponencial + jitter em caso de erro transitório
    (timeout, HTTP 429 ou 5xx) — até `settings.litellm_max_retries`
    tentativas antes de desistir.

    Returns:
        Tupla (resposta_texto, prompt_tokens, completion_tokens).
        Os tokens podem ser None se o modelo não reportar usage.

    Raises:
        RuntimeError: em caso de erro HTTP persistente (após as tentativas
            de retry) ou resposta malformada.
    """
    client = _get_litellm_client()

    # CORREÇÃO (Bug 1): max_tokens agora é configurável via cascata:
    # resolved_config.rag.max_output_tokens > settings.rag_max_output_tokens > default 4096
    # Calibrado empiricamente a partir de ~12 testes manuais (ver config.py para detalhes).
    max_tokens = _rag_setting(resolved_config, "max_output_tokens", "rag_max_output_tokens", 4096)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,       # baixo para respostas factuais/técnicas
        "max_tokens": max_tokens,
    }

    try:
        resp = await _post_chat_completion(client, "/chat/completions", payload)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM retornou HTTP {exc.response.status_code} (model={model}) "
            f"após esgotar as tentativas de retry: {exc.response.text[:300]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Timeout ao chamar LiteLLM (model={model}) após esgotar as "
            "tentativas de retry. Verifique se o modelo está carregado."
        ) from exc

    data = resp.json()

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Resposta do LiteLLM em formato inesperado: {data}"
        ) from exc

    # CORREÇÃO (Bug 1): Extrair finish_reason para detectar truncamento
    finish_reason = data["choices"][0].get("finish_reason") if data.get("choices") else None

    usage = data.get("usage", {})
    return answer, usage.get("prompt_tokens"), usage.get("completion_tokens"), finish_reason


# ---------------------------------------------------------------------------
# Pipeline avançado — núcleo do Advanced RAG
# ---------------------------------------------------------------------------

async def _run_advanced_retrieval(
    user_question: str,
    qdrant: AsyncQdrantClient,
    top_k: int,
    domain_id: str,
    project_source_filter: str | None = None,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Executa as fases de Decomposição + Busca Expandida + Deduplicação + Reranking.

    Este é o coração do Advanced RAG Pipeline. Estruturado como função separada
    para facilitar o isolamento de falhas e o fallback no chamador principal.

    Estratégia de concorrência:
        - A decomposição e o embedding da query original rodam em PARALELO
          via asyncio.gather(), eliminando a latência serial.
        - Os embeddings das sub-queries rodam em paralelo entre si após
          a decomposição retornar.

    MULTI-TENANT: `resolved_config`, quando fornecido, é o objeto que
    carrega TODOS os overrides de tenant/coleção resolvidos pelo endpoint
    (ver chat.py) — esta função é a orquestradora principal e apenas
    REPASSA `resolved_config` para cada fase (decompose_query,
    _embed_and_retrieve → retrieve_chunks_for_query, rerank_with_flashrank)
    e o usa diretamente para os dois parâmetros que pertencem a este nível
    de orquestração (retrieval_limit, min_score). Nenhuma lógica de
    resolução de config vive aqui — só propagação.

    Args:
        user_question: Pergunta original do usuário.
        qdrant: Cliente Qdrant assíncrono.
        top_k: Número final de chunks desejados após reranking.
        project_source_filter: Se informado, restringe TODAS as buscas
            (tradução + sub-queries) a essa procedência — propagado desde
            ChatRequest.project_source_filter (Correção 1).
        resolved_config: Configuração de tenant/coleção já resolvida pelo
            endpoint, ou None se nenhum tenant foi resolvido para esta
            requisição — nesse caso todo o pipeline usa settings global
            (comportamento idêntico ao pré-integração multi-tenant).

    Returns:
        Lista de RetrievedChunk reranqueados, limitada ao top_k e filtrada
        pelo piso de qualidade pós-reranking (rag_rerank_min_score, via
        resolved_config ou settings — ver _rag_setting).
        Pode estar vazia se o Qdrant não retornar resultados.
    """
    # Lê o limite de busca expandida — tenant/coleção > settings > constante
    # local (ver _rag_setting).
    retrieval_limit = _rag_setting(resolved_config, "retrieval_limit_per_query", "rag_retrieval_limit_per_query", _RETRIEVAL_LIMIT_PER_QUERY)

    # === FASE 1 — Decomposição/Tradução ===
    # CORREÇÃO CRÍTICA: anteriormente esta fase disparava embed_text(user_question)
    # em paralelo com a decomposição "para ganhar latência", e reaproveitava esse
    # vetor (calculado sobre o texto em PT-BR) como a busca "principal". Isso
    # bypassava completamente a tradução gerada por decompose_query() e garantia
    # que a busca de maior peso no pool de reranking fosse sempre cross-lingual
    # (query em português contra corpus 100% em inglês — ver ingestion_service.py).
    # Essa era a causa raiz dos scores baixos / chunks existentes não recuperados.
    #
    # Agora esperamos decompose_query() primeiro: ela já retorna [tradução, *sub_queries],
    # todas em inglês, e SÓ ENTÃO embedamos. Perdemos o paralelismo de ~1 chamada
    # de embedding, mas eliminamos a busca cross-lingual garantida em toda requisição.
    log.info("Advanced RAG: traduzindo e decompondo a query...")

    decomposed = await decompose_query(user_question, domain_id=domain_id, resolved_config=resolved_config)
    translated_query = decomposed.translated_query
    sub_queries = decomposed.sub_queries
    keywords_exact = decomposed.keywords_exact

    log.info(
        "Buscando no Qdrant: 1 query traduzida + %d sub-queries em paralelo "
        "(%d keywords_exact para o canal esparso)...",
        len(sub_queries),
        len(keywords_exact),
    )

    # Todas as queries (tradução + sub-queries) são embedadas e buscadas em
    # paralelo entre si — nenhuma delas está em português neste ponto, exceto
    # no caso extremo de falha total de tradução (ver decompose_query).
    # As mesmas keywords_exact (extraídas uma vez para a pergunta inteira)
    # são propagadas a TODAS as buscas paralelas — ativam o canal esparso
    # da busca híbrida em cada uma (ver retrieve_chunks_for_query).
    # `resolved_config` também é propagado idêntico a todas — a config de
    # tenant/coleção não muda entre sub-queries da MESMA requisição.
    retrieval_tasks: list[asyncio.Task] = [
        asyncio.create_task(
            _embed_and_retrieve(
                translated_query,
                qdrant,
                retrieval_limit,
                domain_id=domain_id,
                project_source_filter=project_source_filter,
                keywords_exact=keywords_exact,
                resolved_config=resolved_config,
            )
        )
    ]

    for sub_query in sub_queries:
        retrieval_tasks.append(
            asyncio.create_task(
                _embed_and_retrieve(
                    sub_query,
                    qdrant,
                    retrieval_limit,
                    domain_id=domain_id,
                    project_source_filter=project_source_filter,
                    keywords_exact=keywords_exact,
                    resolved_config=resolved_config,
                )
            )
        )

    # Executa todas as buscas em paralelo
    retrieval_results: list[list[RetrievedChunk]] = await asyncio.gather(
        *retrieval_tasks,
        return_exceptions=False,  # falhas individuais já são tratadas dentro de retrieve_chunks_for_query
    )

    # Achata a lista de listas em uma lista única
    all_chunks: list[RetrievedChunk] = [
        chunk
        for result_list in retrieval_results
        for chunk in result_list
    ]

    log.info(
        "Retrieval expandido concluído: %d chunks brutos (antes da deduplicação)",
        len(all_chunks),
    )

    if not all_chunks:
        log.warning("Nenhum chunk recuperado em nenhuma das queries expandidas.")
        return []

    # === FASE 2.2 — Deduplicação ===
    unique_chunks = _deduplicate_chunks(all_chunks)

    log.info(
        "Pool pós-deduplicação: %d chunks únicos para o reranker",
        len(unique_chunks),
    )

    # === FASE 2.3 — Reranking (CPU-bound, despachado para thread) ===
    # FlashRank é rápido (~10-50ms para dezenas de chunks), mas ainda assim
    # síncrono e bloqueante — rerank_with_flashrank despacha a inferência via
    # asyncio.to_thread internamente para não travar o event loop sob
    # concorrência (ver correção em _get_ranker/rerank_with_flashrank).
    #
    # CORREÇÃO CRÍTICA (mismatch cross-lingual no reranking): antes, esta
    # chamada usava `user_question` — a pergunta ORIGINAL em PT-BR — como
    # âncora de comparação do Cross-Encoder. O modelo do FlashRank
    # (ms-marco-MiniLM, ver _FLASHRANK_MODEL) é treinado e opera em INGLÊS;
    # comparar texto em português contra os chunks em inglês faz o
    # Cross-Encoder atribuir scores de relevância artificialmente baixos e
    # quase uniformes a TODOS os candidatos (mesmo aos genuinamente
    # relevantes) — exatamente o mesmo mismatch cross-lingual que
    # decompose_query()/_run_advanced_retrieval já corrigem para a fase de
    # embedding, mas que nunca tinha sido aplicado à fase de reranking. Isso
    # não bloqueia a resposta (o pipeline segue funcionando e retornando
    # chunks plausíveis, já que o Qdrant já tinha feito o trabalho pesado de
    # retrieval com a query traduzida), mas o rerank final é essencialmente
    # ruído — o campo `score` nos chunks retornados fica artificialmente
    # próximo de zero e não reflete relevância real, e a reordenação que o
    # Cross-Encoder aplica também perde valor. Usar `translated_query` (já
    # disponível neste escopo, calculado na Fase 1 acima) resolve o mismatch.
    #
    # `resolved_config` propagado aqui também: decide se o reranking roda
    # (reranker_enabled) e com qual modelo (reranker_model) — ver
    # rerank_with_flashrank.
    reranked = await rerank_with_flashrank(unique_chunks, translated_query, resolved_config=resolved_config)

    # === FASE 2.4 — Piso de Qualidade Pós-Reranking (Correção 2) ===
    # CORREÇÃO (ALTO — ausência de piso de qualidade pós-reranking): antes,
    # a seleção final simplesmente recortava `reranked[:top_k]`, sem levar
    # em conta se os scores do Cross-Encoder eram bons ou ruins. Se o
    # retrieval expandido trouxesse só ruído (nenhum chunk genuinamente
    # relevante para a pergunta), o pipeline injetava esse ruído no prompt
    # do LLM do mesmo jeito, só porque havia "espaço" no top_k. Agora
    # aplicamos um piso de qualidade ANTES de recortar por top_k —
    # reranked já vem ordenado do maior para o menor score (ver
    # rerank_with_flashrank), então filtrar preserva a ordem dos
    # sobreviventes.
    #
    # MULTI-TENANT: min_score agora vem de resolved_config.rag.rerank_min_score
    # quando um tenant foi resolvido — ver docstring de RAGConfig.rerank_min_score
    # em app/schemas/tenant.py sobre a diferença de escala entre esse valor
    # (score do cross-encoder do FlashRank) e score_threshold (cosine
    # similarity, usado só no fallback naive) — um tenant configurando este
    # campo copiando a intuição de score_threshold pode, sem querer, zerar
    # todos os chunks pós-reranking (mitigado abaixo pelo fallback "mantém
    # o melhor candidato mesmo sem atingir o piso").
    min_score = _rag_setting(resolved_config, "rerank_min_score", "rag_rerank_min_score", 0.0)
    if min_score is None:
        min_score = 0.0

    above_threshold = [c for c in reranked if c.score >= min_score]

    if not above_threshold and reranked:
        # Nenhum chunk atingiu o piso — mantemos ao menos o melhor candidato
        # em vez de deixar o prompt sem nenhum contexto. A REGRA 2 do
        # system_prompt do domínio ativo (ver PromptFactory.get_prompts)
        # já instrui o modelo a avisar explicitamente quando a documentação
        # não cobre o tópico — verdade tanto no UE5Profile quanto no
        # Godot4Profile — então um único chunk fraco não arrisca uma
        # resposta inventada, só dá ao LLM a chance de constatar a lacuna
        # com algo concreto na mão.
        log.warning(
            "Nenhum chunk atingiu o piso de qualidade pós-reranking "
            "(rerank_min_score=%.4f, melhor score=%.4f). Mantendo "
            "apenas o chunk de maior score para não deixar o prompt sem "
            "contexto algum.",
            min_score,
            reranked[0].score,
        )
        above_threshold = reranked[:1]

    final_chunks = above_threshold[:top_k]

    log.info(
        "Advanced RAG: %d chunks selecionados após reranking + piso de "
        "qualidade (top_k=%d, min_score=%.4f, descartados_por_qualidade=%d)",
        len(final_chunks),
        top_k,
        min_score,
        len(reranked) - len(above_threshold),
    )

    return final_chunks


async def _embed_and_retrieve(
    query: str,
    qdrant: AsyncQdrantClient,
    limit: int,
    domain_id: str,
    project_source_filter: str | None = None,
    keywords_exact: list[str] | None = None,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> list[RetrievedChunk]:
    """
    Função helper: gera embedding de uma query e busca no Qdrant.
    Encadeia embed_text() e retrieve_chunks_for_query() de forma segura.

    Em caso de erro no embedding ou na busca, retorna lista vazia
    (não propaga a exceção — falhas individuais de sub-queries não devem
    derrubar o pipeline inteiro).

    NOTA: `embed_text(query)` continua sem receber `resolved_config` — a
    dimensão/modelo de embedding (`ResolvedEmbeddingConfig`) é resolvida
    separadamente e NÃO foi integrada nesta correção (ver relatório —
    escopo desta rodada foi limitado a `ResolvedRAGConfig` e ao nome da
    coleção Qdrant, não a `ResolvedEmbeddingConfig`). Ver seção "O que
    fica pendente" do relatório para o motivo dessa fronteira de escopo.

    Args:
        query: Texto da sub-query.
        qdrant: Cliente Qdrant assíncrono.
        limit: Número máximo de chunks a recuperar.
        project_source_filter: Repassado para retrieve_chunks_for_query —
            restringe a busca por procedência quando informado (Correção 1).
        keywords_exact: Repassado para retrieve_chunks_for_query — ativa o
            canal esparso da busca híbrida quando presente.
        resolved_config: Configuração de tenant/coleção já resolvida,
            repassada para retrieve_chunks_for_query (coleção Qdrant
            correta + kill-switch de busca híbrida).

    Returns:
        Lista de RetrievedChunk, ou lista vazia em caso de falha.
    """
    try:
        vector = await embed_text(query)
        return await retrieve_chunks_for_query(
            qdrant,
            vector,
            limit,
            domain_id=domain_id,
            project_source_filter=project_source_filter,
            keywords_exact=keywords_exact,
            resolved_config=resolved_config,
        )
    except Exception as exc:
        log.warning("Falha ao processar sub-query '%s': %s", query[:60], exc)
        return []


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

async def run_rag_pipeline(
    request: ChatRequest,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    resolved_config: ResolvedCollectionConfig | None = None,
) -> ChatResponse:
    """
    Executa o Advanced RAG Pipeline completo para uma requisição de chat.

    Fluxo Principal (Advanced RAG):
        1. [PARALELO] Decomposição da query (LLM leve) + Embedding da query original
        2. [PARALELO] Busca expandida no Qdrant para todas as queries
        3. Deduplicação in-memory por (source_file, chunk_id)
        4. Reranking Cross-Encoder via FlashRank (CPU, ~10-50ms)
        5. Seleção top-k pós-reranking
        6. Augmentação do prompt com contexto reranqueado
        7. Chamada ao LLM via LiteLLM
        8. Persistência do turno no Redis

    Fallback Automático (Naive RAG):
        Se QUALQUER etapa do pipeline avançado falhar, o sistema faz fallback
        transparente para o RAG ingênuo original (embedding direto + busca simples).
        O usuário nunca vê um HTTP 500 por falha do pipeline avançado.

    INTEGRAÇÃO MULTI-TENANT — LEIA COM ATENÇÃO (ver também o bloco de
    comentário no topo do módulo, logo após os imports):

    `resolved_config` é o ÚNICO ponto de entrada da config de tenant/coleção
    neste módulo inteiro. Esta função NUNCA resolve tenant sozinha — não
    importa `AsyncSession`, não importa `TenantConfigService`, não toca o
    Postgres. Por design: `rag_service.py` deve continuar puro em relação a
    infraestrutura de tenant, para (a) não duplicar a lógica de cache
    (L1 processo + L2 Redis) que já vive em `tenant_config_service.py` e
    (b) manter esta função testável sem precisar de uma sessão de DB mockada
    toda vez que se quer testar só a lógica de retrieval/reranking.

    QUEM RESOLVE `resolved_config`, ENTÃO? O endpoint
    (`app/api/v1/endpoints/chat.py`), ANTES de chamar esta função — via
    `TenantConfigService.resolve_collection_config(db, tenant_id,
    collection_name)` (usa `ChatRequest.collection_name`, se houver) ou
    `resolve_default_collection_for_domain(db, tenant_id, domain_id)`
    (quando só o domínio é conhecido). Ver a implementação atualizada de
    `chat.py` para o padrão exato — a ideia central é: resolver 1x no
    início do handler, propagar já resolvido para cá.

    QUANDO `resolved_config` VEM COMO None (o caso comum em desenvolvimento
    single-tenant, com foco em Godot 4 — PRIORIDADE ATUAL): TODO parâmetro
    de RAG cai para `settings.rag_*` (global, idêntico ao comportamento
    pré-integração) via `_rag_setting`, e a coleção Qdrant cai para
    `settings.qdrant_collection_for(domain_id)` via
    `_resolve_qdrant_collection_name`. Ou seja: rodar sem `resolved_config`
    é um modo de operação totalmente suportado e é o caminho que este app,
    hoje, deve exercitar continuamente — não um caminho degradado.

    Args:
        request: Dados validados da requisição (ChatRequest).
        qdrant: Cliente Qdrant injetado via Depends.
        redis: Cliente Redis injetado via Depends.
        resolved_config: Configuração de tenant/coleção já resolvida pelo
            endpoint (ver acima), ou None se nenhum tenant foi resolvido
            para esta requisição (settings global — comportamento legado).

    Returns:
        ChatResponse com resposta, chunks usados e tokens consumidos.
    """
    # CORREÇÃO (CRÍTICO — domain_id ausente em todo o pipeline): resolvido
    # uma única vez aqui e propagado explicitamente a TODAS as chamadas
    # downstream (decomposição, retrieval híbrido/naive, backfill,
    # formatação de contexto, prompt de geração). `request.domain_id` deve
    # ser adicionado ao schema ChatRequest (ver PATCH_NOTES.md) — até lá,
    # `getattr` com fallback para `settings.active_domain` mantém este
    # arquivo funcional mesmo antes da migração do schema.
    #
    # MULTI-TENANT: quando `resolved_config` está disponível, ele já traz
    # `domain_id` correto (resolvido a partir da Collection no Postgres —
    # ver ResolvedCollectionConfig.domain_id em tenant_config_service.py),
    # e é PREFERIDO sobre `request.domain_id`/`settings.active_domain`: é a
    # fonte da verdade de qual domínio pertence à coleção do tenant, mais
    # confiável que um domain_id solto vindo do client. Se `resolved_config`
    # for None, o comportamento é idêntico ao pré-integração.
    domain_id = (
        resolved_config.domain_id
        if resolved_config is not None
        else (getattr(request, "domain_id", None) or settings.active_domain)
    )

    log.info(
        "Advanced RAG pipeline | domain=%s tenant=%s session=%s model=%s top_k=%d",
        domain_id,
        resolved_config.tenant_slug if resolved_config is not None else "(nenhum — settings global)",
        request.session_id,
        request.model,
        request.top_k,
    )

    # Determina o top_k efetivo: usa o do request, mas respeita o mínimo recomendado
    # para o Advanced RAG ter valor (8 garante contexto rico sem overflow de tokens).
    # CORREÇÃO (ALTO — bug matemático): a fórmula anterior era
    # `max(request.top_k, min(request.top_k, 8))`, que é sempre igual a
    # request.top_k para qualquer valor (min(x, 8) <= x, logo max(x, algo <= x)
    # == x) — o floor de 8 nunca era aplicado na prática. Se o cliente pedisse
    # top_k=1 ou 2, o Advanced RAG rodava com esse valor raquítico mesmo.
    #
    # NOTA MULTI-TENANT: `request.top_k` continua sendo a fonte usada aqui
    # (preferência explícita do CLIENTE nesta requisição específica), não
    # `resolved_config.rag.top_k` (default do TENANT). Isso é intencional —
    # `ResolvedRAGConfig.top_k` é o valor a usar quando o client NÃO
    # especificar um top_k explícito. Como `ChatRequest.top_k` hoje não é
    # Optional (é obrigatório com default fixo no schema, pelo que se pode
    # inferir do uso em chat.py), não há como distinguir "cliente pediu
    # exatamente este valor" de "cliente não informou, ficou no default do
    # schema" neste snapshot — ver PENDÊNCIA no relatório sobre tornar
    # `ChatRequest.top_k` Optional para permitir essa distinção e então
    # preencher o default a partir de `resolved_config.rag.top_k` aqui.
    effective_top_k = max(request.top_k, 8)

    # Correção 1: propaga o filtro de procedência (Engine nativa vs projeto
    # de amostra) do request até as duas trilhas de retrieval abaixo
    # (Advanced e o fallback Naive) — antes esse campo do payload era
    # gravado na ingestão mas nunca chegava a ser usado numa busca.
    project_source_filter = request.project_source_filter

    # =========================================================================
    # TENTATIVA: Advanced RAG Pipeline
    # =========================================================================
    chunks: list[RetrievedChunk] = []
    pipeline_used = "advanced"

    try:
        chunks = await _run_advanced_retrieval(
            user_question=request.message,
            qdrant=qdrant,
            top_k=effective_top_k,
            domain_id=domain_id,
            project_source_filter=project_source_filter,
            resolved_config=resolved_config,
        )

    except Exception as exc:
        # =====================================================================
        # FALLBACK: Naive RAG Pipeline
        # Se o pipeline avançado falhar catastroficamente, fazemos fallback
        # para o RAG ingênuo original. O usuário recebe uma resposta (possivelmente
        # menos rica em contexto) em vez de um erro HTTP 500.
        # =====================================================================
        log.warning(
            "Advanced RAG falhou com erro inesperado: %s. "
            "Fazendo fallback para Naive RAG.",
            exc,
            exc_info=True,
        )
        pipeline_used = "naive_fallback"

        # NOTA: este fallback embeda request.message CRU (potencialmente em
        # PT-BR) sem tradução, porque só é acionado quando o Advanced RAG já
        # falhou catastroficamente (Qdrant/LiteLLM instável) — tentar mais uma
        # chamada de tradução aqui arriscaria falhar de novo. Scores baixos
        # neste caminho são esperados e não devem ser confundidos com o bug
        # de mismatch cross-lingual do pipeline principal (já corrigido em
        # _run_advanced_retrieval/decompose_query).
        log.warning(
            "Naive fallback usará a query original SEM tradução (pipeline "
            "avançado indisponível) — scores mais baixos são esperados aqui."
        )
        try:
            original_vector = await embed_text(request.message)
            chunks = await retrieve_chunks(
                qdrant,
                original_vector,
                effective_top_k,
                domain_id=domain_id,
                project_source_filter=project_source_filter,
                resolved_config=resolved_config,
            )
        except RuntimeError as naive_exc:
            log.error("Naive RAG fallback também falhou: %s", naive_exc)
            raise  # propaga apenas se o fallback também falhar

    log.info(
        "Retrieval concluído | pipeline=%s chunks=%d",
        pipeline_used,
        len(chunks),
    )

    if not chunks:
        log.warning(
            "Nenhum chunk recuperado para session=%s. "
            "Respondendo sem contexto de documentação.",
            request.session_id,
        )
    else:
        # CORREÇÃO CRÍTICA: preenche raw_content ausente (chunk_id != 0)
        # antes de montar o contexto — ver _backfill_raw_content. Não
        # bloqueante: se falhar, os chunks seguem sem a seção B) como já
        # acontecia antes desta correção.
        chunks = await _backfill_raw_content(qdrant, chunks, domain_id=domain_id, resolved_config=resolved_config)

    # =========================================================================
    # AUGMENTAÇÃO + GERAÇÃO
    # =========================================================================

    context = _format_context(chunks, domain_id=domain_id)
    augmented_user_message = _build_augmented_message(request.message, context, domain_id=domain_id)

    # MULTI-TENANT: `resolved_config.system_prompt` já contempla a cadeia de
    # fallback "override de coleção (CollectionConfig.system_prompt_override)
    # > default do domínio (BaseDomainProfile.system_prompt)" — ver
    # `TenantConfigService._build_resolved_config`. Isso é estritamente mais
    # correto que `PromptFactory.get_prompts(domain_id).system_prompt`, que
    # SEMPRE ignora qualquer override de tenant, mesmo que um exista
    # cadastrado no Postgres. Quando `resolved_config` é None, o
    # comportamento cai para PromptFactory — idêntico ao pré-integração.
    system_prompt = (
        resolved_config.system_prompt
        if resolved_config is not None
        else PromptFactory.get_prompts(domain_id).system_prompt
    )

    messages = await build_messages_for_llm(
        redis=redis,
        session_id=request.session_id,
        user_message=augmented_user_message,
        system_prompt=system_prompt,
    )

    try:
        answer, prompt_tokens, completion_tokens, finish_reason = await call_litellm(
            messages=messages,
            model=request.model,
            resolved_config=resolved_config,
        )
    except RuntimeError as exc:
        log.error("Falha ao chamar LiteLLM: %s", exc)
        raise

    # =========================================================================
    # PERSISTÊNCIA
    # =========================================================================

    await save_turn(
        redis=redis,
        session_id=request.session_id,
        user_message=request.message,   # salva a mensagem original, não a aumentada
        assistant_reply=answer,
        model_used=request.model,
    )

    log.info(
        "Pipeline concluído | session=%s pipeline=%s chunks=%d "
        "tokens_in=%s tokens_out=%s",
        request.session_id,
        pipeline_used,
        len(chunks),
        prompt_tokens,
        completion_tokens,
    )

    return ChatResponse(
        session_id=request.session_id,
        answer=answer,
        model_used=request.model,
        retrieved_chunks=chunks,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )
    

async def call_litellm_stream(
    messages: list[dict],
    model: str,
    resolved_config: "ResolvedCollectionConfig | None" = None,
):
    """
    Chama o LiteLLM em modo streaming (SSE, formato OpenAI-compatible) e
    faz yield de cada pedaço de texto (delta) assim que chega.
 
    Yields:
        str — pedaço de texto (pode ser vazio, pular nesse caso).
 
    Ao final, deixa disponível via atributo do generator (`.prompt_tokens`,
    `.completion_tokens`) os valores de usage, SE o provider os enviar —
    ver run_rag_pipeline_stream para como isso é lido.
 
    Raises:
        RuntimeError: erro HTTP na abertura da conexão (mesmo tratamento
            de call_litellm — sem retry automático depois que o stream
            já começou a chegar, para não duplicar tokens já emitidos).
    """
    client = _get_litellm_client()

    # CORREÇÃO (Bug 1): max_tokens agora é configurável via cascata:
    # resolved_config.rag.max_output_tokens > settings.rag_max_output_tokens > default 4096
    # Calibrado empiricamente a partir de ~12 testes manuais (ver config.py para detalhes).
    max_tokens = _rag_setting(resolved_config, "max_output_tokens", "rag_max_output_tokens", 4096)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},  # OpenAI-compatible; ignorado se o backend não suportar
    }
 
    try:
        async with client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
 
            usage: dict = {}
            finish_reason: str | None = None
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line.removeprefix("data:").strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = _json.loads(data_str)
                except _json.JSONDecodeError:
                    log.warning("Chunk SSE não-JSON ignorado do LiteLLM: %r", data_str[:200])
                    continue

                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
                    # CORREÇÃO (Bug 1): Extrair finish_reason do último chunk
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0].get("finish_reason")

                if chunk.get("usage"):
                    usage = chunk["usage"]

            call_litellm_stream.last_usage = usage  # type: ignore[attr-defined]
            call_litellm_stream.last_finish_reason = finish_reason  # type: ignore[attr-defined]
 
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM retornou HTTP {exc.response.status_code} (model={model}, stream=True): "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Timeout ao chamar LiteLLM em modo streaming (model={model})."
        ) from exc
 
 
async def run_rag_pipeline_stream(
    request: ChatRequest,
    qdrant: AsyncQdrantClient,
    redis: Redis,
    resolved_config: ResolvedCollectionConfig | None = None,
):
    """
    Versão streaming de run_rag_pipeline(). Mesma Fase 1 (decomposição) +
    Fase 2 (retrieval expandido + reranking) + mesmo fallback Naive RAG —
    só a Fase 3 (geração) muda para streaming.
 
    Yields eventos no formato consumido por
    app/api/v1/endpoints/chat.py::_stream_chat_response:
 
        {"type": "sources", "retrieved_chunks": [RetrievedChunk, ...]}
        {"type": "token", "content": "..."}
        {"type": "done", "model_used", "prompt_tokens", "completion_tokens"}
 
    Não levanta exceção para o chamador em caso de falha do LLM depois que
    o retrieval já deu certo — nesse caso levanta RuntimeError normalmente
    (igual run_rag_pipeline), e quem trata isso é o endpoint (persiste como
    mensagem de erro e emite {"type": "error", ...} via SSE).

    INTEGRAÇÃO MULTI-TENANT: espelha EXATAMENTE a mesma integração de
    `run_rag_pipeline` — ver a docstring completa lá para a explicação de
    por que `resolved_config` é resolvido pelo endpoint (não aqui), o que
    acontece quando é None, e por que `resolved_config.domain_id` tem
    prioridade sobre `request.domain_id` quando disponível. Esta função é
    deliberadamente mantida em paralelo estrutural com a versão síncrona —
    qualquer mudança na integração multi-tenant de uma deve ser replicada
    na outra.

    Args:
        request: Dados validados da requisição (ChatRequest).
        qdrant: Cliente Qdrant injetado via Depends.
        redis: Cliente Redis injetado via Depends.
        resolved_config: Configuração de tenant/coleção já resolvida pelo
            endpoint, ou None se nenhum tenant foi resolvido (settings
            global — comportamento legado, ver run_rag_pipeline).
    """
    # CORREÇÃO (CRÍTICO — domain_id ausente): mesma resolução usada em
    # run_rag_pipeline — ver comentário lá para o motivo do getattr/fallback
    # e para por que resolved_config.domain_id tem prioridade quando existe.
    domain_id = (
        resolved_config.domain_id
        if resolved_config is not None
        else (getattr(request, "domain_id", None) or settings.active_domain)
    )

    log.info(
        "Advanced RAG pipeline (stream) | domain=%s tenant=%s session=%s model=%s top_k=%d",
        domain_id,
        resolved_config.tenant_slug if resolved_config is not None else "(nenhum — settings global)",
        request.session_id,
        request.model,
        request.top_k,
    )
 
    effective_top_k = max(request.top_k, 8)
    project_source_filter = request.project_source_filter
 
    # -------------------------------------------------------------------
    # FASE 1 + 2 — idêntico a run_rag_pipeline (retrieval + fallback naive)
    # -------------------------------------------------------------------
    chunks: list[RetrievedChunk] = []
    pipeline_used = "advanced"
 
    try:
        chunks = await _run_advanced_retrieval(
            user_question=request.message,
            qdrant=qdrant,
            top_k=effective_top_k,
            domain_id=domain_id,
            project_source_filter=project_source_filter,
            resolved_config=resolved_config,
        )
    except Exception as exc:
        log.warning(
            "Advanced RAG (stream) falhou com erro inesperado: %s. Fazendo fallback para Naive RAG.",
            exc,
            exc_info=True,
        )
        pipeline_used = "naive_fallback"
        try:
            original_vector = await embed_text(request.message)
            chunks = await retrieve_chunks(
                qdrant,
                original_vector,
                effective_top_k,
                domain_id=domain_id,
                project_source_filter=project_source_filter,
                resolved_config=resolved_config,
            )
        except RuntimeError as naive_exc:
            log.error("Naive RAG fallback (stream) também falhou: %s", naive_exc)
            raise
 
    log.info("Retrieval (stream) concluído | pipeline=%s chunks=%d", pipeline_used, len(chunks))
 
    if chunks:
        chunks = await _backfill_raw_content(qdrant, chunks, domain_id=domain_id, resolved_config=resolved_config)
 
    yield {"type": "sources", "retrieved_chunks": chunks}
 
    # -------------------------------------------------------------------
    # FASE 3 — geração via streaming
    # -------------------------------------------------------------------
    context = _format_context(chunks, domain_id=domain_id)
    augmented_user_message = _build_augmented_message(request.message, context, domain_id=domain_id)
 
    # MULTI-TENANT: mesma lógica de run_rag_pipeline — resolved_config.system_prompt
    # já contempla override de coleção > default do domínio; ver lá para detalhes.
    system_prompt = (
        resolved_config.system_prompt
        if resolved_config is not None
        else PromptFactory.get_prompts(domain_id).system_prompt
    )

    messages = await build_messages_for_llm(
        redis=redis,
        session_id=request.session_id,
        user_message=augmented_user_message,
        system_prompt=system_prompt,
    )
 
    full_answer_parts: list[str] = []
    async for token in call_litellm_stream(messages=messages, model=request.model, resolved_config=resolved_config):
        full_answer_parts.append(token)
        yield {"type": "token", "content": token}

    full_answer = "".join(full_answer_parts)
    usage = getattr(call_litellm_stream, "last_usage", {}) or {}
    finish_reason = getattr(call_litellm_stream, "last_finish_reason", None)
 
    # -------------------------------------------------------------------
    # PERSISTÊNCIA — mesmo save_turn do caminho síncrono (Redis, contexto
    # multi-turn). A persistência DURÁVEL (Postgres) é feita pelo endpoint
    # depois que o generator termina — ver chat.py::_stream_chat_response.
    # -------------------------------------------------------------------
    await save_turn(
        redis=redis,
        session_id=request.session_id,
        user_message=request.message,
        assistant_reply=full_answer,
        model_used=request.model,
    )
 
    log.info(
        "Pipeline (stream) concluído | session=%s pipeline=%s chunks=%d tokens_in=%s tokens_out=%s",
        request.session_id,
        pipeline_used,
        len(chunks),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )
 
    yield {
        "type": "done",
        "model_used": request.model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "finish_reason": finish_reason,
    }