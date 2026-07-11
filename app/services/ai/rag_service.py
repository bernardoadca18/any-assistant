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

# Fallback: se o modelo de decomposição principal retornar erro 4xx/5xx
_DECOMPOSITION_MODEL_FALLBACK: str = "gemini-3.1-flash-lite"

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
Você é um sistema especializado em decomposição de queries para busca vetorial \
em documentação técnica da Unreal Engine 5.

Sua única função é analisar a pergunta do usuário e quebrá-la em sub-queries \
técnicas e concisas, cada uma otimizada para recuperar um conjunto específico \
de chunks da documentação UE5 via similaridade semântica.

REGRAS ESTRITAS:
1. Gere no MÁXIMO 3 sub-queries. Se a pergunta já for simples e focada, \
gere apenas 1 ou 2.
2. Cada sub-query deve ser em inglês técnico, curta (5-12 palavras) e \
focada em um único conceito ou tarefa da UE5.
3. Use terminologia UE5 precisa: ACharacter, UMovementComponent, AnimBlueprint, \
AnimInstance, BlueprintCallable, UFUNCTION, etc.
4. NÃO inclua explicações, numeração ou texto extra. Retorne APENAS JSON válido.
5. Formato de saída OBRIGATÓRIO (JSON puro, sem markdown, sem texto extra):
{"sub_queries": ["query técnica 1", "query técnica 2", "query técnica 3"]}

EXEMPLOS:

Pergunta: "Como programar o pulo em C++ e configurar a animação de salto?"
Saída: {"sub_queries": ["ACharacter Jump input binding C++ AddMovementInput", "UE5 Animation Blueprint state machine jump transition", "AnimNotify jump landing C++ Blueprint integration"]}

Pergunta: "O que é o Lumen e como ativar GI global no UE5?"
Saída: {"sub_queries": ["Lumen Global Illumination system UE5 architecture", "enable dynamic global illumination Lumen project settings"]}

Pergunta: "Como usar Niagara para efeitos de partícula de fogo?"
Saída: {"sub_queries": ["Niagara particle system fire emitter UE5 setup"]}
"""


# ---------------------------------------------------------------------------
# FASE 1 — Query Decomposition
# ---------------------------------------------------------------------------

async def decompose_query(user_question: str) -> list[str]:
    """
    Decompõe a query do usuário em sub-queries técnicas para busca vetorial.

    Faz uma chamada assíncrona leve ao LLM de decomposição e parseia
    o JSON de saída. Em caso de qualquer falha, retorna apenas a query
    original (fallback seguro).

    Args:
        user_question: Pergunta original do usuário.

    Returns:
        Lista contendo a query original + sub-queries geradas (sem duplicatas).
        Garante que a query original SEMPRE está na lista resultante.
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

    try:
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
                json=payload,
                headers=settings.litellm_headers,
            )
            resp.raise_for_status()

        data = resp.json()
        raw_content: str = data["choices"][0]["message"]["content"]

    except httpx.HTTPStatusError as exc:
        # Tenta modelo de fallback se o principal retornar 4xx/5xx
        log.warning(
            "Decomposição: modelo '%s' retornou HTTP %d. Tentando fallback '%s'.",
            decomp_model,
            exc.response.status_code,
            _DECOMPOSITION_MODEL_FALLBACK,
        )
        payload["model"] = _DECOMPOSITION_MODEL_FALLBACK
        try:
            async with httpx.AsyncClient(timeout=decomp_timeout) as client:
                resp = await client.post(url, json=payload, headers=settings.litellm_headers)
                resp.raise_for_status()
            data = resp.json()
            raw_content = data["choices"][0]["message"]["content"]
        except Exception as fallback_exc:
            log.warning(
                "Decomposição falhou no modelo de fallback também: %s. "
                "Usando query original.",
                fallback_exc,
            )
            return [user_question]

    except Exception as exc:
        log.warning(
            "Decomposição falhou com erro inesperado: %s. Usando query original.",
            exc,
        )
        return [user_question]

    # --- Parse robusto da resposta JSON ---
    sub_queries = _parse_decomposition_response(raw_content, user_question)

    # A query original SEMPRE deve estar na lista para garantir cobertura máxima
    all_queries: list[str] = [user_question]
    for sq in sub_queries:
        sq_stripped = sq.strip()
        # Evita duplicar a query original se o modelo a devolveu idêntica
        if sq_stripped and sq_stripped.lower() != user_question.lower():
            all_queries.append(sq_stripped)

    log.info(
        "Query decomposta em %d queries | original + %d sub-queries",
        len(all_queries),
        len(all_queries) - 1,
    )
    for i, q in enumerate(all_queries):
        log.debug("  Query[%d]: %s", i, q)

    return all_queries


def _parse_decomposition_response(raw: str, fallback_query: str) -> list[str]:
    """
    Parser ultra-robusto para a saída do modelo de decomposição.

    Estratégias (em ordem de preferência):
    1. JSON direto (caminho feliz — json_object mode do LiteLLM)
    2. Extração de bloco JSON via regex (se o modelo envolveu em markdown)
    3. Extração de lista simples via regex linha por linha
    4. Fallback para lista vazia (query original já foi adicionada pelo chamador)

    Args:
        raw: String bruta retornada pelo LLM.
        fallback_query: Query original para logging.

    Returns:
        Lista de sub-queries extraídas, ou lista vazia se o parse falhar.
    """
    raw = raw.strip()
    if not raw:
        log.warning("Decomposição: resposta vazia do LLM.")
        return []

    # --- Estratégia 1: JSON direto ---
    try:
        parsed: dict[str, Any] = json.loads(raw)
        queries = parsed.get("sub_queries", [])
        if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
            return [q for q in queries if q.strip()]
    except json.JSONDecodeError:
        pass

    # --- Estratégia 2: Bloco JSON em markdown (```json ... ```) ---
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            queries = parsed.get("sub_queries", [])
            if isinstance(queries, list):
                return [q for q in queries if isinstance(q, str) and q.strip()]
        except json.JSONDecodeError:
            pass

    # --- Estratégia 3: JSON embutido sem markdown ---
    json_match2 = re.search(r"\{.*?\}", raw, re.DOTALL)
    if json_match2:
        try:
            parsed = json.loads(json_match2.group(0))
            queries = parsed.get("sub_queries", [])
            if isinstance(queries, list):
                return [q for q in queries if isinstance(q, str) and q.strip()]
        except json.JSONDecodeError:
            pass

    # --- Estratégia 4: Lista de strings entre aspas ---
    quoted_queries = re.findall(r'"([^"]{10,200})"', raw)
    if quoted_queries:
        log.warning(
            "Decomposição: JSON inválido, extraindo via regex de aspas. raw=%r",
            raw[:120],
        )
        return [q for q in quoted_queries if q.strip()]

    log.warning(
        "Decomposição: impossível parsear resposta do LLM. Usando query original. raw=%r",
        raw[:200],
    )
    return []


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

def rerank_with_flashrank(
    chunks: list[RetrievedChunk],
    user_question: str,
) -> list[RetrievedChunk]:
    """
    Reordena os chunks usando um Cross-Encoder leve via FlashRank.

    FlashRank usa modelos ONNX otimizados que rodam em CPU em milissegundos,
    sem necessidade de GPU, sem chamada de API externa. Compara o texto de
    cada chunk diretamente contra a PERGUNTA ORIGINAL (não sub-queries)
    para maximizar relevância final.

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

    try:
        from flashrank import Ranker, RerankRequest  # importação lazy — graceful degradation
    except ImportError:
        log.warning(
            "FlashRank não está instalado. Instale com: pip install flashrank. "
            "Usando ordem original do Qdrant como fallback."
        )
        return chunks

    try:
        # Lê modelo e cache dir das settings (permitem override sem recompilação)
        reranker_model = getattr(settings, "rag_reranker_model", _FLASHRANK_MODEL)
        cache_dir = getattr(settings, "rag_reranker_cache_dir", "/tmp/flashrank_cache")

        # Inicialização do ranker (carrega modelo ONNX na primeira chamada)
        # FlashRank faz cache interno — chamadas subsequentes são instantâneas
        ranker = Ranker(model_name=reranker_model, cache_dir=cache_dir)

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
        reranked_results = ranker.rerank(rerank_request)

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
    Define quais falhas justificam retry com backoff exponencial: timeouts e
    erros HTTP tipicamente transitórios (rate limit e 5xx). Erros 4xx que não
    sejam 429 (payload malformado, modelo inexistente, etc.) NÃO são
    retentados — insistir não resolve um erro de requisição malformada, só
    atrasa a falha e desperdiça tentativas.
    """
    if isinstance(exc, httpx.TimeoutException):
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

    # === PARALELISMO FASE 1 ===
    # Dispara decomposição e embedding da query original simultaneamente.
    # Ganho típico: ~8-12 segundos de latência eliminados.
    log.info("Advanced RAG: iniciando decomposição e embedding em paralelo...")

    decompose_task = asyncio.create_task(decompose_query(user_question))
    embed_original_task = asyncio.create_task(embed_text(user_question))

    all_queries, original_vector = await asyncio.gather(
        decompose_task,
        embed_original_task,
    )

    # === PARALELISMO FASE 2 ===
    # Gera embeddings das sub-queries e faz as buscas, tudo em paralelo.
    # Para a query original, reutilizamos o vetor já calculado.

    # Separa a query original das sub-queries para evitar re-embeddamento
    sub_queries = [q for q in all_queries if q != user_question]

    log.info(
        "Buscando no Qdrant: 1 query original + %d sub-queries em paralelo...",
        len(sub_queries),
    )

    # Monta todas as tasks de retrieval
    retrieval_tasks: list[asyncio.Task] = [
        # Query original já tem seu vetor calculado
        asyncio.create_task(
            retrieve_chunks_for_query(qdrant, original_vector, retrieval_limit)
        )
    ]

    # Sub-queries: embed + retrieve encadeados
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

    # === FASE 2.3 — Reranking (síncrono, CPU-bound) ===
    # FlashRank é síncrono e muito rápido (~10-50ms para dezenas de chunks)
    # Executar em thread pool não é necessário nesta escala
    reranked = rerank_with_flashrank(unique_chunks, user_question)

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
    # para o Advanced RAG ter valor (8 garante contexto rico sem overflow de tokens)
    effective_top_k = max(request.top_k, min(request.top_k, 8))

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