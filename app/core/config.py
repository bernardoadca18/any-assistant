"""
app/core/config.py
==================
Fonte única de verdade para configurações da aplicação.

CORREÇÕES:
- litellm_base_url e ollama_base_url agora são str (não AnyHttpUrl).
  No Pydantic v2, AnyHttpUrl retorna um objeto Url, não str — qualquer
  f-string ou concatenação com ele produz URLs malformadas silenciosamente.
  A validação de formato é mantida via field_validator explícito.
- .python-version indica Python 3.11.9, mas Dockerfile usa 3.12-slim.
  Adicionado comentário e campo para rastreabilidade.
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
    app_version: str = "0.1.0"
    # NOTA: .python-version indica 3.11.9 mas Dockerfile usa python:3.12-slim.
    # Recomendado: alinhe o .python-version para 3.12.x para evitar divergências.
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
    # IMPORTANTE: str, não AnyHttpUrl. No Pydantic v2, AnyHttpUrl não é str
    # e f-strings com ele geram URLs quebradas (ex: "http://litellm-proxy:4000/health"
    # vira "http://litellm-proxy:4000//health" com trailing slash inesperado).
    litellm_base_url: str = Field(
        default="http://litellm-proxy:4000",
        description="URL interna do LiteLLM proxy",
    )
    litellm_master_key: str = Field(..., description="Master key do LiteLLM — obrigatória")

    @field_validator("litellm_base_url")
    @classmethod
    def validate_litellm_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"litellm_base_url deve começar com http:// ou https://: {v!r}")
        return v.rstrip("/")  # remove trailing slash para concatenação segura

    @property
    def litellm_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.litellm_master_key}"}

    # --- Embeddings ----------------------------------------------------------
    embedding_model: str = "mxbai-embed-large"
    embedding_dim: int = 1024
    # str, não AnyHttpUrl — mesma razão acima
    ollama_base_url: str = Field(
        default="http://host.docker.internal:11434",
        description="URL do Ollama — host.docker.internal aponta para o host da máquina",
    )

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"ollama_base_url deve começar com http:// ou https://: {v!r}")
        return v.rstrip("/")

    # --- RAG -----------------------------------------------------------------
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dim(cls, v: int) -> int:
        allowed = {384, 768, 1024, 1536, 3072}
        if v not in allowed:
            raise ValueError(f"embedding_dim deve ser um de {allowed}; recebido: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()