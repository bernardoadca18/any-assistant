"""
app/core/config.py
==================
Fonte única de verdade para configurações da aplicação.
Usa pydantic-settings para validar e tipar todas as variáveis de ambiente.
Acesse via `from app.core.config import settings` — nunca leia os.environ diretamente.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configurações centralizadas.
    Prioridade: variáveis de ambiente > .env > valores padrão.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",           # ignora variáveis não declaradas — segurança
    )

    # --- Aplicação -----------------------------------------------------------
    app_name: str = "UE5 AI Assistant"
    app_version: str = "0.1.0"
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
    litellm_base_url: AnyHttpUrl = Field(
        default="http://litellm-proxy:4000",
        description="URL interna do LiteLLM proxy — resolve via DNS da rede litellm_public",
    )
    litellm_master_key: str = Field(..., description="Master key do LiteLLM — obrigatória")

    @property
    def litellm_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.litellm_master_key}"}

    # --- Embeddings (alinhado com ue5_rag_ingestor.py) -----------------------
    embedding_model: str = "mxbai-embed-large"
    embedding_dim: int = 1024
    ollama_base_url: AnyHttpUrl = Field(
        default="http://host.docker.internal:11434",
        description="URL do Ollama — host.docker.internal aponta para o host da máquina",
    )

    # --- RAG -----------------------------------------------------------------
    rag_top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Número de chunks recuperados do Qdrant por query",
    )
    rag_score_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Score mínimo de similaridade coseno para incluir um resultado",
    )

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dim(cls, v: int) -> int:
        allowed = {384, 768, 1024, 1536, 3072}
        if v not in allowed:
            raise ValueError(f"embedding_dim deve ser um de {allowed}; recebido: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Retorna a instância singleton de Settings.
    O lru_cache garante que o .env é lido apenas uma vez durante o ciclo de vida
    da aplicação — ideal para uso com FastAPI Depends().
    """
    return Settings()


# Instância global pré-carregada para imports diretos (ex: em lifespan)
settings: Settings = get_settings()