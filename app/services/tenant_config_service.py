"""
app/services/tenant_config_service.py
=====================================
Service for dynamically resolving tenant and collection configurations
from PostgreSQL with Redis caching.

This service replaces hardcoded configuration from `settings` and
`app/core/domains/` with database-backed, tenant-specific configurations.

Architecture:
- L1 Cache: In-process memory (short TTL, process-scoped)
- L2 Cache: Redis (medium TTL, shared across processes)
- L3 Source: PostgreSQL (authoritative, persistent)

The resolution chain follows this priority:
1. Collection-specific config override (if set)
2. Tenant default config (if set)
3. Domain profile defaults (from app/core/domains/)
4. System defaults (from settings)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.domains.registry import domain_registry
from app.db.models_tenant import (
    Collection,
    CollectionConfig,
    EmbeddingConfig,
    IngestionConfig,
    LiteLLMConfig,
    RAGConfig,
    Tenant,
    TenantDefaultConfig,
)

log = logging.getLogger(__name__)


# =============================================================================
# Configuration Data Classes (resolved, flattened for runtime use)
# =============================================================================

@dataclass(frozen=True)
class ResolvedEmbeddingConfig:
    """Resolved embedding configuration ready for runtime use."""
    model: str
    dimension: int


@dataclass(frozen=True)
class ResolvedRAGConfig:
    """Resolved RAG pipeline configuration ready for runtime use."""
    decomposition_enabled: bool
    decomposition_model: str
    decomposition_timeout: float
    top_k: int
    retrieval_limit_per_query: int
    score_threshold: float
    hybrid_search_enabled: bool
    decomposition_max_keywords: int
    reranker_enabled: bool
    reranker_model: str
    reranker_cache_dir: str
    rerank_min_score: float


@dataclass(frozen=True)
class ResolvedIngestionConfig:
    """Resolved ingestion pipeline configuration ready for runtime use."""
    documenter_model: str
    batch_size: int
    embedding_concurrency: int
    max_chunk_chars: int
    max_header_chars: int
    max_consecutive_failures: int
    lock_timeout_seconds: int
    default_ue_version: str
    source_code_extensions: list[str]
    source_code_sample_extensions: list[str]
    documentation_extensions: list[str]


@dataclass(frozen=True)
class ResolvedLiteLLMConfig:
    """Resolved LiteLLM configuration ready for runtime use."""
    base_url: str
    master_key: str | None
    max_retries: int


@dataclass(frozen=True)
class ResolvedCollectionConfig:
    """
    Fully resolved configuration for a collection.

    This is the main config object used by RAG and ingestion pipelines.
    All values are guaranteed to be non-None (fallbacks applied).
    """
    # Identity
    tenant_id: uuid.UUID
    tenant_slug: str
    collection_id: uuid.UUID
    collection_name: str
    qdrant_collection_name: str
    domain_id: str

    # System prompt (from domain profile or override)
    system_prompt: str
    decomposition_prompt: str

    # Configurations (resolved with fallbacks)
    embedding: ResolvedEmbeddingConfig
    rag: ResolvedRAGConfig
    ingestion: ResolvedIngestionConfig
    litellm: ResolvedLiteLLMConfig

    # Default filter
    default_project_source_filter: str | None


# =============================================================================
# Cache Keys and TTLs
# =============================================================================

def _tenant_cache_key(tenant_id: uuid.UUID) -> str:
    """Redis key for tenant config cache."""
    return f"tenant_config:{tenant_id}"


def _collection_cache_key(collection_id: uuid.UUID) -> str:
    """Redis key for collection config cache."""
    return f"collection_config:{collection_id}"


def _tenant_by_slug_cache_key(slug: str) -> str:
    """Redis key for tenant slug -> ID lookup cache."""
    return f"tenant_slug:{slug}"


# Cache TTLs
TENANT_CACHE_TTL_SECONDS = 300  # 5 minutes
COLLECTION_CACHE_TTL_SECONDS = 300  # 5 minutes


# =============================================================================
# Tenant Configuration Service
# =============================================================================

class TenantConfigService:
    """
    Service for resolving tenant and collection configurations.

    Provides a unified interface for configuration resolution with:
    - Multi-layer caching (in-process + Redis)
    - Fallback chain (collection -> tenant -> domain -> system)
    - Type-safe resolved config objects
    """

    def __init__(self, redis: Redis):
        self._redis = redis
        # In-process cache for ultra-fast lookups within the same request
        self._process_cache: dict[str, tuple[datetime, Any]] = {}
        self._process_cache_ttl = timedelta(seconds=30)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def get_tenant_by_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> Tenant | None:
        """Fetch tenant by ID with caching."""
        # Check process cache
        cache_key = f"tenant:{tenant_id}"
        cached = self._get_from_process_cache(cache_key)
        if cached is not None:
            return cached

        # Check Redis cache
        redis_key = _tenant_cache_key(tenant_id)
        cached_json = await self._redis.get(redis_key)
        if cached_json:
            try:
                data = json.loads(cached_json)
                tenant = self._deserialize_tenant(data)
                self._set_in_process_cache(cache_key, tenant)
                return tenant
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("Failed to deserialize cached tenant %s: %s", tenant_id, e)

        # Fetch from database
        result = await db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = result.scalar_one_or_none()

        if tenant is not None:
            # Cache in Redis
            await self._redis.set(
                redis_key,
                json.dumps(self._serialize_tenant(tenant)),
                ex=TENANT_CACHE_TTL_SECONDS,
            )
            # Cache in process
            self._set_in_process_cache(cache_key, tenant)

        return tenant

    async def get_tenant_by_slug(
        self,
        db: AsyncSession,
        slug: str,
    ) -> Tenant | None:
        """Fetch tenant by slug with caching."""
        # Check process cache for slug -> ID mapping
        cache_key = f"tenant_slug:{slug}"
        cached_id = self._get_from_process_cache(cache_key)
        if cached_id is not None:
            return await self.get_tenant_by_id(db, cached_id)

        # Check Redis cache for slug -> ID mapping
        redis_key = _tenant_by_slug_cache_key(slug)
        cached_id_str = await self._redis.get(redis_key)
        if cached_id_str:
            tenant_id = uuid.UUID(cached_id_str)
            self._set_in_process_cache(cache_key, tenant_id)
            return await self.get_tenant_by_id(db, tenant_id)

        # Fetch from database
        result = await db.execute(
            select(Tenant).where(Tenant.slug == slug)
        )
        tenant = result.scalar_one_or_none()

        if tenant is not None:
            # Cache slug -> ID mapping
            await self._redis.set(
                redis_key,
                str(tenant.id),
                ex=TENANT_CACHE_TTL_SECONDS,
            )
            self._set_in_process_cache(cache_key, tenant.id)
            # Also cache the tenant itself
            await self.get_tenant_by_id(db, tenant.id)

        return tenant

    async def get_collection_by_name(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_name: str,
    ) -> Collection | None:
        """Fetch collection by tenant ID and name."""
        result = await db.execute(
            select(Collection).where(
                Collection.tenant_id == tenant_id,
                Collection.name == collection_name,
                Collection.is_active == True,
            )
        )
        return result.scalar_one_or_none()

    async def resolve_collection_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_name: str | None = None,
    ) -> ResolvedCollectionConfig:
        """
        Resolve complete configuration for a collection.

        This is the main entry point for RAG and ingestion pipelines.

        Args:
            db: Database session
            tenant_id: Tenant UUID
            collection_name: Collection name (optional, uses tenant default if None)

        Returns:
            Fully resolved configuration with all fallbacks applied.

        Raises:
            ValueError: If tenant not found or no default collection configured.
        """
        # Fetch tenant
        tenant = await self.get_tenant_by_id(db, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")

        # Resolve collection
        collection: Collection | None = None
        if collection_name:
            collection = await self.get_collection_by_name(db, tenant_id, collection_name)

        if collection is None:
            # Try to get default collection
            default_config = await self._get_tenant_default_config(db, tenant_id)
            if default_config and default_config.default_collection_id:
                result = await db.execute(
                    select(Collection).where(Collection.id == default_config.default_collection_id)
                )
                collection = result.scalar_one_or_none()

        if collection is None:
            raise ValueError(
                f"No collection found for tenant {tenant.slug} "
                f"(requested: {collection_name})"
            )

        # Resolve all configurations with fallbacks
        return await self._build_resolved_config(db, tenant, collection)

    async def resolve_default_collection_for_domain(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        domain_id: str,
    ) -> ResolvedCollectionConfig:
        """
        Resolve configuration for a domain's default collection within a tenant.

        Used when no specific collection is requested but a domain is known.
        """
        # Try to find a collection matching the domain
        result = await db.execute(
            select(Collection).where(
                Collection.tenant_id == tenant_id,
                Collection.domain_id == domain_id,
                Collection.is_active == True,
            ).limit(1)
        )
        collection = result.scalar_one_or_none()

        if collection is None:
            raise ValueError(
                f"No collection found for domain '{domain_id}' in tenant {tenant_id}"
            )

        tenant = await self.get_tenant_by_id(db, tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")

        return await self._build_resolved_config(db, tenant, collection)

    # -------------------------------------------------------------------------
    # Configuration Resolution (private)
    # -------------------------------------------------------------------------

    async def _build_resolved_config(
        self,
        db: AsyncSession,
        tenant: Tenant,
        collection: Collection,
    ) -> ResolvedCollectionConfig:
        """Build fully resolved configuration from tenant, collection, and defaults."""

        # Get collection-specific config overrides
        collection_config = await self._get_collection_config(db, collection.id)

        # Get tenant default configs
        tenant_defaults = await self._get_tenant_default_config(db, tenant.id)

        # Resolve each configuration layer
        embedding = await self._resolve_embedding_config(
            db, tenant.id, collection_config, tenant_defaults
        )
        rag = await self._resolve_rag_config(
            db, tenant.id, collection_config, tenant_defaults, collection.domain_id
        )
        ingestion = await self._resolve_ingestion_config(
            db, tenant.id, collection_config, tenant_defaults
        )
        litellm = await self._resolve_litellm_config(
            db, tenant.id, collection_config, tenant_defaults
        )

        # Resolve prompts
        domain_profile = domain_registry.get(collection.domain_id)

        # System prompt: collection override > domain default
        system_prompt = collection_config.system_prompt_override if collection_config else None
        if not system_prompt:
            system_prompt = domain_profile.system_prompt

        # Decomposition prompt: always from domain profile
        decomposition_prompt = domain_profile.decomposition_prompt

        return ResolvedCollectionConfig(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            collection_id=collection.id,
            collection_name=collection.name,
            qdrant_collection_name=collection.qdrant_collection_name,
            domain_id=collection.domain_id,
            system_prompt=system_prompt,
            decomposition_prompt=decomposition_prompt,
            embedding=embedding,
            rag=rag,
            ingestion=ingestion,
            litellm=litellm,
            default_project_source_filter=collection.default_project_source_filter,
        )

    async def _resolve_embedding_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_config: CollectionConfig | None,
        tenant_defaults: TenantDefaultConfig | None,
    ) -> ResolvedEmbeddingConfig:
        """Resolve embedding configuration with fallbacks."""

        config: EmbeddingConfig | None = None

        # 1. Collection override
        if collection_config and collection_config.embedding_config_id:
            config = await self._get_embedding_config(db, collection_config.embedding_config_id)

        # 2. Tenant default
        if config is None and tenant_defaults and tenant_defaults.default_embedding_config_id:
            config = await self._get_embedding_config(db, tenant_defaults.default_embedding_config_id)

        # 3. System default from settings
        if config is None:
            return ResolvedEmbeddingConfig(
                model=settings.embedding_model,
                dimension=settings.embedding_dim,
            )

        return ResolvedEmbeddingConfig(
            model=config.model,
            dimension=config.dimension,
        )

    async def _resolve_rag_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_config: CollectionConfig | None,
        tenant_defaults: TenantDefaultConfig | None,
        domain_id: str,
    ) -> ResolvedRAGConfig:
        """Resolve RAG configuration with fallbacks."""

        config: RAGConfig | None = None

        # 1. Collection override
        if collection_config and collection_config.rag_config_id:
            config = await self._get_rag_config(db, collection_config.rag_config_id)

        # 2. Tenant default
        if config is None and tenant_defaults and tenant_defaults.default_rag_config_id:
            config = await self._get_rag_config(db, tenant_defaults.default_rag_config_id)

        # 3. System default from settings
        if config is None:
            return ResolvedRAGConfig(
                decomposition_enabled=settings.rag_decomposition_enabled,
                decomposition_model=settings.rag_decomposition_model,
                decomposition_timeout=settings.rag_decomposition_timeout,
                top_k=settings.rag_top_k,
                retrieval_limit_per_query=settings.rag_retrieval_limit_per_query,
                score_threshold=settings.rag_score_threshold,
                hybrid_search_enabled=settings.rag_hybrid_search_enabled,
                decomposition_max_keywords=settings.rag_decomposition_max_keywords,
                reranker_enabled=settings.rag_reranker_enabled,
                reranker_model=settings.rag_reranker_model,
                reranker_cache_dir=settings.rag_reranker_cache_dir,
                rerank_min_score=settings.rag_rerank_min_score,
            )

        return ResolvedRAGConfig(
            decomposition_enabled=config.decomposition_enabled,
            decomposition_model=config.decomposition_model,
            decomposition_timeout=config.decomposition_timeout,
            top_k=config.top_k,
            retrieval_limit_per_query=config.retrieval_limit_per_query,
            score_threshold=config.score_threshold,
            hybrid_search_enabled=config.hybrid_search_enabled,
            decomposition_max_keywords=config.decomposition_max_keywords,
            reranker_enabled=config.reranker_enabled,
            reranker_model=config.reranker_model,
            reranker_cache_dir=config.reranker_cache_dir,
            rerank_min_score=config.rerank_min_score,
        )

    async def _resolve_ingestion_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_config: CollectionConfig | None,
        tenant_defaults: TenantDefaultConfig | None,
    ) -> ResolvedIngestionConfig:
        """Resolve ingestion configuration with fallbacks."""

        config: IngestionConfig | None = None

        # 1. Collection override
        if collection_config and collection_config.ingestion_config_id:
            config = await self._get_ingestion_config(db, collection_config.ingestion_config_id)

        # 2. Tenant default
        if config is None and tenant_defaults and tenant_defaults.default_ingestion_config_id:
            config = await self._get_ingestion_config(db, tenant_defaults.default_ingestion_config_id)

        # 3. System default from settings
        if config is None:
            return ResolvedIngestionConfig(
                documenter_model=settings.ingestion_documenter_model,
                batch_size=settings.ingestion_batch_size,
                embedding_concurrency=settings.ingestion_embedding_concurrency,
                max_chunk_chars=settings.ingestion_max_chunk_chars,
                max_header_chars=settings.ingestion_max_header_chars,
                max_consecutive_failures=settings.ingestion_max_consecutive_failures,
                lock_timeout_seconds=settings.ingestion_lock_timeout_seconds,
                default_ue_version=settings.ingestion_default_ue_version,
                source_code_extensions=[".h", ".cpp"],
                source_code_sample_extensions=[".h", ".cpp", ".bp.json"],
                documentation_extensions=[".udn"],
            )

        return ResolvedIngestionConfig(
            documenter_model=config.documenter_model,
            batch_size=config.batch_size,
            embedding_concurrency=config.embedding_concurrency,
            max_chunk_chars=config.max_chunk_chars,
            max_header_chars=config.max_header_chars,
            max_consecutive_failures=config.max_consecutive_failures,
            lock_timeout_seconds=config.lock_timeout_seconds,
            default_ue_version=config.default_ue_version,
            source_code_extensions=config.source_code_extensions if isinstance(config.source_code_extensions, list) else [],
            source_code_sample_extensions=config.source_code_sample_extensions if isinstance(config.source_code_sample_extensions, list) else [],
            documentation_extensions=config.documentation_extensions if isinstance(config.documentation_extensions, list) else [],
        )

    async def _resolve_litellm_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        collection_config: CollectionConfig | None,
        tenant_defaults: TenantDefaultConfig | None,
    ) -> ResolvedLiteLLMConfig:
        """Resolve LiteLLM configuration with fallbacks."""

        config: LiteLLMConfig | None = None

        # 1. Collection override
        if collection_config and collection_config.litellm_config_id:
            config = await self._get_litellm_config(db, collection_config.litellm_config_id)

        # 2. Tenant default
        if config is None and tenant_defaults and tenant_defaults.default_litellm_config_id:
            config = await self._get_litellm_config(db, tenant_defaults.default_litellm_config_id)

        # 3. System default from settings
        if config is None:
            return ResolvedLiteLLMConfig(
                base_url=settings.litellm_base_url,
                master_key=settings.litellm_master_key,
                max_retries=settings.litellm_max_retries,
            )

        return ResolvedLiteLLMConfig(
            base_url=config.base_url,
            master_key=config.master_key or settings.litellm_master_key,
            max_retries=config.max_retries,
        )

    # -------------------------------------------------------------------------
    # Database Fetch Helpers
    # -------------------------------------------------------------------------

    async def _get_collection_config(
        self,
        db: AsyncSession,
        collection_id: uuid.UUID,
    ) -> CollectionConfig | None:
        """Fetch collection-specific config overrides."""
        result = await db.execute(
            select(CollectionConfig).where(CollectionConfig.collection_id == collection_id)
        )
        return result.scalar_one_or_none()

    async def _get_tenant_default_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
    ) -> TenantDefaultConfig | None:
        """Fetch tenant default config references."""
        result = await db.execute(
            select(TenantDefaultConfig).where(TenantDefaultConfig.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def _get_embedding_config(
        self,
        db: AsyncSession,
        config_id: uuid.UUID,
    ) -> EmbeddingConfig | None:
        """Fetch embedding config by ID."""
        result = await db.execute(
            select(EmbeddingConfig).where(EmbeddingConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    async def _get_rag_config(
        self,
        db: AsyncSession,
        config_id: uuid.UUID,
    ) -> RAGConfig | None:
        """Fetch RAG config by ID."""
        result = await db.execute(
            select(RAGConfig).where(RAGConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    async def _get_ingestion_config(
        self,
        db: AsyncSession,
        config_id: uuid.UUID,
    ) -> IngestionConfig | None:
        """Fetch ingestion config by ID."""
        result = await db.execute(
            select(IngestionConfig).where(IngestionConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    async def _get_litellm_config(
        self,
        db: AsyncSession,
        config_id: uuid.UUID,
    ) -> LiteLLMConfig | None:
        """Fetch LiteLLM config by ID."""
        result = await db.execute(
            select(LiteLLMConfig).where(LiteLLMConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    # -------------------------------------------------------------------------
    # Cache Utilities
    # -------------------------------------------------------------------------

    def _get_from_process_cache(self, key: str) -> Any | None:
        """Get value from in-process cache if not expired."""
        if key in self._process_cache:
            expires_at, value = self._process_cache[key]
            if datetime.utcnow() < expires_at:
                return value
            del self._process_cache[key]
        return None

    def _set_in_process_cache(self, key: str, value: Any) -> None:
        """Set value in in-process cache with TTL."""
        expires_at = datetime.utcnow() + self._process_cache_ttl
        self._process_cache[key] = (expires_at, value)

    # -------------------------------------------------------------------------
    # Serialization Helpers
    # -------------------------------------------------------------------------

    def _serialize_tenant(self, tenant: Tenant) -> dict[str, Any]:
        """Serialize tenant for Redis cache."""
        return {
            "id": str(tenant.id),
            "slug": tenant.slug,
            "name": tenant.name,
            "description": tenant.description,
            "is_active": tenant.is_active,
            "rate_limit_rpm": tenant.rate_limit_rpm,
            "rate_limit_tpm": tenant.rate_limit_tpm,
        }

    def _deserialize_tenant(self, data: dict[str, Any]) -> Tenant:
        """Deserialize tenant from Redis cache."""
        # Create a detached Tenant instance (not attached to session)
        tenant = Tenant.__new__(Tenant)
        tenant.id = uuid.UUID(data["id"])
        tenant.slug = data["slug"]
        tenant.name = data["name"]
        tenant.description = data["description"]
        tenant.is_active = data["is_active"]
        tenant.rate_limit_rpm = data.get("rate_limit_rpm")
        tenant.rate_limit_tpm = data.get("rate_limit_tpm")
        return tenant

    # -------------------------------------------------------------------------
    # Cache Invalidation
    # -------------------------------------------------------------------------

    async def invalidate_tenant_cache(self, tenant_id: uuid.UUID) -> None:
        """Invalidate all cache entries for a tenant."""
        # Clear Redis cache
        await self._redis.delete(_tenant_cache_key(tenant_id))

        # Clear process cache
        keys_to_remove = [
            k for k in self._process_cache
            if k.startswith(f"tenant:{tenant_id}") or k.startswith(f"tenant_slug:")
        ]
        for key in keys_to_remove:
            del self._process_cache[key]

        log.info("Invalidated cache for tenant %s", tenant_id)

    async def invalidate_collection_cache(self, collection_id: uuid.UUID) -> None:
        """Invalidate cache for a collection."""
        await self._redis.delete(_collection_cache_key(collection_id))
        log.info("Invalidated cache for collection %s", collection_id)


# =============================================================================
# Service Factory
# =============================================================================

_tenant_config_service: TenantConfigService | None = None


def get_tenant_config_service(redis: Redis) -> TenantConfigService:
    """Get or create TenantConfigService singleton."""
    global _tenant_config_service
    if _tenant_config_service is None:
        _tenant_config_service = TenantConfigService(redis)
    return _tenant_config_service
