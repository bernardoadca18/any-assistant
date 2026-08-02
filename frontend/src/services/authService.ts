/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Authentication Service:
 * Handles JWT and API Key login, logout, token verification, and user profile loading.
 */

import { apiClient, STORAGE_KEYS } from './apiClient';
import type {
  AuthTokenPayload,
  LoginRequest,
  User,
} from '@/types';

/**
 * Validates credentials and initializes an authenticated session.
 * Supports API Key authentication or JWT Bearer Token exchange.
 */
export async function login(request: LoginRequest): Promise<AuthTokenPayload> {
  const tenantId = request.tenant_slug || 'default';

  // 1. If API Key is provided, validate it against the backend health/sessions
  if (request.apiKey) {
    const apiKey = request.apiKey.trim();
    
    // Store temporarily in localStorage so headers attach to verify call
    localStorage.setItem(STORAGE_KEYS.TOKEN, apiKey);
    localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantId);

    try {
      // Verify connectivity and authorization by calling health check
      await apiClient.get('/health');
    } catch (err) {
      localStorage.removeItem(STORAGE_KEYS.TOKEN);
      throw new Error('Falha na autenticação: API Key inválida ou sem permissão para o tenant informado.');
    }

    const user: User = {
      id: 'api-key-user',
      name: request.username || 'API Key Developer',
      role: 'admin',
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
  }

  // 2. Standard JWT Login Endpoint (if username / password provided)
  try {
    const response = await apiClient.post<AuthTokenPayload>('/auth/token', {
      username: request.username,
      password: request.password,
      tenant_slug: tenantId,
    });

    const payload = response.data;
    localStorage.setItem(STORAGE_KEYS.TOKEN, payload.token);
    localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(payload.user));
    localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, payload.user.tenant_id || tenantId);

    return payload;
  } catch (err) {
    // If standard /auth/token endpoint isn't enabled yet, support Dev Mode login
    if (import.meta.env.DEV || import.meta.env.VITE_DEV_AUTH_FALLBACK === 'true') {
      const devUser: User = {
        id: 'dev-user-001',
        name: request.username || 'UE5 Developer (Dev Mode)',
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
    throw err;
  }
}

/**
 * Logs out the active user and removes persisted session tokens.
 */
export function logout(): void {
  localStorage.removeItem(STORAGE_KEYS.TOKEN);
  localStorage.removeItem(STORAGE_KEYS.USER);
  localStorage.removeItem(STORAGE_KEYS.ACTIVE_TENANT);
  localStorage.removeItem(STORAGE_KEYS.ACTIVE_COLLECTION);
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
    role: 'admin',
    tenant_id: localStorage.getItem(STORAGE_KEYS.ACTIVE_TENANT) || 'default',
  };
}

/**
 * Renews / refreshes the JWT token if supported by backend.
 */
export async function refreshToken(): Promise<string | null> {
  const token = localStorage.getItem(STORAGE_KEYS.TOKEN);
  if (!token) return null;

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
