"""
app/core/config.py
==================
Fonte única de verdade para configurações da aplicação.

Arquitetura de chamadas de modelo (toda via LiteLLM):
- Embeddings: FastAPI → LiteLLM /embeddings → Ollama (Windows)
- Chat/LLM:   FastAPI → LiteLLM /chat/completions → Ollama (Windows)
- Decomposição: FastAPI → LiteLLM → Gemini 2.0 Flash Lite (query decomposer)
- Ingestão (Code-to-Doc): FastAPI → LiteLLM → modelo documentador rápido
  (ver seção "Ingestão" abaixo). Roda DENTRO do próprio container ue5-agent,
  diferente do antigo ingestor externo que rodava no host WSL.

O campo ollama_base_url foi removido do FastAPI — não é mais necessário
porque o FastAPI nunca chama o Ollama diretamente. Toda comunicação com
modelos passa pelo LiteLLM proxy, que já tem o Ollama configurado.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Aplicação -----------------------------------------------------------
    app_name: str = "UE5 AI Assistant"
    app_version: str = "0.2.0"
    app_env: Literal["production", "development", "test"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    # --- Qdrant --------------------------------------------------------------
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "ue5_docs"

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.qdrant_host}:{self.qdrant_port}"

    # --- Redis (Agent Memory) ------------------------------------------------
    redis_host: str = "agent-redis"
    redis_port: int = 6379
    redis_password: str = Field(..., description="Senha do Redis do agente — obrigatória")
    redis_db: int = 0
    redis_session_ttl: int = Field(
        default=3600,
        description="TTL das sessões de conversa em segundos (padrão: 1h)",
    )

    @property
    def redis_url(self) -> str:
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # --- LiteLLM Proxy -------------------------------------------------------
    # Ponto único de entrada para TODOS os modelos: LLM + embeddings + decomposição.
    # O LiteLLM roteia internamente para o Ollama (Windows) ou Gemini (cloud).
    litellm_base_url: str = Field(
        default="http://litellm-proxy:4000",
        description="URL interna do LiteLLM proxy (resolve via DNS da rede litellm_public)",
    )
    litellm_master_key: str = Field(..., description="Master key do LiteLLM — obrigatória")

    @field_validator("litellm_base_url")
    @classmethod
    def validate_litellm_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"litellm_base_url deve começar com http:// ou https://: {v!r}")
        return v.rstrip("/")

    @property
    def litellm_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.litellm_master_key}"}

    # --- Embeddings ----------------------------------------------------------
    # O modelo deve estar configurado no LiteLLM config.yml como um modelo
    # de embedding (ex: provider = ollama, model = mxbai-embed-large).
    embedding_model: str = "ollama-embed"
    embedding_dim: int = 1024

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dim(cls, v: int) -> int:
        allowed = {384, 768, 1024, 1536, 3072}
        if v not in allowed:
            raise ValueError(f"embedding_dim deve ser um de {allowed}; recebido: {v}")
        return v

    # --- RAG (configurações gerais) ------------------------------------------
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_score_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "Score mínimo de similaridade coseno para o Naive RAG. "
            "Não usado no Advanced RAG (o reranker substitui este filtro)."
        ),
    )

    # --- Advanced RAG — Query Decomposition ----------------------------------
    rag_decomposition_enabled: bool = Field(
        default=True,
        description=(
            "Habilita a decomposição de query via LLM. "
            "Desabilitar faz o pipeline rodar como Naive RAG puro."
        ),
    )
    rag_decomposition_model: str = Field(
        default="gemini-3.5-flash-lite",
        description="Modelo LiteLLM para decomposição de queries (deve ser rápido e barato).",
    )
    rag_decomposition_timeout: float = Field(
        default=12.0,
        ge=1.0,
        le=30.0,
        description="Timeout em segundos para a chamada de decomposição.",
    )

    # --- Advanced RAG — Retrieval Expansion ----------------------------------
    rag_retrieval_limit_per_query: int = Field(
        default=15,
        ge=5,
        le=50,
        description=(
            "Número de chunks buscados por query no Qdrant antes do reranking. "
            "Maior = melhor recall, maior latência de rede."
        ),
    )

    # --- Advanced RAG — FlashRank Reranker -----------------------------------
    rag_reranker_enabled: bool = Field(
        default=True,
        description=(
            "Habilita o reranking via FlashRank. "
            "Requer o pacote 'flashrank' instalado. "
            "Se falhar, faz fallback para a ordem original do Qdrant."
        ),
    )
    rag_reranker_model: str = Field(
        default="ms-marco-MiniLM-L-12-v2",
        description=(
            "Modelo FlashRank para Cross-Encoder reranking. "
            "Opções: ms-marco-MiniLM-L-12-v2 (equilibrado), "
            "ms-marco-MultiBERT-L-12 (multilíngue, maior), "
            "rank-T5-flan (maior precisão, mais lento)."
        ),
    )
    rag_reranker_cache_dir: str = Field(
        default="/tmp/flashrank_cache",
        description="Diretório para cache dos modelos FlashRank (ONNX).",
    )
    rag_rerank_min_score: float = Field(
        default=0.0,
        description=(
            "Piso de qualidade aplicado à lista já reordenada pelo FlashRank "
            "(rag_service.py::_run_advanced_retrieval). Chunks com score de "
            "Cross-Encoder abaixo deste valor são descartados antes de ir "
            "para o prompt do LLM, mesmo que estejam dentro do top_k — evita "
            "injetar ruído quando o retrieval expandido não traz nada "
            "realmente relevante. O default 0.0 é seguro (equivale a nenhum "
            "filtro, já que o score do Cross-Encoder do FlashRank não é "
            "negativo na prática) — ajuste para um valor positivo (ex: 0.1) "
            "para de fato descartar candidatos fracos. Sempre mantém ao "
            "menos 1 chunk (o de maior score) mesmo se todos caírem abaixo "
            "do threshold, para não quebrar o prompt do RAG."
        ),
    )

    # --- Ingestão — Pipeline ETL Code-to-Doc ----------------------------------
    # Endpoint: POST /v1/ingest/process-folder (ver app/services/ai/ingestion_service.py).
    # Roda DENTRO do próprio container ue5-agent — substitui os antigos
    # scripts externos de ingestão que rodavam no host WSL.
    ingestion_documenter_model: str = Field(
        default="qwen-local",
        description=(
            "Modelo LiteLLM usado para transformar headers C++ (.h) em "
            "documentação Markdown educacional orientada a gameplay (Canal A). "
            "Deve ser rápido e barato — é chamado uma vez por header relevante."
        ),
    )
    ingestion_batch_size: int = Field(
        default=32,
        ge=1,
        le=256,
        description=(
            "Quantidade de chunks acumulados antes de gerar embeddings e "
            "fazer upsert em lote no Qdrant. Equilibra throughput de rede "
            "contra uso de memória durante a ingestão."
        ),
    )
    ingestion_embedding_concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description=(
            "Número máximo de chamadas de embedding simultâneas dentro de "
            "um mesmo lote. Evita sobrecarregar um Ollama local, que costuma "
            "ter baixa concorrência efetiva em hardware de desenvolvimento."
        ),
    )
    ingestion_max_chunk_chars: int = Field(
        default=3000,
        ge=500,
        le=20_000,
        description=(
            "Rede de segurança: tamanho máximo (em caracteres) de um chunk "
            "após o split estrutural por headers Markdown. Seções "
            "excepcionalmente longas são subdivididas por parágrafo para "
            "não estourar o contexto do modelo de embedding."
        ),
    )
    ingestion_default_ue_version: str = Field(
        default="5.8",
        description=(
            "Versão UE5 usada como fallback quando não é possível extrair "
            "automaticamente um padrão 'X.Y' do caminho da pasta ingerida."
        ),
    )
    ingestion_lock_timeout_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            "TTL (em segundos) do lock distribuído no Redis que serializa "
            "execuções de `process_folder_ingestion` por coleção. Age como "
            "rede de segurança: se o processo morrer sem liberar o lock, ele "
            "expira sozinho após esse tempo em vez de travar ingestões futuras "
            "para sempre."
        ),
    )
    ingestion_max_header_chars: int = Field(
        default=12_000,
        ge=1_000,
        description=(
            "Tamanho máximo (em caracteres) do header C++ já limpo enviado à "
            "LLM documentadora do Canal A. Headers acima desse limite são "
            "truncados antes da chamada para evitar erros de estouro de "
            "contexto (context_length_exceeded), que sem esse corte fariam o "
            "arquivo falhar e ser reprocessado do zero em toda ingestão futura."
        ),
    )
    ingestion_max_consecutive_failures: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Número de falhas consecutivas de um mesmo arquivo (erro de "
            "leitura, LLM ou embedding) antes de colocá-lo em quarentena — "
            "ele para de ser reprocessado automaticamente até intervenção "
            "manual, evitando desperdiçar chamadas de LLM/embedding em loop "
            "por um arquivo permanentemente quebrado."
        ),
    )
    litellm_max_retries: int = Field(
        default=4,
        ge=1,
        le=10,
        description=(
            "Número máximo de tentativas (incluindo a primeira) para chamadas "
            "ao LiteLLM (chat/completions e embeddings) em caso de erro "
            "transitório (HTTP 429/500/502/503/504 ou timeout)."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()