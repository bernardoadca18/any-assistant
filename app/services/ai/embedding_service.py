"""
app/services/ai/embedding_service.py
=====================================
Serviço responsável por gerar embeddings via Ollama.

Decisões de design:
- Usa httpx.AsyncClient diretamente em vez do SDK ollama (que é síncrono).
  O SDK ollama não tem suporte async nativo; chamar ollama.embeddings() em
  um endpoint FastAPI bloquearia o event loop inteiro.
- Cliente HTTP reutilizado (não criado por chamada) via singleton gerenciado
  pelo lifespan — evita overhead de TCP handshake em cada request.
- Timeout configurável via settings para ambientes com Ollama lento (cold start
  do modelo pode levar vários segundos na primeira chamada).
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton do cliente HTTP — inicializado no lifespan
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


async def init_embedding_client() -> httpx.AsyncClient:
    """
    Cria o cliente HTTP compartilhado para chamadas ao Ollama.
    Deve ser chamado no lifespan startup, APÓS init_qdrant e init_redis.
    """
    global _http_client
    _http_client = httpx.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=httpx.Timeout(
            connect=5.0,
            read=120.0,   # modelos grandes podem demorar no cold start
            write=10.0,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    log.info("EmbeddingService inicializado | model=%s url=%s",
             settings.embedding_model, settings.ollama_base_url)
    return _http_client


async def close_embedding_client() -> None:
    """Fecha o cliente HTTP. Chamado no lifespan shutdown."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
        log.info("EmbeddingService encerrado.")


def get_embedding_client() -> httpx.AsyncClient:
    """FastAPI Dependency — retorna o cliente HTTP inicializado."""
    if _http_client is None:
        raise RuntimeError("EmbeddingService não inicializado.")
    return _http_client


# ---------------------------------------------------------------------------
# Função principal de embedding
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float]:
    """
    Gera o vetor de embedding para um texto via Ollama.

    Args:
        text: Texto a ser embedado. Deve ter no máximo ~8192 tokens
              (limite do mxbai-embed-large).

    Returns:
        Lista de floats com dimensão EMBEDDING_DIM (1024 para mxbai-embed-large).

    Raises:
        RuntimeError: se o Ollama retornar erro ou resposta inesperada.
        httpx.TimeoutException: se o Ollama não responder no timeout configurado.
    """
    if _http_client is None:
        raise RuntimeError("EmbeddingService não inicializado. Verifique o lifespan.")

    payload = {
        "model": settings.embedding_model,
        "prompt": text,
    }

    try:
        resp = await _http_client.post("/api/embeddings", json=payload)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Ollama retornou HTTP {exc.response.status_code} ao gerar embedding: "
            f"{exc.response.text[:200]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Timeout ao chamar Ollama para embedding (model={settings.embedding_model}). "
            "Verifique se o Ollama está rodando e o modelo está carregado."
        ) from exc

    data = resp.json()
    vector = data.get("embedding")

    if not vector or not isinstance(vector, list):
        raise RuntimeError(
            f"Ollama retornou resposta sem campo 'embedding' válido: {data}"
        )

    if len(vector) != settings.embedding_dim:
        raise RuntimeError(
            f"Dimensão do embedding inesperada: esperado {settings.embedding_dim}, "
            f"recebido {len(vector)}. Verifique se o modelo '{settings.embedding_model}' "
            f"está correto."
        )

    return vector