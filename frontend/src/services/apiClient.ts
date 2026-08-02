/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Centralized Axios API Client instance with request & response interceptors:
 * - Configurable baseURL via VITE_API_BASE_URL
 * - Automatically attaches JWT Bearer token (Authorization) or API Key
 * - Automatically attaches multi-tenant headers: X-Tenant-ID and X-Domain-ID
 * - Centralized response error handling (401 global unauthorized event, Pydantic error formatting)
 */

import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { getApiBaseUrl } from '@/config/links';
import type { ApiErrorResponse, ValidationError } from '@/types';

// Constants for LocalStorage persistence keys
export const STORAGE_KEYS = {
  TOKEN: 'ue5_rag_auth_token',
  USER: 'ue5_rag_user',
  ACTIVE_TENANT: 'ue5_rag_active_tenant',
  ACTIVE_COLLECTION: 'ue5_rag_active_collection',
  ACTIVE_DOMAIN: 'ue5_rag_active_domain',
} as const;

// Event emitted when a 401 Unauthorized response is intercepted
export const UNAUTHORIZED_EVENT = 'auth:unauthorized';

/**
 * Creates and configures the singleton Axios instance for backend communication.
 */
export const apiClient = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 45000,
  headers: {
    'Content-Type': 'application/json',
  },
});

/**
 * Retrieves the currently active JWT token or API Key from localStorage.
 */
export function getStoredToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEYS.TOKEN);
  } catch {
    return null;
  }
}

/**
 * Retrieves the currently selected Tenant ID (e.g. 'ue5-official' or 'default').
 */
export function getStoredTenantId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACTIVE_TENANT) || 'default';
  } catch {
    return 'default';
  }
}

/**
 * Retrieves the currently selected Domain ID (e.g. 'ue5' or 'godot_4').
 */
export function getStoredDomainId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACTIVE_DOMAIN) || 'godot_4';
  } catch {
    return 'godot_4';
  }
}

/**
 * Returns complete Headers object for native Fetch API calls (e.g., SSE streaming).
 */
export function getAuthHeaders(additionalHeaders: Record<string, string> = {}): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...additionalHeaders,
  };

  const token = getStoredToken();
  if (token) {
    // Supports either standard JWT Bearer token or custom API key
    if (token.startsWith('Bearer ') || token.length > 50) {
      headers['Authorization'] = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
    } else {
      headers['X-API-Key'] = token;
      headers['Authorization'] = `Bearer ${token}`;
    }
  }

  const tenantId = getStoredTenantId();
  if (tenantId) {
    headers['X-Tenant-ID'] = tenantId;
  }

  const domainId = getStoredDomainId();
  if (domainId) {
    headers['X-Domain-ID'] = domainId;
  }

  return headers;
}

// ============================================================================
// REQUEST INTERCEPTOR
// Automatically injects Authorization token and X-Tenant-ID / X-Domain-ID headers
// ============================================================================
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = getStoredToken();
    if (token && config.headers) {
      config.headers.Authorization = token.startsWith('Bearer ') ? token : `Bearer ${token}`;
    }

    const tenantId = getStoredTenantId();
    if (tenantId && config.headers) {
      config.headers['X-Tenant-ID'] = tenantId;
    }

    const domainId = getStoredDomainId();
    if (domainId && config.headers) {
      config.headers['X-Domain-ID'] = domainId;
    }

    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// ============================================================================
// RESPONSE INTERCEPTOR
// Centralized error handling, 401 Unauthorized dispatching, and error formatting
// ============================================================================
apiClient.interceptors.response.use(
  (response) => {
    return response;
  },
  (error: AxiosError) => {
    const status = error.response?.status || 500;
    const data = error.response?.data as Record<string, unknown> | undefined;

    // Dispatch global event on 401 Unauthorized so AppStore can clear session
    if (status === 401) {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { status, data } }));
    }

    // Parse Pydantic validation errors or standard detail messages
    let formattedMessage = error.message || 'An unexpected error occurred.';
    if (data?.detail) {
      if (typeof data.detail === 'string') {
        formattedMessage = data.detail;
      } else if (Array.isArray(data.detail)) {
        // Handle Pydantic HTTPValidationError loc + msg array
        const validationErrors = data.detail as ValidationError[];
        formattedMessage = validationErrors
          .map((err) => `${err.loc.join('.')}: ${err.msg}`)
          .join(' | ');
      }
    }

    const enhancedError: ApiErrorResponse = {
      status,
      message: formattedMessage,
      detail: data?.detail as string | ValidationError[] | undefined,
      code: error.code,
    };

    return Promise.reject(enhancedError);
  }
);
