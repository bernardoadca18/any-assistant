/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * Authentication Service:
 * Handles API Key validation, logout, token verification, and user profile loading.
 *
 * Authentication flow:
 * 1. User provides API Key (starts with 'ue5_')
 * 2. We validate it by calling an authenticated endpoint
 * 3. If valid, we store the key and create a user session
 */

import { apiClient, STORAGE_KEYS, isApiKey } from './apiClient';
import type {
  AuthTokenPayload,
  LoginRequest,
  User,
} from '@/types';

/**
 * Validates an API Key by attempting an authenticated request.
 * Tries /admin/tenants first (requires admin), falls back to /sessions.
 */
async function validateApiKey(apiKey: string): Promise<{ valid: boolean; isAdmin: boolean; tenantSlug: string }> {
  // Temporarily store the key so the interceptor picks it up
  const previousToken = localStorage.getItem(STORAGE_KEYS.TOKEN);
  localStorage.setItem(STORAGE_KEYS.TOKEN, apiKey);

  try {
    // Try admin endpoint first
    await apiClient.get('/admin/tenants', { params: { page_size: 1 } });
    // If successful, user has admin privileges
    return { valid: true, isAdmin: true, tenantSlug: 'default' };
  } catch (err) {
    // If 403, the key is valid but not admin
    // If 401, the key is invalid
    const status = err && typeof err === 'object' && 'status' in err ? (err as { status: number }).status : 0;

    if (status === 403) {
      // Key is valid but not admin - try to get tenant info from sessions
      try {
        await apiClient.get('/sessions');
        return { valid: true, isAdmin: false, tenantSlug: 'default' };
      } catch {
        // Sessions endpoint also failed, but key is valid (403 means authenticated)
        return { valid: true, isAdmin: false, tenantSlug: 'default' };
      }
    }

    if (status === 401) {
      // Invalid credentials
      return { valid: false, isAdmin: false, tenantSlug: '' };
    }

    // Other error (network, 500, etc.) - restore previous token and rethrow
    if (previousToken) {
      localStorage.setItem(STORAGE_KEYS.TOKEN, previousToken);
    } else {
      localStorage.removeItem(STORAGE_KEYS.TOKEN);
    }
    throw err;
  }
}

/**
 * Validates credentials and initializes an authenticated session.
 * Supports API Key authentication (recommended) or development mode fallback.
 */
export async function login(request: LoginRequest): Promise<AuthTokenPayload> {
  const tenantId = request.tenant_slug || 'default';

  // 1. API Key authentication
  if (request.apiKey) {
    const apiKey = request.apiKey.trim();

    // Validate API Key format
    if (!isApiKey(apiKey)) {
      throw new Error('Formato de API Key inválido. A chave deve começar com "ue5_".');
    }

    try {
      // Validate the API Key against the backend
      const validation = await validateApiKey(apiKey);

      if (!validation.valid) {
        throw new Error('API Key inválida ou expirada. Verifique a chave e tente novamente.');
      }

      // Store the validated key
      localStorage.setItem(STORAGE_KEYS.TOKEN, apiKey);
      localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantId);

      const user: User = {
        id: `api-key-user-${Date.now()}`,
        name: request.username || 'API Key Developer',
        role: validation.isAdmin ? 'admin' : 'user',
        tenant_id: tenantId,
        active_tenant_id: tenantId,
        active_collection: 'ue5_docs',
        active_domain_id: 'godot_4',
      };

      localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user));

      return {
        token: apiKey,
        user,
      };
    } catch (err) {
      // Clean up on failure
      localStorage.removeItem(STORAGE_KEYS.TOKEN);

      // Re-throw with better message if it's a generic error
      if (err instanceof Error) {
        throw err;
      }
      throw new Error('Falha na conexão com o servidor. Verifique se o backend está acessível.');
    }
  }

  // 2. Development mode fallback (only in DEV mode with VITE_DEV_AUTH_FALLBACK)
  if (import.meta.env.DEV || import.meta.env.VITE_DEV_AUTH_FALLBACK === 'true') {
    const devUser: User = {
      id: 'dev-user-001',
      name: request.username || 'Developer (Dev Mode)',
      email: 'dev@ai.studio',
      role: 'admin',
      tenant_id: tenantId,
      active_tenant_id: tenantId,
      active_collection: 'ue5_docs',
      active_domain_id: 'godot_4',
    };
    const devToken = `dev-jwt-token-${Date.now()}`;

    localStorage.setItem(STORAGE_KEYS.TOKEN, devToken);
    localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(devUser));
    localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantId);

    return {
      token: devToken,
      user: devUser,
    };
  }

  throw new Error('Credenciais inválidas. Forneça uma API Key válida.');
}

/**
 * Logs out the active user and removes persisted session tokens.
 */
export function logout(): void {
  localStorage.removeItem(STORAGE_KEYS.TOKEN);
  localStorage.removeItem(STORAGE_KEYS.USER);
  localStorage.removeItem(STORAGE_KEYS.ACTIVE_TENANT);
  localStorage.removeItem(STORAGE_KEYS.ACTIVE_COLLECTION);
  localStorage.removeItem(STORAGE_KEYS.ACTIVE_DOMAIN);
}

/**
 * Retrieves the currently logged in User from storage or validates with backend.
 */
export async function getCurrentUser(): Promise<User | null> {
  const token = localStorage.getItem(STORAGE_KEYS.TOKEN);
  if (!token) return null;

  try {
    const userStr = localStorage.getItem(STORAGE_KEYS.USER);
    if (userStr) {
      const storedUser = JSON.parse(userStr) as User;
      return storedUser;
    }
  } catch {
    // Fallthrough to return generic user if JSON fails
  }

  return {
    id: 'active-user',
    name: 'Authenticated User',
    role: 'user',
    tenant_id: localStorage.getItem(STORAGE_KEYS.ACTIVE_TENANT) || 'default',
  };
}

/**
 * Verifies if the current session is still valid by making an authenticated request.
 */
export async function verifySession(): Promise<boolean> {
  const token = localStorage.getItem(STORAGE_KEYS.TOKEN);
  if (!token) return false;

  try {
    // Try a lightweight authenticated endpoint
    await apiClient.get('/sessions');
    return true;
  } catch (err) {
    const status = err && typeof err === 'object' && 'status' in err ? (err as { status: number }).status : 0;
    if (status === 401) {
      // Session is invalid - clear it
      logout();
      return false;
    }
    // Other errors don't necessarily mean the session is invalid
    return true;
  }
}

/**
 * Renews / refreshes the JWT token if supported by backend.
 * Note: API Keys don't support refresh - they remain valid until revoked.
 */
export async function refreshToken(): Promise<string | null> {
  const token = localStorage.getItem(STORAGE_KEYS.TOKEN);
  if (!token) return null;

  // API Keys don't support refresh
  if (isApiKey(token)) {
    return token;
  }

  try {
    const response = await apiClient.post<{ token: string }>('/auth/refresh');
    const newToken = response.data.token;
    if (newToken) {
      localStorage.setItem(STORAGE_KEYS.TOKEN, newToken);
      return newToken;
    }
  } catch {
    // Keep existing token if refresh endpoint is not supported
  }
  return token;
}
