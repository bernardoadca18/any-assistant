"""
app/core/config.py
==================
Fonte única de verdade para configurações da aplicação.

Arquitetura de chamadas de modelo (toda via LiteLLM):
- Embeddings: FastAPI → LiteLLM /embeddings → Ollama (Windows)
- Chat/LLM:   FastAPI → LiteLLM /chat/completions → Ollama (Windows)

O campo ollama_base_url foi removido do FastAPI — não é mais necessário
porque o FastAPI nunca chama o Ollama diretamente. Toda comunicação com
modelos passa pelo LiteLLM proxy, que já tem o Ollama configurado.
O ingestor (que roda no host WSL) ainda acessa o Ollama diretamente via
localhost:11434 — isso está correto e não passa por aqui.
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
    # Ponto único de entrada para TODOS os modelos: LLM + embeddings.
    # O LiteLLM roteia internamente para o Ollama no Windows.
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
    embedding_model: str = "mxbai-embed-large"
    embedding_dim: int = 1024

    @field_validator("embedding_dim")
    @classmethod
    def validate_embedding_dim(cls, v: int) -> int:
        allowed = {384, 768, 1024, 1536, 3072}
        if v not in allowed:
            raise ValueError(f"embedding_dim deve ser um de {allowed}; recebido: {v}")
        return v

    # --- RAG -----------------------------------------------------------------
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()