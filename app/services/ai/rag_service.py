"""
app/services/ai/rag_service.py
================================
Advanced RAG Pipeline — Unreal Engine 5 AI Assistant

Pipeline de duas fases para maximizar cobertura e precisão em perguntas
multi-tópico (ex: "como fazer pulo em C++ E integrar com Animation Blueprint"):

    FASE 1 — QUERY DECOMPOSITION (LLM-Driven)
    ─────────────────────────────────────────
    1.1  decompose_query()        → até 3 sub-queries otimizadas para busca vetorial
         (chamada assíncrona leve ao Gemini 2.0 Flash Lite via LiteLLM)

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
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings
from app.schemas.chat import ChatRequest, ChatResponse, RetrievedChunk
from app.services.ai.embedding_service import embed_text
from app.services.ai.session_service import build_messages_for_llm, save_turn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de configuração do pipeline avançado
# Valores padrão — sobrescritos pelas settings em runtime quando relevante.
# ---------------------------------------------------------------------------

# Limite expandido de chunks por query (referência interna; usa settings em runtime)
_RETRIEVAL_LIMIT_PER_QUERY: int = 15

# Modelo de decomposição padrão (pode ser sobrescrito via settings)
_DECOMPOSITION_MODEL: str = "gemini-3.1-flash-lite"

# Fallback: se o modelo de decomposição principal falhar (HTTP 4xx/5xx,
# timeout ou erro de conexão — ver decompose_query).
# CORREÇÃO (ALTO): este valor estava idêntico a _DECOMPOSITION_MODEL, o que
# tornava o fallback um no-op — uma indisponibilidade real do modelo
# principal falhava da mesma forma nas duas tentativas. Precisa ser um
# modelo genuinamente diferente (outro provider/tier no LiteLLM) para
# fornecer redundância de verdade. Ajuste este valor para o modelo de
# fallback real disponível no seu LiteLLM (ex.: outro provider hospedado).
_DECOMPOSITION_MODEL_FALLBACK: str = "gpt-4o-mini"

# Timeout dedicado para a chamada de decomposição (deve ser rápida)
_DECOMPOSITION_TIMEOUT: float = 12.0

# FlashRank: modelo Cross-Encoder embutido (baixa latência, sem GPU)
# Sobrescrito em runtime por settings.rag_reranker_model
_FLASHRANK_MODEL: str = "ms-marco-MiniLM-L-12-v2"

# ---------------------------------------------------------------------------
# System Prompt principal — especializado em Unreal Engine 5
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Você é um Assistente de IA especializado em Unreal Engine 5, \
com profundo conhecimento de Blueprints, C++, Lumen, Nanite, Niagara, \
Mass Entity, PCG Framework e pipeline de renderização do UE5.

Regras de resposta:
1. Base suas respostas EXCLUSIVAMENTE no contexto de documentação fornecido.
2. Se o contexto não contiver informação suficiente, diga explicitamente: \
"A documentação disponível não cobre esse tópico com detalhes suficientes."
3. Use terminologia técnica correta do UE5 (ex: Actor, Component, Blueprint, \
World Partition, Data Layer).
4. Quando relevante, mencione qual arquivo de documentação originou a informação.
5. Responda em Português Brasileiro a menos que o usuário escreva em outro idioma.
6. Para trechos de código C++ ou Blueprint, use blocos de código formatados.
7. Nunca invente funções, classes ou parâmetros que não estejam na documentação.
8. Cada documento de contexto pode conter duas camadas, claramente rotuladas: \
(A) ANÁLISE SEMÂNTICA — um resumo conceitual gerado a partir do código ou da \
documentação oficial, que explica a INTENÇÃO e o uso prático dentro do \
framework do UE5 — e (B) REFERÊNCIA DIRETA — um bloco de código com o \
conteúdo original e literal (C++ bruto ou Markdown .udn bruto), sem \
resumo ou interpretação. Use a Análise Semântica para entender COMO e \
POR QUE algo é usado, mas trate a Referência Direta como a fonte \
soberana e absoluta sempre que houver qualquer divergência sobre \
assinaturas exatas de função, tipos de dado, nomes de propriedades \
(UPROPERTY/UFUNCTION) ou parâmetros — nunca corrija ou substitua o que \
está na Referência Direta com base na Análise Semântica.\
"""

# ---------------------------------------------------------------------------
# System Prompt de decomposição — instrui o modelo a ser um decompositor puro
# ---------------------------------------------------------------------------

_DECOMPOSITION_SYSTEM_PROMPT = """\
Você é um sistema especializado em tradução e decomposição de queries para \
busca vetorial em documentação técnica da Unreal Engine 5.

A base vetorial (Qdrant) é 100% em INGLÊS TÉCNICO — tanto os headers C++ \
documentados quanto a documentação oficial .udn da Epic. Se qualquer query \
usada para gerar embedding chegar em português, ela sofre de mismatch \
cross-lingual contra o corpus e recupera chunks ruins ou nenhum chunk. \
Por isso NENHUMA query em português pode ser usada para embedding — \
inclusive a "query 0" (tradução literal) abaixo.

Sua função tem DUAS partes:
1. TRADUÇÃO LITERAL (query 0): traduza a pergunta do usuário para inglês \
técnico da forma mais fiel possível, preservando a intenção original sem \
reformular ou expandir. Esta é a âncora usada como substituta da query \
original nas buscas.
2. SUB-QUERIES TÉCNICAS (1 a 3 adicionais): decomponha em sub-queries \
técnicas e concisas, cada uma otimizada para recuperar um conjunto \
específico de chunks via similaridade semântica.

REGRAS ESTRITAS:
1. "translated_query" é OBRIGATÓRIA em todo resultado — é a tradução \
literal em inglês da pergunta do usuário, nunca vazia.
2. Gere no MÁXIMO 3 "sub_queries" técnicas. Se a pergunta já for simples e \
focada, gere apenas 1 ou 2 (pode ser até 0 se a tradução literal já cobrir \
tudo).
3. Cada sub-query deve ser em inglês técnico, curta (5-12 palavras) e \
focada em um único conceito ou tarefa da UE5.
4. Use terminologia UE5 precisa: ACharacter, UMovementComponent, AnimBlueprint, \
AnimInstance, BlueprintCallable, UFUNCTION, etc.
5. NÃO inclua explicações, numeração ou texto extra. Retorne APENAS JSON válido.
6. Formato de saída OBRIGATÓRIO (JSON puro, sem markdown, sem texto extra):
{"translated_query": "tradução literal em inglês", "sub_queries": ["query técnica 1", "query técnica 2"]}

EXEMPLOS:

Pergunta: "Como programar o pulo em C++ e configurar a animação de salto?"
Saída: {"translated_query": "How to program jumping in C++ and set up the jump animation?", "sub_queries": ["ACharacter Jump input binding C++ AddMovementInput", "UE5 Animation Blueprint state machine jump transition", "AnimNotify jump landing C++ Blueprint integration"]}

Pergunta: "O que é o Lumen e como ativar GI global no UE5?"
Saída: {"translated_query": "What is Lumen and how to enable global GI in UE5?", "sub_queries": ["Lumen Global Illumination system UE5 architecture", "enable dynamic global illumination Lumen project settings"]}

Pergunta: "Como usar Niagara para efeitos de partícula de fogo?"
Saída: {"translated_query": "How to use Niagara for fire particle effects?", "sub_queries": ["Niagara particle system fire emitter UE5 setup"]}
"""


# ---------------------------------------------------------------------------
# FASE 1 — Query Decomposition
# ---------------------------------------------------------------------------

async def decompose_query(user_question: str) -> list[str]:
    """
    Traduz a query do usuário para inglês técnico e a decompõe em sub-queries
    técnicas para busca vetorial.

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

    Args:
        user_question: Pergunta original do usuário.

    Returns:
        Lista de queries em inglês para busca vetorial: [tradução_literal,
        *sub_queries_técnicas], sem duplicatas. Só contém texto em português
        no caso extremo de falha total de tradução (ver NOTA acima).
    """
    # Lê configurações em runtime para permitir override sem recompilação
    decomp_model = getattr(settings, "rag_decomposition_model", _DECOMPOSITION_MODEL)
    decomp_timeout = getattr(settings, "rag_decomposition_timeout", _DECOMPOSITION_TIMEOUT)

    url = f"{settings.litellm_base_url}/chat/completions"
    payload = {
        "model": decomp_model,
        "messages": [
            {"role": "system", "content": _DECOMPOSITION_SYSTEM_PROMPT},
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
    async def _call_decomposition_model(model_name: str) -> str:
        request_payload = {**payload, "model": model_name}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=decomp_timeout,
                write=5.0,
                pool=5.0,
            )
        ) as client:
            resp = await client.post(
                url,
                json=request_payload,
                headers=settings.litellm_headers,
            )
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
            return [user_question]

    # --- Parse robusto da resposta JSON ---
    translated_query, sub_queries = _parse_decomposition_response(raw_content, user_question)

    # A tradução literal em inglês é a âncora da busca — NUNCA a query
    # original em português (ver docstring desta função para o porquê).
    all_queries: list[str] = [translated_query]
    for sq in sub_queries:
        sq_stripped = sq.strip()
        # Evita duplicar a tradução se o modelo a devolveu idêntica
        if sq_stripped and sq_stripped.lower() != translated_query.lower():
            all_queries.append(sq_stripped)

    log.info(
        "Query traduzida e decomposta em %d queries | tradução + %d sub-queries",
        len(all_queries),
        len(all_queries) - 1,
    )
    for i, q in enumerate(all_queries):
        log.debug("  Query[%d]: %s", i, q)

    return all_queries


def _parse_decomposition_response(
    raw: str, fallback_query: str
) -> tuple[str, list[str]]:
    """
    Parser ultra-robusto para a saída do modelo de decomposição/tradução.

    Estratégias (em ordem de preferência), aplicadas ao dict parseado:
    1. JSON direto (caminho feliz — json_object mode do LiteLLM)
    2. Extração de bloco JSON via regex (se o modelo envolveu em markdown)
    3. Extração de JSON embutido sem markdown
    4. Extração de lista simples via regex de aspas (sem 'translated_query'
       confiável — usa a primeira string extraída como tradução aproximada)
    5. Fallback total: 'fallback_query' (PT-BR) como tradução — só ocorre se
       TODAS as estratégias de parse falharem, e é logado como erro alto,
       nunca como comportamento silencioso esperado.

    Args:
        raw: String bruta retornada pelo LLM.
        fallback_query: Query original do usuário, usada em ÚLTIMO CASO se
            nenhuma tradução puder ser extraída da resposta do modelo.

    Returns:
        Tupla (translated_query, sub_queries). translated_query nunca é
        vazia — cai para fallback_query apenas se o parse falhar por completo.
    """
    raw = raw.strip()
    if not raw:
        log.warning(
            "Decomposição: resposta vazia do LLM. Usando query original como "
            "tradução — sem tradução real disponível para esta requisição."
        )
        return fallback_query, []

    def _extract(parsed: dict[str, Any]) -> tuple[str, list[str]] | None:
        translated = parsed.get("translated_query")
        queries = parsed.get("sub_queries", [])
        if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
            queries = []
        clean_queries = [q for q in queries if isinstance(q, str) and q.strip()]

        if isinstance(translated, str) and translated.strip():
            return translated.strip(), clean_queries

        # Campo 'translated_query' ausente/inválido: modelo pode ter usado
        # formato antigo (só sub_queries). Reaproveita a primeira sub-query
        # como âncora de tradução aproximada em vez de cair para PT-BR.
        if clean_queries:
            log.warning(
                "Decomposição: 'translated_query' ausente na resposta do "
                "modelo. Reaproveitando primeira sub-query como tradução "
                "aproximada."
            )
            return clean_queries[0], clean_queries[1:]

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
        return quoted_queries[0], quoted_queries[1:]

    log.error(
        "Decomposição: impossível parsear resposta do LLM em nenhum formato. "
        "Usando query original em PT-BR como último recurso — mismatch "
        "cross-lingual esperado para esta requisição. raw=%r",
        raw[:200],
    )
    return fallback_query, []


# ---------------------------------------------------------------------------
# FASE 2.1 — Busca Expandida no Qdrant
# ---------------------------------------------------------------------------

async def retrieve_chunks_for_query(
    qdrant: AsyncQdrantClient,
    query_vector: list[float],
    limit: int,
) -> list[RetrievedChunk]:
    """
    Executa uma única busca vetorial no Qdrant com limite expandido.

    Não aplica score_threshold aqui — o reranker vai cuidar da qualidade.
    Threshold é aplicado apenas se o reranker não estiver disponível.

    Args:
        qdrant: Cliente Qdrant assíncrono.
        query_vector: Vetor de embedding da query.
        limit: Número máximo de resultados a recuperar.

    Returns:
        Lista de RetrievedChunk, possivelmente vazia se Qdrant falhar.
    """
    try:
        results = await qdrant.search(
            collection_name=settings.qdrant_collection,
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
            # Sem score_threshold aqui — deixamos o reranker filtrar a qualidade
        )
        log.debug("Recebidos %d resultados do Qdrant.", len(results))
        if results:
            log.debug("Payload de exemplo (1º resultado): %s", results[0].payload)
    except Exception as exc:
        log.error("Falha na busca vetorial (Qdrant): %s", exc)
        return []

    chunks: list[RetrievedChunk] = []
    for hit in results:
        payload = hit.payload or {}
        try:
            chunks.append(
                RetrievedChunk(
                    score=hit.score,
                    text=payload.get("text", ""),
                    title=payload.get("title", "Sem título"),
                    description=payload.get("description", ""),
                    source_file=payload.get("source_file", ""),
                    chunk_id=payload.get("chunk_id", 0),
                    ue_version=payload.get("ue_version", ""),
                    doc_type=payload.get("doc_type", ""),
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
# Singleton do Ranker (FlashRank)
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
# intervalo. Agora o Ranker é construído uma única vez (fora do event loop,
# via to_thread) e reaproveitado; a inferência também roda em thread separada.
_ranker_instance: Any = None
_ranker_init_lock = asyncio.Lock()


async def _get_ranker() -> Any:
    """Retorna o Ranker cacheado, construindo-o (em thread) na 1ª chamada."""
    global _ranker_instance
    if _ranker_instance is not None:
        return _ranker_instance

    async with _ranker_init_lock:
        # Re-checa dentro do lock: outra corrotina pode ter inicializado
        # enquanto esperávamos o lock.
        if _ranker_instance is None:
            try:
                from flashrank import Ranker  # importação lazy — graceful degradation
            except ImportError:
                log.warning(
                    "FlashRank não está instalado. Instale com: pip install flashrank. "
                    "Usando ordem original do Qdrant como fallback."
                )
                return None

            reranker_model = getattr(settings, "rag_reranker_model", _FLASHRANK_MODEL)
            cache_dir = getattr(settings, "rag_reranker_cache_dir", "/tmp/flashrank_cache")
            try:
                _ranker_instance = await asyncio.to_thread(
                    Ranker, model_name=reranker_model, cache_dir=cache_dir
                )
                log.info(
                    "FlashRank Ranker inicializado e cacheado | model=%s",
                    reranker_model,
                )
            except Exception as exc:
                log.warning(
                    "Falha ao inicializar o FlashRank Ranker: %s. "
                    "Reranking ficará desabilitado (fallback: ordem original).",
                    exc,
                )
                return None

    return _ranker_instance


async def rerank_with_flashrank(
    chunks: list[RetrievedChunk],
    user_question: str,
) -> list[RetrievedChunk]:
    """
    Reordena os chunks usando um Cross-Encoder leve via FlashRank.

    FlashRank usa modelos ONNX otimizados que rodam em CPU em milissegundos,
    sem necessidade de GPU, sem chamada de API externa. Compara o texto de
    cada chunk diretamente contra a PERGUNTA ORIGINAL (não sub-queries)
    para maximizar relevância final.

    O Ranker é cacheado como singleton (ver _get_ranker) e a inferência roda
    em thread separada (asyncio.to_thread) para nunca bloquear o event loop.

    Em caso de falha (biblioteca não instalada, modelo não disponível, etc.),
    retorna os chunks na ordem original do Qdrant como fallback.

    Args:
        chunks: Lista deduplicada de chunks para reranking.
        user_question: Pergunta original do usuário (âncora do reranking).

    Returns:
        Lista reordenada pelo score do Cross-Encoder (mais relevante primeiro).
    """
    if not chunks:
        return chunks

    ranker = await _get_ranker()
    if ranker is None:
        return chunks

    try:
        from flashrank import RerankRequest

        # Formata os passages no formato esperado pelo FlashRank
        passages = [
            {
                "id": f"{chunk.source_file}|{chunk.chunk_id}",
                "text": chunk.text,
                "meta": {
                    "source_file": chunk.source_file,
                    "chunk_id": chunk.chunk_id,
                    "original_score": chunk.score,
                    "title": chunk.title,
                },
            }
            for chunk in chunks
        ]

        rerank_request = RerankRequest(query=user_question, passages=passages)
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
) -> list[RetrievedChunk]:
    """
    Busca naive original: busca direta no Qdrant com top_k fixo e score_threshold.
    Mantida como fallback para o caso do pipeline avançado falhar completamente.

    Args:
        qdrant: Cliente Qdrant assíncrono.
        query_vector: Vetor de embedding da query original.
        top_k: Número de resultados desejados.

    Returns:
        Lista de RetrievedChunk.

    Raises:
        RuntimeError: se a busca no Qdrant falhar.
    """
    try:
        results = await qdrant.search(
            collection_name=settings.qdrant_collection,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
            score_threshold=settings.rag_score_threshold,
        )
        log.debug("Recebidos %d resultados do Qdrant.", len(results))
        if results:
            log.debug("Payload de exemplo (1º resultado): %s", results[0].payload)
    except Exception as exc:
        log.error("Erro ao buscar no Qdrant (fallback naive): %s", exc)
        raise RuntimeError(f"Falha na busca vetorial: {exc}") from exc

    chunks: list[RetrievedChunk] = []
    for hit in results:
        payload = hit.payload or {}
        try:
            chunks.append(
                RetrievedChunk(
                    score=hit.score,
                    text=payload.get("text", ""),
                    title=payload.get("title", "Sem título"),
                    description=payload.get("description", ""),
                    source_file=payload.get("source_file", ""),
                    chunk_id=payload.get("chunk_id", 0),
                    ue_version=payload.get("ue_version", ""),
                    doc_type=payload.get("doc_type", ""),
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
        settings.rag_score_threshold,
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
    qdrant: AsyncQdrantClient, chunks: list[RetrievedChunk]
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
            collection_name=settings.qdrant_collection,
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


def _format_context(chunks: list) -> str:
    """
    Formata os chunks recuperados em um bloco de contexto legível para o LLM.

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
        text = _get_chunk_field(chunk, "text", "")
        title = _get_chunk_field(chunk, "title", "Sem título")
        source_file = _get_chunk_field(chunk, "source_file", "")
        score = _get_chunk_field(chunk, "score", 0.0)
        doc_type = _get_chunk_field(chunk, "doc_type", "")
        raw_content = (_get_chunk_field(chunk, "raw_content", "") or "").strip()

        source = source_file.split("/")[-1] if "/" in source_file else source_file

        block = (
            f"--- DOCUMENTO {i} ---\n"
            f"Título: {title}\n"
            f"Arquivo: {source}\n"
            f"Score de relevância: {score:.4f}\n\n"
            f"[A) ANÁLISE SEMÂNTICA]\n"
            f"{text}"
        )

        if raw_content:
            # Canal A ("C++ Source API") usa fence ```cpp; qualquer outro
            # doc_type (Canal B / documentação oficial .udn) usa ```markdown.
            fence_lang = "cpp" if doc_type == "C++ Source API" else "markdown"
            block += (
                f"\n\n[B) REFERÊNCIA DIRETA — verdade absoluta para sintaxe, "
                f"assinaturas e tipos]\n"
                f"```{fence_lang}\n{raw_content}\n```"
            )

        parts.append(block)

    return "\n\n".join(parts)


def _build_augmented_message(user_question: str, context: str) -> str:
    """
    Monta a mensagem do usuário com o contexto RAG injetado.
    O contexto é apresentado antes da pergunta para maximizar a atenção do LLM.
    """
    return (
        f"## Contexto da Documentação UE5\n\n"
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


@retry(
    retry=retry_if_exception(_is_retryable_litellm_error),
    wait=wait_exponential_jitter(initial=2, max=30),
    stop=stop_after_attempt(settings.litellm_max_retries),
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
) -> tuple[str, int | None, int | None]:
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
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,       # baixo para respostas factuais/técnicas
        "max_tokens": 2048,
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

    usage = data.get("usage", {})
    return answer, usage.get("prompt_tokens"), usage.get("completion_tokens")


# ---------------------------------------------------------------------------
# Pipeline avançado — núcleo do Advanced RAG
# ---------------------------------------------------------------------------

async def _run_advanced_retrieval(
    user_question: str,
    qdrant: AsyncQdrantClient,
    top_k: int,
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

    Args:
        user_question: Pergunta original do usuário.
        qdrant: Cliente Qdrant assíncrono.
        top_k: Número final de chunks desejados após reranking.

    Returns:
        Lista de RetrievedChunk reranqueados, limitada ao top_k.
        Pode estar vazia se o Qdrant não retornar resultados.
    """
    # Lê o limite de busca expandida das settings (override sem recompilação)
    retrieval_limit = getattr(
        settings, "rag_retrieval_limit_per_query", _RETRIEVAL_LIMIT_PER_QUERY
    )

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

    all_queries = await decompose_query(user_question)
    translated_query = all_queries[0]
    sub_queries = all_queries[1:]

    log.info(
        "Buscando no Qdrant: 1 query traduzida + %d sub-queries em paralelo...",
        len(sub_queries),
    )

    # Todas as queries (tradução + sub-queries) são embedadas e buscadas em
    # paralelo entre si — nenhuma delas está em português neste ponto, exceto
    # no caso extremo de falha total de tradução (ver decompose_query).
    retrieval_tasks: list[asyncio.Task] = [
        asyncio.create_task(
            _embed_and_retrieve(translated_query, qdrant, retrieval_limit)
        )
    ]

    for sub_query in sub_queries:
        retrieval_tasks.append(
            asyncio.create_task(
                _embed_and_retrieve(sub_query, qdrant, retrieval_limit)
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
    reranked = await rerank_with_flashrank(unique_chunks, user_question)

    # === FASE 2.4 — Seleção Final top-k ===
    final_chunks = reranked[:top_k]

    log.info(
        "Advanced RAG: %d chunks selecionados após reranking (top_k=%d)",
        len(final_chunks),
        top_k,
    )

    return final_chunks


async def _embed_and_retrieve(
    query: str,
    qdrant: AsyncQdrantClient,
    limit: int,
) -> list[RetrievedChunk]:
    """
    Função helper: gera embedding de uma query e busca no Qdrant.
    Encadeia embed_text() e retrieve_chunks_for_query() de forma segura.

    Em caso de erro no embedding ou na busca, retorna lista vazia
    (não propaga a exceção — falhas individuais de sub-queries não devem
    derrubar o pipeline inteiro).

    Args:
        query: Texto da sub-query.
        qdrant: Cliente Qdrant assíncrono.
        limit: Número máximo de chunks a recuperar.

    Returns:
        Lista de RetrievedChunk, ou lista vazia em caso de falha.
    """
    try:
        vector = await embed_text(query)
        return await retrieve_chunks_for_query(qdrant, vector, limit)
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

    Args:
        request: Dados validados da requisição (ChatRequest).
        qdrant: Cliente Qdrant injetado via Depends.
        redis: Cliente Redis injetado via Depends.

    Returns:
        ChatResponse com resposta, chunks usados e tokens consumidos.
    """
    log.info(
        "Advanced RAG pipeline | session=%s model=%s top_k=%d",
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
    effective_top_k = max(request.top_k, 8)

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
            chunks = await retrieve_chunks(qdrant, original_vector, effective_top_k)
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
        chunks = await _backfill_raw_content(qdrant, chunks)

    # =========================================================================
    # AUGMENTAÇÃO + GERAÇÃO
    # =========================================================================

    context = _format_context(chunks)
    augmented_user_message = _build_augmented_message(request.message, context)

    messages = await build_messages_for_llm(
        redis=redis,
        session_id=request.session_id,
        user_message=augmented_user_message,
        system_prompt=_SYSTEM_PROMPT,
    )

    try:
        answer, prompt_tokens, completion_tokens = await call_litellm(
            messages=messages,
            model=request.model,
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
    )