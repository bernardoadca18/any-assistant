"""
app/core/auth.py
================
Authentication and authorization infrastructure for multi-tenant API.

Supports:
- API Key authentication (primary)
- JWT Bearer token authentication (future/optional)
- Tenant context extraction and injection
- Rate limiting hooks

Security considerations:
- API keys are stored as bcrypt hashes (cost factor 12)
- Keys are never logged or returned in responses
- Key prefix (first 8 chars) is stored for identification
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import bcrypt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_tenant import APIKey, Tenant
from app.db.postgres import get_db_session
from app.db.redis import get_redis_client

log = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

API_KEY_HEADER_NAME = "X-API-Key"
API_KEY_PREFIX = "ue5_"  # Keys are prefixed with this for identification
API_KEY_LENGTH = 32  # Random bytes (generates 64-char hex string)
BCOST_COST_FACTOR = 12  # bcrypt cost factor for hashing


# =============================================================================
# Security Schemes
# =============================================================================

# API Key security scheme
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, scheme_name="api_key", auto_error=False)

# JWT Bearer security scheme (for future use)
bearer_scheme = HTTPBearer(auto_error=False)


# =============================================================================
# Auth Context
# =============================================================================

@dataclass(frozen=True)
class AuthContext:
    """
    Authenticated tenant context extracted from request.

    This is the principal object injected into endpoints via dependency.
    """
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_name: str
    api_key_id: uuid.UUID | None
    api_key_name: str | None
    is_admin: bool
    is_read_only: bool

    # Rate limiting (from tenant settings)
    rate_limit_rpm: int | None
    rate_limit_tpm: int | None


class AuthenticatedTenant(BaseModel):
    """Pydantic model for tenant info in request state."""
    id: uuid.UUID
    slug: str
    name: str


# =============================================================================
# API Key Generation and Hashing
# =============================================================================

def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        Tuple of (full_key, key_prefix, key_hash)
        - full_key: The complete key to show to user ONCE (e.g., "ue5_a1b2c3d4...")
        - key_prefix: First 8 chars for identification in DB
        - key_hash: bcrypt hash for storage
    """
    # Generate random key
    random_part = secrets.token_hex(API_KEY_LENGTH)
    full_key = f"{API_KEY_PREFIX}{random_part}"

    # Extract prefix for identification
    key_prefix = full_key[:8]

    # Hash with bcrypt
    key_hash = bcrypt.hashpw(full_key.encode("utf-8"), bcrypt.gensalt(rounds=BCOST_COST_FACTOR))
    key_hash_str = key_hash.decode("utf-8")

    return full_key, key_prefix, key_hash_str


def verify_api_key(provided_key: str, key_hash: str) -> bool:
    """
    Verify an API key against its stored hash.

    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        return bcrypt.checkpw(provided_key.encode("utf-8"), key_hash.encode("utf-8"))
    except Exception as e:
        log.warning("API key verification error: %s", e)
        return False


# =============================================================================
# Authentication Dependencies
# =============================================================================

async def get_current_tenant(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> AuthContext:
    """
    Extract and validate tenant context from request.

    Authentication priority:
    1. API Key (X-API-Key header)
    2. JWT Bearer token (Authorization header) - future
    3. X-Tenant-ID header (for development/admin only, requires trusted network)

    Returns:
        AuthContext with tenant information

    Raises:
        HTTPException 401: Invalid or missing credentials
        HTTPException 403: Tenant is inactive or key expired
    """
    # Get dependencies internally to avoid OpenAPI schema pollution
    from app.db.postgres import get_db_session
    from app.db.redis import get_redis_client

    # Skip auth for health endpoints
    if request.url.path.endswith("/health"):
        # Return default tenant for health checks
        return _get_default_tenant_context()

    # Get API key and authorization headers directly from request
    api_key = request.headers.get("X-API-Key")
    authorization_header = request.headers.get("Authorization")

    # Get db and redis from app state or create new connections
    db = None
    redis = None

    # Try to get from app state (initialized in lifespan)
    from app.db.postgres import get_session_factory
    from app.db.redis import get_redis_client

    try:
        factory = get_session_factory()
        db = factory()
    except RuntimeError:
        # DB not initialized, will fail later if needed
        pass

    try:
        redis = get_redis_client()
    except RuntimeError:
        # Redis not available, will fail later if needed
        pass

    # Try API Key authentication
    if api_key:
        if db is None or redis is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database or Redis not available.",
            )
        async with db:
            try:
                result = await _authenticate_via_api_key(api_key, db, redis, request)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    # Try JWT Bearer authentication (future implementation)
    if authorization_header and authorization_header.startswith("Bearer "):
        if db is None or redis is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database or Redis not available.",
            )
        from fastapi.security import HTTPAuthorizationCredentials
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=authorization_header[7:]
        )
        return await _authenticate_via_jwt(credentials, db, redis)

    # Try X-Tenant-ID header (development/admin mode)
    if x_tenant_id:
        if db is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database not available.",
            )
        async with db:
            try:
                result = await _authenticate_via_tenant_id(x_tenant_id, db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    # No valid authentication provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication credentials. Provide X-API-Key header.",
        headers={"WWW-Authenticate": f'ApiKey realm="API", charset="UTF-8"'},
    )


async def get_optional_tenant(
    request: Request,
    api_key: Annotated[str | None, Depends(api_key_scheme)] = None,
    authorization: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    db: Annotated[AsyncSession, Depends(get_db_session)] = None,
    redis: Annotated[Redis, Depends(get_redis_client)] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> AuthContext | None:
    """
    Extract tenant context if provided, but don't require it.

    Used for endpoints that work with or without tenant context
    (e.g., public documentation, health checks).
    """
    try:
        return await get_current_tenant(request, api_key, authorization, db, redis, x_tenant_id)
    except HTTPException:
        return None


def require_admin(
    auth: Annotated[AuthContext, Depends(get_current_tenant)]
) -> AuthContext:
    """
    Dependency that requires admin privileges.

    Raises:
        HTTPException 403: Not an admin
    """
    if not auth.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation.",
        )
    return auth


def require_write_access(
    auth: Annotated[AuthContext, Depends(get_current_tenant)]
) -> AuthContext:
    """
    Dependency that requires write access (not read-only).

    Raises:
        HTTPException 403: Read-only access
    """
    if auth.is_read_only:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required. API key is read-only.",
        )
    return auth


# =============================================================================
# Authentication Implementations
# =============================================================================

async def _authenticate_via_api_key(
    api_key: str,
    db: AsyncSession,
    redis: Redis,
    request: Request,
) -> AuthContext:
    """Authenticate via API key."""

    # Validate key format
    if not api_key.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format.",
        )

    # Extract prefix for lookup
    key_prefix = api_key[:8]

    # Look up key by prefix
    result = await db.execute(
        select(APIKey).where(
            APIKey.key_prefix == key_prefix,
            APIKey.is_active == True,
        )
    )
    api_key_record = result.scalar_one_or_none()

    if api_key_record is None:
        log.warning("API key not found or inactive: prefix=%s", key_prefix)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    # Verify key hash
    if not verify_api_key(api_key, api_key_record.key_hash):
        log.warning(
            "API key verification failed: tenant=%s key=%s",
            api_key_record.tenant_id, api_key_record.name
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    # Check expiration
    if api_key_record.expires_at and api_key_record.expires_at < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired.",
        )

    # Fetch tenant
    tenant_result = await db.execute(
        select(Tenant).where(Tenant.id == api_key_record.tenant_id)
    )
    tenant = tenant_result.scalar_one_or_none()

    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant not found or inactive.",
        )

    # Update last used tracking (async, don't wait)
    await _update_key_usage(db, api_key_record, request)

    log.info(
        "Authenticated tenant via API key: tenant=%s key=%s",
        tenant.slug, api_key_record.name
    )

    return AuthContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tenant_name=tenant.name,
        api_key_id=api_key_record.id,
        api_key_name=api_key_record.name,
        is_admin=api_key_record.is_admin,
        is_read_only=api_key_record.is_read_only,
        rate_limit_rpm=tenant.rate_limit_rpm,
        rate_limit_tpm=tenant.rate_limit_tpm,
    )


async def _authenticate_via_jwt(
    credentials: HTTPAuthorizationCredentials,
    db: AsyncSession,
    redis: Redis,
) -> AuthContext:
    """Authenticate via JWT Bearer token (future implementation)."""
    # TODO: Implement JWT authentication
    # For now, return not implemented
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="JWT authentication not yet implemented. Use API key.",
        headers={"WWW-Authenticate": 'Bearer realm="API", charset="UTF-8"'},
    )


async def _authenticate_via_tenant_id(
    tenant_id_str: str,
    db: AsyncSession,
) -> AuthContext:
    """
    Authenticate via X-Tenant-ID header.

    WARNING: This should only be enabled in development mode or
    behind a trusted network. It bypasses API key validation.
    """
    # Only allow in development
    from app.core.config import settings
    if not settings.is_development:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-ID authentication only available in development mode.",
        )

    try:
        tenant_id = uuid.UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tenant ID format.",
        )

    # Fetch tenant
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id)
    )
    tenant = result.scalar_one_or_none()

    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant not found or inactive.",
        )

    log.warning(
        "DEV MODE: Authenticated tenant via X-Tenant-ID header: tenant=%s",
        tenant.slug
    )

    return AuthContext(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tenant_name=tenant.name,
        api_key_id=None,
        api_key_name=None,
        is_admin=True,  # Grant admin in dev mode
        is_read_only=False,
        rate_limit_rpm=tenant.rate_limit_rpm,
        rate_limit_tpm=tenant.rate_limit_tpm,
    )


def _get_default_tenant_context() -> AuthContext:
    """Return default tenant context for health checks."""
    return AuthContext(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        tenant_slug="default",
        tenant_name="Default Tenant",
        api_key_id=None,
        api_key_name=None,
        is_admin=False,
        is_read_only=False,
        rate_limit_rpm=None,
        rate_limit_tpm=None,
    )


async def _update_key_usage(
    db: AsyncSession,
    api_key_record: APIKey,
    request: Request,
) -> None:
    """Update last used timestamp and IP for an API key."""
    try:
        api_key_record.last_used_at = datetime.utcnow()

        # Extract client IP (handle X-Forwarded-For)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else None

        if client_ip:
            from ipaddress import ip_address
            api_key_record.last_used_ip = ip_address(client_ip)

        await db.flush()
    except Exception as e:
        log.warning("Failed to update key usage tracking: %s", e)


# =============================================================================
# Type Aliases for Cleaner Dependencies
# =============================================================================

# Use these in endpoint signatures for cleaner code
CurrentTenant = Annotated[AuthContext, Depends(get_current_tenant)]
OptionalTenant = Annotated[AuthContext | None, Depends(get_optional_tenant)]
AdminTenant = Annotated[AuthContext, Depends(require_admin)]
WriteTenant = Annotated[AuthContext, Depends(require_write_access)]
