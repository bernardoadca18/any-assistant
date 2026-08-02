/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Tenant & Collection Service:
 * Manages Multi-Tenant namespaces, Qdrant Collections, API Keys,
 * and switching active tenant/collection contexts.
 */

import { apiClient, STORAGE_KEYS } from './apiClient';
import type {
  APIKeyCreate,
  APIKeyCreateResponse,
  APIKeyResponse,
  CollectionCreate,
  CollectionListResponse,
  CollectionResponse,
  CollectionUpdate,
  PaginatedQueryParams,
  TenantCreate,
  TenantListResponse,
  TenantResponse,
  TenantUpdate,
} from '@/types';

// ============================================================================
// TENANT OPERATIONS
// ============================================================================

/**
 * Lists all available tenants with pagination and optional active filter.
 */
export async function listTenants(params: PaginatedQueryParams = {}): Promise<TenantListResponse> {
  const { page = 1, page_size = 20, is_active } = params;
  const response = await apiClient.get<TenantListResponse>('/admin/tenants', {
    params: {
      page,
      page_size,
      ...(is_active !== undefined ? { is_active } : {}),
    },
  });
  return response.data;
}

/**
 * Creates a new tenant with slug, name, description, and rate limits.
 */
export async function createTenant(payload: TenantCreate): Promise<TenantResponse> {
  const response = await apiClient.post<TenantResponse>('/admin/tenants', payload);
  return response.data;
}

/**
 * Retrieves detailed information about a specific tenant.
 */
export async function getTenant(tenantId: string): Promise<TenantResponse> {
  const response = await apiClient.get<TenantResponse>(`/admin/tenants/${tenantId}`);
  return response.data;
}

/**
 * Updates tenant properties (name, description, active status, rate limits).
 */
export async function updateTenant(tenantId: string, payload: TenantUpdate): Promise<TenantResponse> {
  const response = await apiClient.patch<TenantResponse>(`/admin/tenants/${tenantId}`, payload);
  return response.data;
}

/**
 * Deletes a tenant and its associated resources.
 */
export async function deleteTenant(tenantId: string): Promise<void> {
  await apiClient.delete(`/admin/tenants/${tenantId}`);
}

// ============================================================================
// COLLECTION / NAMESPACE OPERATIONS
// ============================================================================

/**
 * Lists Qdrant collections with optional filtering by tenant_id or domain_id.
 */
export async function listCollections(params: {
  tenant_id?: string;
  domain_id?: string;
  page?: number;
  page_size?: number;
} = {}): Promise<CollectionListResponse> {
  const { page = 1, page_size = 20, tenant_id, domain_id } = params;
  const response = await apiClient.get<CollectionListResponse>('/admin/collections', {
    params: {
      page,
      page_size,
      ...(tenant_id ? { tenant_id } : {}),
      ...(domain_id ? { domain_id } : {}),
    },
  });
  return response.data;
}

/**
 * Creates a new Qdrant vector collection within a tenant namespace.
 */
export async function createCollection(payload: CollectionCreate): Promise<CollectionResponse> {
  const response = await apiClient.post<CollectionResponse>('/admin/collections', payload);
  return response.data;
}

/**
 * Retrieves detailed configuration for a collection.
 */
export async function getCollection(collectionId: string): Promise<CollectionResponse> {
  const response = await apiClient.get<CollectionResponse>(`/admin/collections/${collectionId}`);
  return response.data;
}

/**
 * Updates collection display name, description, active state, or default filter.
 */
export async function updateCollection(
  collectionId: string,
  payload: CollectionUpdate
): Promise<CollectionResponse> {
  const response = await apiClient.patch<CollectionResponse>(
    `/admin/collections/${collectionId}`,
    payload
  );
  return response.data;
}

/**
 * Deletes a Qdrant collection.
 */
export async function deleteCollection(collectionId: string): Promise<void> {
  await apiClient.delete(`/admin/collections/${collectionId}`);
}

// ============================================================================
// API KEY MANAGEMENT
// ============================================================================

/**
 * Lists API Keys created for a specific tenant.
 */
export async function listApiKeys(tenantId: string): Promise<APIKeyResponse[]> {
  const response = await apiClient.get<APIKeyResponse[]>(`/admin/tenants/${tenantId}/api-keys`);
  return response.data;
}

/**
 * Creates a new API Key for a tenant.
 * Note: The full key is only returned once in the APIKeyCreateResponse.key field.
 */
export async function createApiKey(
  tenantId: string,
  payload: APIKeyCreate
): Promise<APIKeyCreateResponse> {
  const response = await apiClient.post<APIKeyCreateResponse>(
    `/admin/tenants/${tenantId}/api-keys`,
    payload
  );
  return response.data;
}

/**
 * Revokes (deletes) an API Key for a tenant.
 */
export async function revokeApiKey(tenantId: string, keyId: string): Promise<void> {
  await apiClient.delete(`/admin/tenants/${tenantId}/api-keys/${keyId}`);
}

// ============================================================================
// ACTIVE TENANT & COLLECTION CONTEXT SWITCHING
// ============================================================================

/**
 * Switches the active Tenant ID in localStorage and API Client headers.
 */
export function setActiveTenant(tenantId: string): void {
  localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantId);
}

/**
 * Switches the active Collection name in localStorage.
 */
export function setActiveCollection(collectionName: string): void {
  localStorage.setItem(STORAGE_KEYS.ACTIVE_COLLECTION, collectionName);
}

/**
 * Switches the active Domain ID (e.g., 'godot_4' or 'ue5').
 */
export function setActiveDomain(domainId: string): void {
  localStorage.setItem(STORAGE_KEYS.ACTIVE_DOMAIN, domainId);
}
