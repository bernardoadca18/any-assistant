"""
app/schemas/tenant.py
=====================
Pydantic schemas for multi-tenant configuration.

Defines TenantConfig and CollectionConfig for per-tenant/collection
customization of RAG pipeline, ingestion, and model settings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Embedding Configuration per Collection
# ---------------------------------------------------------------------------

class EmbeddingConfig(BaseModel):
    """Embedding model configuration for a specific collection."""

    model: str = Field(
        default="ollama-embed",
        description="LiteLLM model name for embeddings (must be configured in LiteLLM config.yml)",
    )
    dimension: int = Field(
        default=1024,
        description="Embedding vector dimension. Must match the model output.",
    )


# ---------------------------------------------------------------------------
# RAG Pipeline Configuration per Collection/Tenant
# ---------------------------------------------------------------------------

class RAGConfig(BaseModel):
    """RAG pipeline configuration for a specific collection/tenant."""

    # Query Decomposition
    decomposition_enabled: bool = Field(
        default=True,
        description="Enable query decomposition via LLM",
    )
    decomposition_model: str = Field(
        default="gemini-3.5-flash-lite",
        description="LiteLLM model for query decomposition (fast, cheap)",
    )
    decomposition_timeout: float = Field(
        default=12.0,
        ge=1.0,
        le=30.0,
        description="Timeout in seconds for decomposition call",
    )

    # Retrieval
    top_k: int = Field(default=5, ge=1, le=20, description="Final chunks returned to LLM")
    retrieval_limit_per_query: int = Field(
        default=15,
        ge=5,
        le=50,
        description="Chunks retrieved per sub-query before reranking",
    )
    score_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity for naive RAG (not used in advanced RAG)",
    )

    # Hybrid Search
    hybrid_search_enabled: bool = Field(
        default=True,
        description="Enable hybrid search (dense + sparse via RRF)",
    )
    decomposition_max_keywords: int = Field(
        default=6,
        ge=0,
        le=20,
        description="Max technical keywords extracted per query for sparse channel",
    )

    # FlashRank Reranker
    reranker_enabled: bool = Field(
        default=True,
        description="Enable FlashRank cross-encoder reranking",
    )
    reranker_model: str = Field(
        default="ms-marco-MiniLM-L-12-v2",
        description="FlashRank model for cross-encoder reranking",
    )
    reranker_cache_dir: str = Field(
        default="/tmp/flashrank_cache",
        description="Directory for FlashRank ONNX model cache",
    )
    rerank_min_score: float = Field(
        default=0.0,
        description="Minimum cross-encoder score floor (post-rerank quality filter)",
    )

    # System Prompt
    system_prompt: str | None = Field(
        default=None,
        description="Custom system prompt for this tenant/collection. If None, uses default UE5 prompt.",
    )


# ---------------------------------------------------------------------------
# Ingestion Configuration per Collection
# ---------------------------------------------------------------------------

class IngestionConfig(BaseModel):
    """Ingestion pipeline configuration for a specific collection."""

    documenter_model: str = Field(
        default="qwen-local",
        description="LiteLLM model for code-to-doc transformation (Channel A/C/D)",
    )
    batch_size: int = Field(
        default=32,
        ge=1,
        le=256,
        description="Chunks accumulated before embedding + upsert batch",
    )
    embedding_concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Max concurrent embedding calls within a batch",
    )
    max_chunk_chars: int = Field(
        default=3000,
        ge=500,
        le=20000,
        description="Max chunk size after structural markdown split",
    )
    max_header_chars: int = Field(
        default=12000,
        ge=1000,
        description="Max C++ header chars sent to documenter LLM (truncation safety)",
    )
    max_consecutive_failures: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Failures before file quarantine",
    )
    lock_timeout_seconds: int = Field(
        default=3600,
        ge=60,
        description="Distributed lock TTL for ingestion serialization",
    )
    default_ue_version: str = Field(
        default="5.8",
        description="Fallback UE version when not extractable from path",
    )

    # Channel extensions (extensible for plugins)
    source_code_extensions: tuple[str, ...] = Field(
        default=(".h", ".cpp"),
        description="Extensions for source_code content_type",
    )
    source_code_sample_extensions: tuple[str, ...] = Field(
        default=(".h", ".cpp", ".bp.json"),
        description="Extensions for source_code with sample_mode=True",
    )
    documentation_extensions: tuple[str, ...] = Field(
        default=(".udn",),
        description="Extensions for documentation content_type",
    )


# ---------------------------------------------------------------------------
# LiteLLM Configuration
# ---------------------------------------------------------------------------

class LiteLLMConfig(BaseModel):
    """LiteLLM proxy configuration (can be overridden per tenant if needed)."""

    base_url: str = Field(
        default="http://litellm-proxy:4000",
        description="Internal LiteLLM proxy URL",
    )
    master_key: str | None = Field(
        default=None,
        description="Master key for LiteLLM (if different from global)",
    )
    max_retries: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Max retries for transient LiteLLM errors",
    )


# ---------------------------------------------------------------------------
# Collection Configuration (aggregates all configs for a collection)
# ---------------------------------------------------------------------------

class CollectionConfig(BaseModel):
    """
    Complete configuration for a single Qdrant collection.

    A tenant can have multiple collections (e.g., ue5_docs, ue5_samples, custom_docs).
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Qdrant collection name (must be unique across tenants)",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable name for UI/admin",
    )
    description: str = Field(
        default="",
        description="Optional description of this collection's content",
    )

    # Embedding config (defines vector dimension for Qdrant collection creation)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)

    # RAG pipeline config
    rag: RAGConfig = Field(default_factory=RAGConfig)

    # Ingestion config
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)

    # Optional LiteLLM override
    litellm: LiteLLMConfig | None = Field(
        default=None,
        description="Optional per-collection LiteLLM override",
    )

    # Domain association (links to app.core.domains.registry)
    domain: str | None = Field(
        default=None,
        description="Domain identifier (e.g., 'ue5', 'godot4') for system prompt selection",
    )

    # Project source filter default (used when not specified in chat request)
    default_project_source_filter: str | None = Field(
        default=None,
        description="Default project_source filter for this collection",
    )

    # Status
    is_active: bool = Field(
        default=True,
        description="Whether this collection is available for queries/ingestion",
    )

    # Metadata
    created_at: str = Field(
        default="",
        description="ISO timestamp when collection was created",
    )
    updated_at: str = Field(
        default="",
        description="ISO timestamp when collection was last updated",
    )


# ---------------------------------------------------------------------------
# Tenant Configuration (aggregates collections + tenant-level settings)
# ---------------------------------------------------------------------------

class TenantConfig(BaseModel):
    """
    Complete configuration for a tenant.

    A tenant represents an isolated namespace with its own collections,
    configurations, and access control.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unique tenant identifier (slug, e.g., 'acme-corp', 'ue5-official')",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable tenant name",
    )
    description: str = Field(
        default="",
        description="Optional description",
    )

    # Collections belonging to this tenant
    collections: dict[str, CollectionConfig] = Field(
        default_factory=dict,
        description="Map of collection_name -> CollectionConfig",
    )

    # Default collection for this tenant (used when none specified)
    default_collection: str | None = Field(
        default=None,
        description="Default collection name for chat/ingest when not specified",
    )

    # Tenant-level overrides (apply to all collections unless overridden)
    default_embedding: EmbeddingConfig | None = Field(
        default=None,
        description="Tenant-wide default embedding config",
    )
    default_rag: RAGConfig | None = Field(
        default=None,
        description="Tenant-wide default RAG config",
    )
    default_ingestion: IngestionConfig | None = Field(
        default=None,
        description="Tenant-wide default ingestion config",
    )
    default_litellm: LiteLLMConfig | None = Field(
        default=None,
        description="Tenant-wide default LiteLLM config",
    )

    # Rate limiting (future use)
    rate_limit_rpm: int | None = Field(
        default=None,
        ge=1,
        description="Requests per minute limit for this tenant",
    )
    rate_limit_tpm: int | None = Field(
        default=None,
        ge=1,
        description="Tokens per minute limit for this tenant",
    )

    # Status
    is_active: bool = Field(
        default=True,
        description="Whether tenant is active and can make requests",
    )

    # Metadata
    created_at: str = Field(
        default="",
        description="ISO timestamp when tenant was created",
    )
    updated_at: str = Field(
        default="",
        description="ISO timestamp when tenant was last updated",
    )


# ---------------------------------------------------------------------------
# API Request/Response Schemas
# ---------------------------------------------------------------------------

class TenantCreateRequest(BaseModel):
    """Request to create a new tenant."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="")
    default_collection: str | None = None


class TenantUpdateRequest(BaseModel):
    """Request to update a tenant."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    default_collection: str | None = None
    is_active: bool | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1)
    rate_limit_tpm: int | None = Field(default=None, ge=1)


class TenantResponse(BaseModel):
    """Response for tenant operations."""

    id: str
    name: str
    description: str
    default_collection: str | None
    is_active: bool
    rate_limit_rpm: int | None
    rate_limit_tpm: int | None
    created_at: str
    updated_at: str
    collections: list[str] = Field(default_factory=list)


class CollectionCreateRequest(BaseModel):
    """Request to create a new collection within a tenant."""

    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="")
    embedding: EmbeddingConfig | None = None
    rag: RAGConfig | None = None
    ingestion: IngestionConfig | None = None
    litellm: LiteLLMConfig | None = None
    domain: str | None = None
    default_project_source_filter: str | None = None


class CollectionUpdateRequest(BaseModel):
    """Request to update a collection."""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    embedding: EmbeddingConfig | None = None
    rag: RAGConfig | None = None
    ingestion: IngestionConfig | None = None
    litellm: LiteLLMConfig | None = None
    domain: str | None = None
    default_project_source_filter: str | None = None
    is_active: bool | None = None


class CollectionResponse(BaseModel):
    """Response for collection operations."""

    name: str
    display_name: str
    description: str
    embedding: EmbeddingConfig
    rag: RAGConfig
    ingestion: IngestionConfig
    litellm: LiteLLMConfig | None
    domain: str | None
    default_project_source_filter: str | None
    is_active: bool
    created_at: str
    updated_at: str