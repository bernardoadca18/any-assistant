"""
app/services/cache/session_service.py
======================================
Gerencia o histórico de conversas por sessão no Redis.

Estrutura de dados:
    session:{session_id}:history  → JSON string de lista de ChatMessage
    session:{session_id}:meta     → JSON string de dict com metadados

O histórico é uma lista de mensagens ordenadas cronologicamente.
Cada turno adiciona duas entradas: {"role":"user",...} e {"role":"assistant",...}.

TTL: renovado a cada interação para que sessões ativas nunca expirem
inesperadamente. Sessões inativas expiram após redis_session_ttl segundos.

Limitação de contexto: mantemos no máximo MAX_HISTORY_TURNS turnos
(pares user+assistant) para evitar context window overflow no LLM.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from redis.asyncio import Redis

from app.core.config import settings
from app.db.redis import session_history_key, session_meta_key

log = logging.getLogger(__name__)

# Máximo de turnos (pares user+assistant) mantidos no histórico.
# Cada turno = 2 mensagens. 10 turnos = 20 mensagens no contexto.
MAX_HISTORY_TURNS: int = 10


async def load_history(redis: Redis, session_id: str) -> list[dict]:
    """
    Carrega o histórico de mensagens de uma sessão.

    Returns:
        Lista de dicts com chaves 'role' e 'content'.
        Retorna lista vazia se a sessão não existir ou o JSON estiver corrompido.
    """
    key = session_history_key(session_id)
    try:
        raw = await redis.get(key)
        if not raw:
            return []
        history = json.loads(raw)
        if not isinstance(history, list):
            log.warning("Histórico corrompido para session=%s, resetando.", session_id)
            return []
        return history
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("Erro ao carregar histórico da sessão %s: %s", session_id, exc)
        return []


async def save_turn(
    redis: Redis,
    session_id: str,
    user_message: str,
    assistant_reply: str,
    model_used: str,
) -> None:
    """
    Persiste um turno de conversa (user + assistant) no Redis.

    Aplica a janela deslizante MAX_HISTORY_TURNS e renova o TTL da sessão.
    """
    history = await load_history(redis, session_id)

    # Adiciona as duas mensagens do turno atual
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_reply})

    # Janela deslizante: mantém apenas os últimos MAX_HISTORY_TURNS turnos
    # 1 turno = 2 mensagens (user + assistant)
    max_messages = MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        history = history[-max_messages:]
        log.debug(
            "Histórico truncado para %d mensagens (session=%s)",
            max_messages,
            session_id,
        )

    # Atualiza metadados
    meta = {
        "last_active": datetime.now(timezone.utc).isoformat(),
        "model_used": model_used,
        "turn_count": len(history) // 2,
    }

    ttl = settings.redis_session_ttl
    history_key = session_history_key(session_id)
    meta_key = session_meta_key(session_id)

    # Pipeline atômico: escreve history + meta + renova TTL de ambas as chaves
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(history_key, json.dumps(history, ensure_ascii=False), ex=ttl)
        pipe.set(meta_key, json.dumps(meta, ensure_ascii=False), ex=ttl)
        await pipe.execute()

    log.debug(
        "Turno salvo | session=%s turns=%d ttl=%ds",
        session_id,
        len(history) // 2,
        ttl,
    )


async def build_messages_for_llm(
    redis: Redis,
    session_id: str,
    user_message: str,
    system_prompt: str,
) -> list[dict]:
    """
    Monta a lista de mensagens para enviar ao LLM via LiteLLM.

    Estrutura: [system] + [histórico] + [user_atual]

    Args:
        redis: Cliente Redis.
        session_id: ID da sessão de conversa.
        user_message: Mensagem atual do usuário (JÁ com contexto RAG injetado).
        system_prompt: Prompt de sistema com persona e instruções do agente.

    Returns:
        Lista de dicts compatível com a API OpenAI/LiteLLM.
    """
    history = await load_history(redis, session_id)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_message},
    ]
    return messages


async def clear_session(redis: Redis, session_id: str) -> None:
    """Remove todos os dados de uma sessão do Redis."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(session_history_key(session_id))
        pipe.delete(session_meta_key(session_id))
        await pipe.execute()
    log.info("Sessão %s removida.", session_id)