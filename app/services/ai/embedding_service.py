"""
app/services/ai/embedding_service.py
=====================================
Gera embeddings via LiteLLM proxy (que por sua vez chama o Ollama no Windows).

MUDANÇA ARQUITETURAL — por que LiteLLM em vez de Ollama direto:
- O Ollama roda no Windows nativo, fora do WSL e dos containers Docker.
- A comunicação WSL → Windows via host.docker.internal é instável para o
  Ollama (Connection refused intermitente, dependente de configuração de
  firewall do Windows e da porta de escuta do Ollama).
- O LiteLLM já está configurado, rodando e com todos os modelos vinculados.
  Usar o LiteLLM como ponto único de saída para TODAS as chamadas de modelo
  (tanto LLM quanto embeddings) simplifica a arquitetura e elimina a
  dependência de host.docker.internal completamente para o FastAPI.
- A API de embeddings do LiteLLM é compatível com OpenAI: POST /embeddings
  com body {"model": "...", "input": "..."} e resposta em data[0].embedding.

O ingestor (ue5_rag_ingestor.py) roda no HOST (WSL), onde tem acesso direto
ao Ollama em localhost:11434, então não precisa mudar.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton do cliente HTTP
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


async def init_embedding_client() -> httpx.AsyncClient:
    """
    Cria o cliente HTTP apontando para o LiteLLM proxy.
    O LiteLLM roteia a chamada de embedding para o Ollama no Windows.
    """
    global _http_client
    _http_client = httpx.AsyncClient(
        base_url=settings.litellm_base_url,   # http://litellm-proxy:4000
        headers=settings.litellm_headers,      # Authorization: Bearer ...
        timeout=httpx.Timeout(
            connect=5.0,
            read=120.0,
            write=10.0,
            pool=5.0,
        ),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    log.info(
        "EmbeddingService inicializado via LiteLLM | model=%s url=%s",
        settings.embedding_model,
        settings.litellm_base_url,
    )
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
# Retry / backoff para erros transitórios
# ---------------------------------------------------------------------------
# NOTA: durante a ingestão em lote (ver ingestion_service.py), esta função é
# chamada dezenas de milhares de vezes numa única execução. Sem retry, um
# rate limit (429) ou uma falha momentânea do Ollama/LiteLLM derruba o chunk
# inteiro na hora — o loop principal segue imediatamente para o próximo
# chunk sem pausa, o que pode transformar um soluço passageiro numa cascata
# de falhas por todo o restante do lote. O backoff exponencial + jitter dá
# tempo do serviço se recuperar antes de desistir do chunk.

# CORREÇÃO (ALTO — retry não cobria erros de conexão): antes só
# httpx.TimeoutException era tratado como transitório. httpx.ConnectError
# (conexão recusada, DNS falhando, container do LiteLLM ainda subindo) é
# subclasse de httpx.TransportError, NÃO de TimeoutException — ficava de
# fora e desistia na primeira tentativa mesmo com retries configurados.
# httpx.RemoteProtocolError (conexão encerrada abruptamente pelo servidor,
# comum em restarts de proxy) tem o mesmo problema e foi incluído também.
def _is_retryable_embedding_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


@retry(
    retry=retry_if_exception(_is_retryable_embedding_error),
    wait=wait_exponential_jitter(initial=2, max=30),
    stop=stop_after_attempt(settings.litellm_max_retries),
    reraise=True,
)
async def _post_embedding(client: httpx.AsyncClient, payload: dict) -> httpx.Response:
    resp = await client.post("/embeddings", json=payload)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Função principal de embedding
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float]:
    """
    Gera embedding via LiteLLM proxy (OpenAI-compatible /embeddings endpoint).

    O LiteLLM recebe a chamada e a repassa para o Ollama configurado no
    config do LiteLLM (mxbai-embed-large rodando no Windows via Ollama).

    Args:
        text: Texto a ser embedado.

    Returns:
        Lista de floats com dimensão 1024 (mxbai-embed-large).

    Raises:
        RuntimeError: se o LiteLLM/Ollama retornar erro persistente (após as
            tentativas de retry) ou resposta inesperada.
    """
    if _http_client is None:
        raise RuntimeError("EmbeddingService não inicializado. Verifique o lifespan.")

    # API OpenAI-compatible: usa "input" (não "prompt" que é específico do Ollama)
    payload = {
        "model": settings.embedding_model,
        "input": text,
    }

    try:
        resp = await _post_embedding(_http_client, payload)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM retornou HTTP {exc.response.status_code} ao gerar embedding "
            f"após esgotar as tentativas de retry: {exc.response.text[:400]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Timeout ao gerar embedding via LiteLLM (model={settings.embedding_model}) "
            "após esgotar as tentativas de retry. Verifique se o LiteLLM e o "
            "Ollama estão respondendo."
        ) from exc

    data = resp.json()

    # Resposta OpenAI-compatible: {"data": [{"embedding": [...], "index": 0}]}
    try:
        vector = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"LiteLLM retornou resposta de embedding em formato inesperado: {data}"
        ) from exc

    if not vector or not isinstance(vector, list):
        raise RuntimeError(
            f"Campo 'embedding' inválido na resposta do LiteLLM: {data}"
        )

    if len(vector) != settings.embedding_dim:
        raise RuntimeError(
            f"Dimensão do embedding inesperada: esperado {settings.embedding_dim}, "
            f"recebido {len(vector)}. Verifique se o modelo '{settings.embedding_model}' "
            f"está configurado corretamente no LiteLLM."
        )

    return vector