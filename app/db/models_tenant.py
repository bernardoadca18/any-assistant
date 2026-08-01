"""
app/db/models_tenant.py
=======================
SQLAlchemy ORM models for multi-tenant infrastructure.

These models map 1:1 to the schema defined in db/init/003_tenants_and_collections_schema.sql.
The schema is the source of truth; these models are for runtime ORM operations.

Design principles:
- 3NF compliant with proper foreign key relationships
- Async-compatible (SQLAlchemy async session)
- Cascade rules match database-level constraints
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BOOLEAN,
    FLOAT,
    INTEGER,
    REAL,
    TEXT,
    VARCHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models import Base


# =============================================================================
# TENANTS — Top-level isolation unit
# =============================================================================

class Tenant(Base):
    """Represents an isolated tenant/customer/workspace."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Rate limiting
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer)
    rate_limit_tpm: Mapped[int | None] = mapped_column(Integer)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    collections: Mapped[list["Collection"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    embedding_configs: Mapped[list["EmbeddingConfig"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    rag_configs: Mapped[list["RAGConfig"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    ingestion_configs: Mapped[list["IngestionConfig"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    litellm_configs: Mapped[list["LiteLLMConfig"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    default_configs: Mapped["TenantDefaultConfig | None"] = relationship(
        back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "rate_limit_rpm IS NULL OR rate_limit_rpm > 0",
            name="ck_tenants_rate_limit_rpm_positive"
        ),
        CheckConstraint(
            "rate_limit_tpm IS NULL OR rate_limit_tpm > 0",
            name="ck_tenants_rate_limit_tpm_positive"
        ),
    )


# =============================================================================
# COLLECTIONS — Qdrant collection configuration per tenant
# =============================================================================

class Collection(Base):
    """Represents a Qdrant collection configuration within a tenant."""

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    # Collection identity
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    qdrant_collection_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    # Domain association
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False, default="godot_4")

    # Display metadata
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    # Default filter
    default_project_source_filter: Mapped[str | None] = mapped_column(String(64))

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="collections")
    config: Mapped["CollectionConfig | None"] = relationship(
        back_populates="collection", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),
    )


# =============================================================================
# EMBEDDING CONFIGS — Embedding model configuration
# =============================================================================

class EmbeddingConfig(Base):
    """Embedding model configuration for a tenant."""

    __tablename__ = "embedding_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="ollama-embed")
    dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="embedding_configs")
    collection_configs: Mapped[list["CollectionConfig"]] = relationship(
        back_populates="embedding_config"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_embedding_configs_tenant_name"),
        CheckConstraint("dimension > 0", name="ck_embedding_configs_dimension_positive"),
    )


# =============================================================================
# RAG CONFIGS — RAG pipeline configuration
# =============================================================================

class RAGConfig(Base):
    """RAG pipeline configuration for a tenant."""

    __tablename__ = "rag_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Query Decomposition
    decomposition_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    decomposition_model: Mapped[str] = mapped_column(String(128), nullable=False, default="gemini-3.5-flash-lite")
    decomposition_timeout: Mapped[float] = mapped_column(REAL, nullable=False, default=12.0)

    # Retrieval
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retrieval_limit_per_query: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    score_threshold: Mapped[float] = mapped_column(REAL, nullable=False, default=0.6)

    # Hybrid Search
    hybrid_search_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    decomposition_max_keywords: Mapped[int] = mapped_column(Integer, nullable=False, default=6)

    # FlashRank Reranker
    reranker_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reranker_model: Mapped[str] = mapped_column(String(128), nullable=False, default="ms-marco-MiniLM-L-12-v2")
    reranker_cache_dir: Mapped[str] = mapped_column(String(256), nullable=False, default="/tmp/flashrank_cache")
    rerank_min_score: Mapped[float] = mapped_column(REAL, nullable=False, default=0.0)

    # Custom system prompt override
    system_prompt: Mapped[str | None] = mapped_column(Text)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="rag_configs")
    collection_configs: Mapped[list["CollectionConfig"]] = relationship(
        back_populates="rag_config"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_rag_configs_tenant_name"),
        CheckConstraint(
            "decomposition_timeout >= 1.0 AND decomposition_timeout <= 30.0",
            name="ck_rag_configs_decomposition_timeout_range"
        ),
        CheckConstraint("top_k >= 1 AND top_k <= 20", name="ck_rag_configs_top_k_range"),
        CheckConstraint(
            "retrieval_limit_per_query >= 5 AND retrieval_limit_per_query <= 50",
            name="ck_rag_configs_retrieval_limit_range"
        ),
        CheckConstraint(
            "score_threshold >= 0.0 AND score_threshold <= 1.0",
            name="ck_rag_configs_score_threshold_range"
        ),
        CheckConstraint(
            "decomposition_max_keywords >= 0 AND decomposition_max_keywords <= 20",
            name="ck_rag_configs_max_keywords_range"
        ),
    )


# =============================================================================
# INGESTION CONFIGS — Ingestion pipeline configuration
# =============================================================================

class IngestionConfig(Base):
    """Ingestion pipeline configuration for a tenant."""

    __tablename__ = "ingestion_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Documenter LLM
    documenter_model: Mapped[str] = mapped_column(String(128), nullable=False, default="qwen-local")

    # Batching
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=32)
    embedding_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=8)

    # Chunking limits
    max_chunk_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=3000)
    max_header_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=12000)

    # Failure handling
    max_consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lock_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)

    # Default engine version
    default_ue_version: Mapped[str] = mapped_column(String(16), nullable=False, default="5.8")

    # Source code extensions (stored as JSONB)
    source_code_extensions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: [".h", ".cpp"]
    )
    source_code_sample_extensions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: [".h", ".cpp", ".bp.json"]
    )
    documentation_extensions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: [".udn"]
    )

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="ingestion_configs")
    collection_configs: Mapped[list["CollectionConfig"]] = relationship(
        back_populates="ingestion_config"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_ingestion_configs_tenant_name"),
        CheckConstraint("batch_size >= 1 AND batch_size <= 256", name="ck_ingestion_batch_size_range"),
        CheckConstraint("embedding_concurrency >= 1 AND embedding_concurrency <= 32", name="ck_ingestion_concurrency_range"),
        CheckConstraint("max_chunk_chars >= 500 AND max_chunk_chars <= 20000", name="ck_ingestion_max_chunk_range"),
        CheckConstraint("max_header_chars >= 1000", name="ck_ingestion_max_header_min"),
        CheckConstraint("max_consecutive_failures >= 1 AND max_consecutive_failures <= 20", name="ck_ingestion_failures_range"),
        CheckConstraint("lock_timeout_seconds >= 60", name="ck_ingestion_lock_timeout_min"),
    )


# =============================================================================
# LITELLM CONFIGS — LiteLLM proxy configuration
# =============================================================================

class LiteLLMConfig(Base):
    """LiteLLM proxy configuration for a tenant."""

    __tablename__ = "litellm_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(256), nullable=False, default="http://litellm-proxy:4000")
    master_key: Mapped[str | None] = mapped_column(String(256))  # Encrypted at app layer
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=4)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="litellm_configs")
    collection_configs: Mapped[list["CollectionConfig"]] = relationship(
        back_populates="litellm_config"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_litellm_configs_tenant_name"),
        CheckConstraint("max_retries >= 1 AND max_retries <= 10", name="ck_litellm_retries_range"),
    )


# =============================================================================
# COLLECTION CONFIGS — Junction table for collection-specific configs
# =============================================================================

class CollectionConfig(Base):
    """Configuration overrides for a specific collection."""

    __tablename__ = "collection_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # Optional config overrides (NULL = inherit from tenant default)
    embedding_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("embedding_configs.id", ondelete="SET NULL")
    )
    rag_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rag_configs.id", ondelete="SET NULL")
    )
    ingestion_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_configs.id", ondelete="SET NULL")
    )
    litellm_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("litellm_configs.id", ondelete="SET NULL")
    )

    # Custom system prompt for this collection
    system_prompt_override: Mapped[str | None] = mapped_column(Text)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    collection: Mapped["Collection"] = relationship(back_populates="config")
    embedding_config: Mapped["EmbeddingConfig | None"] = relationship(
        back_populates="collection_configs"
    )
    rag_config: Mapped["RAGConfig | None"] = relationship(
        back_populates="collection_configs"
    )
    ingestion_config: Mapped["IngestionConfig | None"] = relationship(
        back_populates="collection_configs"
    )
    litellm_config: Mapped["LiteLLMConfig | None"] = relationship(
        back_populates="collection_configs"
    )


# =============================================================================
# TENANT DEFAULT CONFIGS — Default configurations for a tenant
# =============================================================================

class TenantDefaultConfig(Base):
    """Default configuration references for a tenant."""

    __tablename__ = "tenant_default_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    # Default config references
    default_embedding_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("embedding_configs.id", ondelete="SET NULL")
    )
    default_rag_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rag_configs.id", ondelete="SET NULL")
    )
    default_ingestion_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingestion_configs.id", ondelete="SET NULL")
    )
    default_litellm_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("litellm_configs.id", ondelete="SET NULL")
    )

    # Default collection
    default_collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="SET NULL")
    )

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="default_configs")


# =============================================================================
# API KEYS — Secure API key storage
# =============================================================================

class APIKey(Base):
    """API key for tenant authentication."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    # Key identifier
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)  # bcrypt hash

    # Permissions
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column()

    # Usage tracking
    last_used_at: Mapped[datetime | None] = mapped_column()
    last_used_ip: Mapped[str | None] = mapped_column(INET)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Relationships
    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_api_keys_tenant_name"),
    )
