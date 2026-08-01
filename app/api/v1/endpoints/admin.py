"""
app/api/v1/endpoints/admin.py
=============================
Admin API endpoints for tenant and collection management.

All endpoints require admin privileges (API key with is_admin=True).

Endpoints:
- POST   /admin/tenants                 — Create tenant
- GET    /admin/tenants                 — List tenants
- GET    /admin/tenants/{tenant_id}     — Get tenant details
- PATCH  /admin/tenants/{tenant_id}     — Update tenant
- DELETE /admin/tenants/{tenant_id}     — Delete tenant
- POST   /admin/tenants/{tenant_id}/api-keys — Create API key
- GET    /admin/tenants/{tenant_id}/api-keys — List API keys
- DELETE /admin/tenants/{tenant_id}/api-keys/{key_id} — Revoke API key

- POST   /admin/collections             — Create collection
- GET    /admin/collections             — List collections
- GET    /admin/collections/{collection_id} — Get collection details
- PATCH  /admin/collections/{collection_id} — Update collection
- DELETE /admin/collections/{collection_id} — Delete collection
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import (
    AdminTenant,
    AuthContext,
    CurrentTenant,
    WriteTenant,
    generate_api_key,
)
from app.db.models_tenant import (
    APIKey,
    Collection,
    CollectionConfig,
    EmbeddingConfig,
    IngestionConfig,
    LiteLLMConfig,
    RAGConfig,
    Tenant,
    TenantDefaultConfig,
)
from app.db.postgres import get_db_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# Pydantic Schemas
# =============================================================================

# --- Tenant Schemas ---

class TenantCreate(BaseModel):
    """Request to create a new tenant."""
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="")
    rate_limit_rpm: int | None = Field(default=None, ge=1)
    rate_limit_tpm: int | None = Field(default=None, ge=1)


class TenantUpdate(BaseModel):
    """Request to update a tenant."""
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    is_active: bool | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1)
    rate_limit_tpm: int | None = Field(default=None, ge=1)


class TenantResponse(BaseModel):
    """Tenant details response."""
    id: uuid.UUID
    slug: str
    name: str
    description: str
    is_active: bool
    rate_limit_rpm: int | None
    rate_limit_tpm: int | None
    collections_count: int = 0
    api_keys_count: int = 0

    class Config:
        from_attributes = True


class TenantListResponse(BaseModel):
    """Paginated tenant list response."""
    items: list[TenantResponse]
    total: int
    page: int
    page_size: int


# --- API Key Schemas ---

class APIKeyCreate(BaseModel):
    """Request to create a new API key."""
    name: str = Field(..., min_length=1, max_length=64)
    is_admin: bool = False
    is_read_only: bool = False
    expires_at: str | None = Field(default=None, description="ISO 8601 datetime")


class APIKeyResponse(BaseModel):
    """API key response (key shown only on creation)."""
    id: uuid.UUID
    name: str
    key_prefix: str
    is_admin: bool
    is_read_only: bool
    is_active: bool
    expires_at: str | None
    last_used_at: str | None
    created_at: str

    class Config:
        from_attributes = True


class APIKeyCreateResponse(APIKeyResponse):
    """API key creation response with the full key (shown once)."""
    key: str = Field(..., description="Full API key - save this, it won't be shown again")


# --- Collection Schemas ---

class CollectionCreate(BaseModel):
    """Request to create a new collection."""
    tenant_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    qdrant_collection_name: str = Field(..., min_length=1, max_length=128)
    domain_id: str = Field(default="godot_4")
    display_name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    default_project_source_filter: str | None = None


class CollectionUpdate(BaseModel):
    """Request to update a collection."""
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    is_active: bool | None = None
    default_project_source_filter: str | None = None


class CollectionResponse(BaseModel):
    """Collection details response."""
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    qdrant_collection_name: str
    domain_id: str
    display_name: str
    description: str
    is_active: bool
    default_project_source_filter: str | None
    has_custom_config: bool

    class Config:
        from_attributes = True


class CollectionListResponse(BaseModel):
    """Paginated collection list response."""
    items: list[CollectionResponse]
    total: int
    page: int
    page_size: int


# =============================================================================
# Tenant Endpoints
# =============================================================================

@router.post(
    "/tenants",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tenant",
)
async def create_tenant(
    auth: AdminTenant,
    payload: TenantCreate,
    db: AsyncSession = Depends(get_db_session),
) -> TenantResponse:
    """Create a new tenant with default configuration."""
    # Check for duplicate slug
    existing = await db.execute(
        select(Tenant).where(Tenant.slug == payload.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant with slug '{payload.slug}' already exists.",
        )

    # Create tenant
    tenant = Tenant(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        rate_limit_rpm=payload.rate_limit_rpm,
        rate_limit_tpm=payload.rate_limit_tpm,
    )
    db.add(tenant)
    await db.flush()

    # Create default config record
    default_config = TenantDefaultConfig(tenant_id=tenant.id)
    db.add(default_config)

    log.info("Created tenant: %s (%s) by %s", tenant.slug, tenant.id, auth.tenant_slug)

    return TenantResponse(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        description=tenant.description,
        is_active=tenant.is_active,
        rate_limit_rpm=tenant.rate_limit_rpm,
        rate_limit_tpm=tenant.rate_limit_tpm,
        collections_count=0,
        api_keys_count=0,
    )


@router.get(
    "/tenants",
    response_model=TenantListResponse,
    summary="List all tenants",
)
async def list_tenants(
    auth: AdminTenant,
    db: AsyncSession = Depends(get_db_session),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    is_active: bool | None = None,
) -> TenantListResponse:
    """List all tenants with pagination."""
    # Build query
    query = select(Tenant)
    count_query = select(func.count(Tenant.id))

    if is_active is not None:
        query = query.where(Tenant.is_active == is_active)
        count_query = count_query.where(Tenant.is_active == is_active)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = query.order_by(Tenant.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    tenants = result.scalars().all()

    # Get counts for each tenant
    items = []
    for tenant in tenants:
        # Count collections
        coll_count = await db.execute(
            select(func.count(Collection.id)).where(Collection.tenant_id == tenant.id)
        )
        collections_count = coll_count.scalar() or 0

        # Count API keys
        key_count = await db.execute(
            select(func.count(APIKey.id)).where(APIKey.tenant_id == tenant.id)
        )
        api_keys_count = key_count.scalar() or 0

        items.append(TenantResponse(
            id=tenant.id,
            slug=tenant.slug,
            name=tenant.name,
            description=tenant.description,
            is_active=tenant.is_active,
            rate_limit_rpm=tenant.rate_limit_rpm,
            rate_limit_tpm=tenant.rate_limit_tpm,
            collections_count=collections_count,
            api_keys_count=api_keys_count,
        ))

    return TenantListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    summary="Get tenant details",
)
async def get_tenant(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> TenantResponse:
    """Get detailed information about a tenant."""
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {tenant_id} not found.",
        )

    # Count collections and API keys
    coll_count = await db.execute(
        select(func.count(Collection.id)).where(Collection.tenant_id == tenant.id)
    )
    collections_count = coll_count.scalar() or 0

    key_count = await db.execute(
        select(func.count(APIKey.id)).where(APIKey.tenant_id == tenant.id)
    )
    api_keys_count = key_count.scalar() or 0

    return TenantResponse(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        description=tenant.description,
        is_active=tenant.is_active,
        rate_limit_rpm=tenant.rate_limit_rpm,
        rate_limit_tpm=tenant.rate_limit_tpm,
        collections_count=collections_count,
        api_keys_count=api_keys_count,
    )


@router.patch(
    "/tenants/{tenant_id}",
    response_model=TenantResponse,
    summary="Update tenant",
)
async def update_tenant(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    payload: TenantUpdate,
    db: AsyncSession = Depends(get_db_session),
) -> TenantResponse:
    """Update tenant properties."""
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {tenant_id} not found.",
        )

    # Apply updates
    if payload.name is not None:
        tenant.name = payload.name
    if payload.description is not None:
        tenant.description = payload.description
    if payload.is_active is not None:
        tenant.is_active = payload.is_active
    if payload.rate_limit_rpm is not None:
        tenant.rate_limit_rpm = payload.rate_limit_rpm
    if payload.rate_limit_tpm is not None:
        tenant.rate_limit_tpm = payload.rate_limit_tpm

    log.info("Updated tenant %s by %s", tenant.slug, auth.tenant_slug)

    # Count for response
    coll_count = await db.execute(
        select(func.count(Collection.id)).where(Collection.tenant_id == tenant.id)
    )
    collections_count = coll_count.scalar() or 0

    key_count = await db.execute(
        select(func.count(APIKey.id)).where(APIKey.tenant_id == tenant.id)
    )
    api_keys_count = key_count.scalar() or 0

    return TenantResponse(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        description=tenant.description,
        is_active=tenant.is_active,
        rate_limit_rpm=tenant.rate_limit_rpm,
        rate_limit_tpm=tenant.rate_limit_tpm,
        collections_count=collections_count,
        api_keys_count=api_keys_count,
    )


@router.delete(
    "/tenants/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete tenant",
)
async def delete_tenant(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
):
    """Delete a tenant and all associated resources."""
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {tenant_id} not found.",
        )

    # Prevent deleting default tenant
    if tenant.slug == "default":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the default tenant.",
        )

    await db.delete(tenant)
    log.warning("Deleted tenant %s by %s", tenant.slug, auth.tenant_slug)


# =============================================================================
# API Key Endpoints
# =============================================================================

@router.post(
    "/tenants/{tenant_id}/api-keys",
    response_model=APIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create API key for tenant",
)
async def create_api_key(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    payload: APIKeyCreate,
    db: AsyncSession = Depends(get_db_session),
) -> APIKeyCreateResponse:
    """Create a new API key for a tenant."""
    # Verify tenant exists
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {tenant_id} not found.",
        )

    # Check for duplicate name
    existing = await db.execute(
        select(APIKey).where(
            APIKey.tenant_id == tenant_id,
            APIKey.name == payload.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"API key '{payload.name}' already exists for this tenant.",
        )

    # Generate key
    full_key, key_prefix, key_hash = generate_api_key()

    # Parse expiration
    expires_at = None
    if payload.expires_at:
        from datetime import datetime
        try:
            expires_at = datetime.fromisoformat(payload.expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid expires_at format. Use ISO 8601.",
            )

    # Create key record
    api_key = APIKey(
        tenant_id=tenant_id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        is_admin=payload.is_admin,
        is_read_only=payload.is_read_only,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.flush()

    log.info("Created API key '%s' for tenant %s by %s", payload.name, tenant.slug, auth.tenant_slug)

    return APIKeyCreateResponse(
        id=api_key.id,
        name=api_key.name,
        key=full_key,  # Only shown once!
        key_prefix=api_key.key_prefix,
        is_admin=api_key.is_admin,
        is_read_only=api_key.is_read_only,
        is_active=api_key.is_active,
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        last_used_at=None,
        created_at=api_key.created_at.isoformat(),
    )


@router.get(
    "/tenants/{tenant_id}/api-keys",
    response_model=list[APIKeyResponse],
    summary="List API keys for tenant",
)
async def list_api_keys(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> list[APIKeyResponse]:
    """List all API keys for a tenant."""
    result = await db.execute(
        select(APIKey)
        .where(APIKey.tenant_id == tenant_id)
        .order_by(APIKey.created_at.desc())
    )
    keys = result.scalars().all()

    return [
        APIKeyResponse(
            id=key.id,
            name=key.name,
            key_prefix=key.key_prefix,
            is_admin=key.is_admin,
            is_read_only=key.is_read_only,
            is_active=key.is_active,
            expires_at=key.expires_at.isoformat() if key.expires_at else None,
            last_used_at=key.last_used_at.isoformat() if key.last_used_at else None,
            created_at=key.created_at.isoformat(),
        )
        for key in keys
    ]


@router.delete(
    "/tenants/{tenant_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke API key",
)
async def revoke_api_key(
    auth: AdminTenant,
    tenant_id: uuid.UUID,
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
):
    """Revoke (delete) an API key."""
    result = await db.execute(
        select(APIKey).where(
            APIKey.id == key_id,
            APIKey.tenant_id == tenant_id,
        )
    )
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found.",
        )

    await db.delete(api_key)
    log.info("Revoked API key '%s' for tenant %s by %s", api_key.name, tenant_id, auth.tenant_slug)


# =============================================================================
# Collection Endpoints
# =============================================================================

@router.post(
    "/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create collection",
)
async def create_collection(
    auth: AdminTenant,
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db_session),
) -> CollectionResponse:
    """Create a new collection within a tenant."""
    # Verify tenant exists
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == payload.tenant_id)
    )
    tenant = tenant_result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant {payload.tenant_id} not found.",
        )

    # Check for duplicate name within tenant
    existing = await db.execute(
        select(Collection).where(
            Collection.tenant_id == payload.tenant_id,
            Collection.name == payload.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Collection '{payload.name}' already exists in this tenant.",
        )

    # Check for duplicate Qdrant collection name globally
    existing_qdrant = await db.execute(
        select(Collection).where(Collection.qdrant_collection_name == payload.qdrant_collection_name)
    )
    if existing_qdrant.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Qdrant collection '{payload.qdrant_collection_name}' already in use.",
        )

    # Validate domain exists
    from app.core.domains.registry import domain_registry
    try:
        domain_registry.get(payload.domain_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown domain_id: {payload.domain_id}",
        )

    # Create collection
    collection = Collection(
        tenant_id=payload.tenant_id,
        name=payload.name,
        qdrant_collection_name=payload.qdrant_collection_name,
        domain_id=payload.domain_id,
        display_name=payload.display_name,
        description=payload.description,
        default_project_source_filter=payload.default_project_source_filter,
    )
    db.add(collection)
    await db.flush()

    log.info(
        "Created collection '%s' (%s) for tenant %s by %s",
        collection.name, collection.qdrant_collection_name, tenant.slug, auth.tenant_slug
    )

    return CollectionResponse(
        id=collection.id,
        tenant_id=collection.tenant_id,
        name=collection.name,
        qdrant_collection_name=collection.qdrant_collection_name,
        domain_id=collection.domain_id,
        display_name=collection.display_name,
        description=collection.description,
        is_active=collection.is_active,
        default_project_source_filter=collection.default_project_source_filter,
        has_custom_config=False,
    )


@router.get(
    "/collections",
    response_model=CollectionListResponse,
    summary="List collections",
)
async def list_collections(
    auth: AdminTenant,
    db: AsyncSession = Depends(get_db_session),
    tenant_id: uuid.UUID | None = None,
    domain_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> CollectionListResponse:
    """List collections with optional filtering."""
    # Build query
    query = select(Collection)
    count_query = select(func.count(Collection.id))

    if tenant_id:
        query = query.where(Collection.tenant_id == tenant_id)
        count_query = count_query.where(Collection.tenant_id == tenant_id)

    if domain_id:
        query = query.where(Collection.domain_id == domain_id)
        count_query = count_query.where(Collection.domain_id == domain_id)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = query.order_by(Collection.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    collections = result.scalars().all()

    items = []
    for coll in collections:
        # Check for custom config
        config_result = await db.execute(
            select(CollectionConfig).where(CollectionConfig.collection_id == coll.id)
        )
        has_config = config_result.scalar_one_or_none() is not None

        items.append(CollectionResponse(
            id=coll.id,
            tenant_id=coll.tenant_id,
            name=coll.name,
            qdrant_collection_name=coll.qdrant_collection_name,
            domain_id=coll.domain_id,
            display_name=coll.display_name,
            description=coll.description,
            is_active=coll.is_active,
            default_project_source_filter=coll.default_project_source_filter,
            has_custom_config=has_config,
        ))

    return CollectionListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    summary="Get collection details",
)
async def get_collection(
    auth: AdminTenant,
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> CollectionResponse:
    """Get detailed information about a collection."""
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id)
    )
    collection = result.scalar_one_or_none()

    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection {collection_id} not found.",
        )

    # Check for custom config
    config_result = await db.execute(
        select(CollectionConfig).where(CollectionConfig.collection_id == collection.id)
    )
    has_config = config_result.scalar_one_or_none() is not None

    return CollectionResponse(
        id=collection.id,
        tenant_id=collection.tenant_id,
        name=collection.name,
        qdrant_collection_name=collection.qdrant_collection_name,
        domain_id=collection.domain_id,
        display_name=collection.display_name,
        description=collection.description,
        is_active=collection.is_active,
        default_project_source_filter=collection.default_project_source_filter,
        has_custom_config=has_config,
    )


@router.patch(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    summary="Update collection",
)
async def update_collection(
    auth: AdminTenant,
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    db: AsyncSession = Depends(get_db_session),
) -> CollectionResponse:
    """Update collection properties."""
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id)
    )
    collection = result.scalar_one_or_none()

    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection {collection_id} not found.",
        )

    # Apply updates
    if payload.display_name is not None:
        collection.display_name = payload.display_name
    if payload.description is not None:
        collection.description = payload.description
    if payload.is_active is not None:
        collection.is_active = payload.is_active
    if payload.default_project_source_filter is not None:
        collection.default_project_source_filter = payload.default_project_source_filter

    log.info("Updated collection '%s' by %s", collection.name, auth.tenant_slug)

    # Check for custom config
    config_result = await db.execute(
        select(CollectionConfig).where(CollectionConfig.collection_id == collection.id)
    )
    has_config = config_result.scalar_one_or_none() is not None

    return CollectionResponse(
        id=collection.id,
        tenant_id=collection.tenant_id,
        name=collection.name,
        qdrant_collection_name=collection.qdrant_collection_name,
        domain_id=collection.domain_id,
        display_name=collection.display_name,
        description=collection.description,
        is_active=collection.is_active,
        default_project_source_filter=collection.default_project_source_filter,
        has_custom_config=has_config,
    )


@router.delete(
    "/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete collection",
)
async def delete_collection(
    auth: AdminTenant,
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
):
    """Delete a collection."""
    result = await db.execute(
        select(Collection).where(Collection.id == collection_id)
    )
    collection = result.scalar_one_or_none()

    if not collection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection {collection_id} not found.",
        )

    await db.delete(collection)
    log.warning("Deleted collection '%s' by %s", collection.name, auth.tenant_slug)
