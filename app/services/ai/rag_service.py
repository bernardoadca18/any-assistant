"""
app/services/ai/rag_service.py
================================
Orquestra o pipeline RAG completo:
    1. embed_text()        → vetor da query do usuário
    2. qdrant.search()     → top-k chunks da documentação UE5
    3. build_prompt()      → prompt aumentado com contexto
    4. litellm_chat()      → resposta do LLM via LiteLLM proxy
    5. session.save_turn() → persiste turno no Redis

O system prompt é especializado em UE5 para garantir que o modelo
responda com o vocabulário e estilo de um desenvolvedor sênior de UE5,
sem alucinar sobre funcionalidades que não existem na documentação.
"""

from __future__ import annotations

import logging

import httpx
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from app.core.config import settings
from app.schemas.chat import ChatRequest, ChatResponse, RetrievedChunk
from app.services.ai.embedding_service import embed_text
from app.services.cache.session_service import build_messages_for_llm, save_turn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt — especializado em Unreal Engine 5
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Você é um Assistente de IA especializado em Unreal Engine 5, \
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
7. Nunca invente funções, classes ou parâmetros que não estejam na documentação."""


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------

def _format_context(chunks: list[RetrievedChunk]) -> str:
    """
    Formata os chunks recuperados em um bloco de contexto legível para o LLM.
    Cada chunk inclui título, arquivo de origem e o texto, separados por delimitadores.
    """
    if not chunks:
        return "Nenhum contexto de documentação relevante encontrado."

    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.source_file.split("/")[-1] if "/" in chunk.source_file else chunk.source_file
        parts.append(
            f"--- DOCUMENTO {i} ---\n"
            f"Título: {chunk.title}\n"
            f"Arquivo: {source}\n"
            f"Score de relevância: {chunk.score:.3f}\n\n"
            f"{chunk.text}"
        )
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
# Busca no Qdrant
# ---------------------------------------------------------------------------

async def retrieve_chunks(
    qdrant: AsyncQdrantClient,
    query_vector: list[float],
    top_k: int,
) -> list[RetrievedChunk]:
    """
    Busca os top-k chunks mais similares no Qdrant.

    Aplica score_threshold para descartar resultados com baixa relevância.
    Retorna lista vazia se nenhum resultado ultrapassar o threshold.
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
        log.error("Erro ao buscar no Qdrant: %s", exc)
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
                )
            )
        except Exception as exc:
            log.warning("Chunk ignorado por payload inválido: %s | %s", payload, exc)

    log.debug(
        "Retrieval concluído: %d/%d chunks acima do threshold (%.2f)",
        len(chunks),
        top_k,
        settings.rag_score_threshold,
    )
    return chunks


# ---------------------------------------------------------------------------
# Chamada ao LiteLLM
# ---------------------------------------------------------------------------

async def call_litellm(
    messages: list[dict],
    model: str,
) -> tuple[str, int | None, int | None]:
    """
    Chama o LiteLLM proxy com o histórico de mensagens e retorna a resposta.

    Returns:
        Tupla (resposta_texto, prompt_tokens, completion_tokens).
        Os tokens podem ser None se o modelo não reportar usage.

    Raises:
        RuntimeError: em caso de erro HTTP ou resposta malformada.
    """
    url = f"{settings.litellm_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,       # baixo para respostas factuais/técnicas
        "max_tokens": 2048,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)) as client:
        try:
            resp = await client.post(url, json=payload, headers=settings.litellm_headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"LiteLLM retornou HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Timeout ao chamar LiteLLM (model={model}). "
                "Verifique se o Ollama tem o modelo carregado."
            ) from exc

    data = resp.json()

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(
            f"Resposta do LiteLLM em formato inesperado: {data}"
        ) from exc

    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")

    return answer, prompt_tokens, completion_tokens


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

async def run_rag_pipeline(
    request: ChatRequest,
    qdrant: AsyncQdrantClient,
    redis: Redis,
) -> ChatResponse:
    """
    Executa o pipeline RAG completo para uma requisição de chat.

    Fluxo:
        1. Gera embedding da pergunta do usuário
        2. Recupera chunks relevantes do Qdrant
        3. Monta mensagens com contexto + histórico
        4. Chama o LLM via LiteLLM
        5. Persiste o turno no Redis
        6. Retorna ChatResponse estruturado

    Args:
        request: Dados validados da requisição (ChatRequest).
        qdrant: Cliente Qdrant injetado via Depends.
        redis: Cliente Redis injetado via Depends.

    Returns:
        ChatResponse com resposta, chunks usados e tokens consumidos.
    """
    log.info(
        "RAG pipeline | session=%s model=%s top_k=%d",
        request.session_id,
        request.model,
        request.top_k,
    )

    # --- 1. Embedding da query -----------------------------------------------
    try:
        query_vector = await embed_text(request.message)
    except RuntimeError as exc:
        log.error("Falha ao gerar embedding: %s", exc)
        raise

    # --- 2. Retrieval ---------------------------------------------------------
    chunks = await retrieve_chunks(qdrant, query_vector, request.top_k)

    if not chunks:
        log.warning(
            "Nenhum chunk recuperado acima do threshold para session=%s. "
            "Respondendo sem contexto de documentação.",
            request.session_id,
        )

    # --- 3. Augmentation ------------------------------------------------------
    context = _format_context(chunks)
    augmented_user_message = _build_augmented_message(request.message, context)

    messages = await build_messages_for_llm(
        redis=redis,
        session_id=request.session_id,
        user_message=augmented_user_message,
        system_prompt=_SYSTEM_PROMPT,
    )

    # --- 4. Generation --------------------------------------------------------
    try:
        answer, prompt_tokens, completion_tokens = await call_litellm(
            messages=messages,
            model=request.model,
        )
    except RuntimeError as exc:
        log.error("Falha ao chamar LiteLLM: %s", exc)
        raise

    # --- 5. Persistência do turno ---------------------------------------------
    await save_turn(
        redis=redis,
        session_id=request.session_id,
        user_message=request.message,   # salva a mensagem original, não a aumentada
        assistant_reply=answer,
        model_used=request.model,
    )

    log.info(
        "RAG concluído | session=%s chunks=%d tokens_in=%s tokens_out=%s",
        request.session_id,
        len(chunks),
        prompt_tokens,
        completion_tokens,
    )

    # --- 6. Response ----------------------------------------------------------
    return ChatResponse(
        session_id=request.session_id,
        answer=answer,
        model_used=request.model,
        retrieved_chunks=chunks,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )