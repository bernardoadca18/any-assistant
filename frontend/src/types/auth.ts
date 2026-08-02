/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Authentication and User Session types for JWT & API Key Multi-Tenant access.
 */

export type UserRole = 'admin' | 'user';

export interface User {
  id: string;
  name: string;
  email?: string;
  role: UserRole;
  tenant_id: string;
  active_tenant_id?: string;
  active_collection?: string;
  active_domain_id?: string;
}

export interface LoginRequest {
  apiKey?: string;
  username?: string;
  password?: string;
  tenant_slug?: string;
}

export interface AuthTokenPayload {
  token: string;
  expires_at?: string;
  user: User;
}

export interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
}
