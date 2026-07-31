"""
app/services/ai/session_service.py
===================================
Wrapper para funções de gerenciamento de sessão usadas pelo RAG pipeline.

Este módulo serve como adaptador entre o rag_service.py e o
app/services/session_service.py (repositório principal), fornecendo
as funções esperadas pelo pipeline RAG:
- build_messages_for_llm: monta lista de mensagens para o LLM
- save_turn: persiste turno no Redis

A separação existe para evitar imports circulares e manter a
responsabilidade de persistência claramente isolada.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings

log = logging.getLogger(__name__)

# TTL do histórico no Redis (6 horas)
REDIS_HISTORY_TTL_SECONDS = 60 * 60 * 6
# Máximo de mensagens no contexto multi-turn
REDIS_HISTORY_MAX_MESSAGES = 20


def _session_history_key(session_id: str) -> str:
    """Chave Redis para o histórico de mensagens de uma sessão."""
    return f"session:{session_id}:history"


async def get_history(redis: Redis, session_id: str) -> list[dict[str, str]]:
    """
    Recupera o histórico de mensagens do Redis.

    Args:
        redis: Cliente Redis assíncrono.
        session_id: ID da sessão.

    Returns:
        Lista de mensagens no formato [{"role": "...", "content": "..."}].
    """
    key = _session_history_key(session_id)
    cached = await redis.get(key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            log.warning("Histórico Redis corrompido para sessão %s, iniciando vazio.", session_id)
    return []


async def build_messages_for_llm(
    redis: Redis,
    session_id: str,
    user_message: str,
    system_prompt: str,
) -> list[dict[str, str]]:
    """
    Monta a lista de mensagens para enviar ao LLM.

    Combina:
    1. System prompt (instruções do domínio ativo)
    2. Histórico multi-turn do Redis (contexto da conversa)
    3. Mensagem atual do usuário (com contexto RAG já injetado)

    Args:
        redis: Cliente Redis assíncrono.
        session_id: ID da sessão (UUID como string).
        user_message: Mensagem do usuário (já enriquecida com contexto RAG).
        system_prompt: Prompt do sistema (do domínio ativo).

    Returns:
        Lista de mensagens no formato OpenAI-compatible:
        [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]
    """
    messages: list[dict[str, str]] = []

    # 1. System prompt sempre primeiro
    messages.append({"role": "system", "content": system_prompt})

    # 2. Histórico do Redis (mensagens anteriores)
    history = await get_history(redis, session_id)

    # Limita ao número máximo de mensagens de contexto
    if len(history) > REDIS_HISTORY_MAX_MESSAGES:
        history = history[-REDIS_HISTORY_MAX_MESSAGES:]

    messages.extend(history)

    # 3. Mensagem atual do usuário
    messages.append({"role": "user", "content": user_message})

    log.debug(
        "Mensagens montadas para LLM | session=%s | system=1 | history=%d | user=1",
        session_id,
        len(history),
    )

    return messages


async def save_turn(
    redis: Redis,
    session_id: str,
    user_message: str,
    assistant_reply: str,
    model_used: str,
) -> None:
    """
    Persiste um turno (par pergunta/resposta) no Redis.

    O Redis serve como cache de curto prazo para o contexto multi-turn.
    O Postgres (via app/services/session_service.py) é a fonte de verdade
    durável e é gravado pelo endpoint após a resposta ser gerada.

    Args:
        redis: Cliente Redis assíncrono.
        session_id: ID da sessão (UUID como string).
        user_message: Mensagem do usuário.
        assistant_reply: Resposta do assistente.
        model_used: Modelo utilizado para gerar a resposta.
    """
    key = _session_history_key(session_id)

    # Recupera histórico existente
    history = await get_history(redis, session_id)

    # Adiciona o novo turno
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_reply})

    # Limita ao tamanho máximo (mantém as mais recentes)
    if len(history) > REDIS_HISTORY_MAX_MESSAGES * 2:  # *2 porque são pares
        history = history[-(REDIS_HISTORY_MAX_MESSAGES * 2) :]

    # Salva com TTL
    await redis.set(
        key,
        json.dumps(history),
        ex=REDIS_HISTORY_TTL_SECONDS,
    )

    log.debug(
        "Turno salvo no Redis | session=%s | history_size=%d | model=%s",
        session_id,
        len(history),
        model_used,
    )


async def clear_history(redis: Redis, session_id: str) -> None:
    """
    Limpa o histórico de uma sessão no Redis.

    Útil para reiniciar uma conversa sem criar nova sessão.

    Args:
        redis: Cliente Redis assíncrono.
        session_id: ID da sessão.
    """
    key = _session_history_key(session_id)
    await redis.delete(key)
    log.debug("Histórico Redis limpo | session=%s", session_id)
