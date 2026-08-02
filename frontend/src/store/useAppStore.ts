/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Global App State Manager (Zustand):
 * - Auth & User Session (token, user, error)
 * - Multi-Tenant context (activeTenant, activeCollection, activeDomain)
 * - Cached registries (tenantsList, collectionsList)
 * - Developer Hub System Health (systemHealth, checkSystemHealth)
 * - Persistent synchronization with localStorage
 */

import { create } from 'zustand';
import {
  STORAGE_KEYS,
  UNAUTHORIZED_EVENT,
  apiClient,
} from '@/services/apiClient';
import * as authService from '@/services/authService';
import * as tenantService from '@/services/tenantService';
import type {
  CollectionResponse,
  HealthResponse,
  SystemHealthSummary,
  TenantResponse,
  User,
} from '@/types';

export interface AppState {
  // Authentication & User Session
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  authLoading: boolean;
  authError: string | null;

  // Active Multi-Tenant & Collection Contexts
  activeTenant: string;
  activeCollection: string;
  activeDomainId: string;

  // Available Tenants & Collections Registries
  tenantsList: TenantResponse[];
  collectionsList: CollectionResponse[];
  registriesLoading: boolean;

  // System Status / Developer Hub Health Indicator
  systemHealth: SystemHealthSummary | null;
  isHealthChecking: boolean;

  // Actions
  loginWithApiKey: (apiKey: string, tenantSlug?: string, name?: string) => Promise<void>;
  loginWithCredentials: (username?: string, password?: string, tenantSlug?: string) => Promise<void>;
  logout: () => void;
  setUser: (user: User | null) => void;
  setToken: (token: string | null) => void;
  setActiveTenant: (tenantId: string) => Promise<void>;
  setActiveCollection: (collectionName: string) => void;
  setActiveDomain: (domainId: string) => void;
  fetchTenantsAndCollections: () => Promise<void>;
  checkSystemHealth: () => Promise<SystemHealthSummary>;
  initAuthListener: () => () => void;
}

/**
 * Reads initial active tenant from storage or fallback to 'default'.
 */
function getInitialTenant(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACTIVE_TENANT) || 'default';
  } catch {
    return 'default';
  }
}

/**
 * Reads initial active collection from storage or fallback to 'ue5_docs'.
 */
function getInitialCollection(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACTIVE_COLLECTION) || 'ue5_docs';
  } catch {
    return 'ue5_docs';
  }
}

/**
 * Reads initial active domain from storage or fallback to 'godot_4'.
 */
function getInitialDomain(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACTIVE_DOMAIN) || 'godot_4';
  } catch {
    return 'godot_4';
  }
}

/**
 * Reads initial User object from storage if present.
 */
function getInitialUser(): User | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.USER);
    if (!raw) return null;
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

export const useAppStore = create<AppState>((set, get) => ({
  user: getInitialUser(),
  token: localStorage.getItem(STORAGE_KEYS.TOKEN),
  isAuthenticated: Boolean(localStorage.getItem(STORAGE_KEYS.TOKEN)),
  authLoading: false,
  authError: null,

  activeTenant: getInitialTenant(),
  activeCollection: getInitialCollection(),
  activeDomainId: getInitialDomain(),

  tenantsList: [],
  collectionsList: [],
  registriesLoading: false,

  systemHealth: null,
  isHealthChecking: false,

  /**
   * Login with API Key (attaches API Key header to requests)
   */
  loginWithApiKey: async (apiKey: string, tenantSlug = 'default', name = 'API Key User') => {
    set({ authLoading: true, authError: null });
    try {
      const payload = await authService.login({
        apiKey,
        tenant_slug: tenantSlug,
        username: name,
      });

      localStorage.setItem(STORAGE_KEYS.TOKEN, payload.token);
      localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(payload.user));
      localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, payload.user.tenant_id);

      set({
        token: payload.token,
        user: payload.user,
        isAuthenticated: true,
        authLoading: false,
        activeTenant: payload.user.tenant_id,
      });

      // After successful login, refresh tenant/collection registries
      await get().fetchTenantsAndCollections();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Falha ao autenticar com API Key.';
      set({ authLoading: false, authError: message, isAuthenticated: false });
      throw err;
    }
  },

  /**
   * Login with standard JWT username / password exchange
   */
  loginWithCredentials: async (username = '', password = '', tenantSlug = 'default') => {
    set({ authLoading: true, authError: null });
    try {
      const payload = await authService.login({
        username,
        password,
        tenant_slug: tenantSlug,
      });

      set({
        token: payload.token,
        user: payload.user,
        isAuthenticated: true,
        authLoading: false,
        activeTenant: payload.user.tenant_id || tenantSlug,
      });

      await get().fetchTenantsAndCollections();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Falha no login com credenciais.';
      set({ authLoading: false, authError: message, isAuthenticated: false });
      throw err;
    }
  },

  /**
   * Clears session token, user data, and resets authentication state
   */
  logout: () => {
    authService.logout();
    set({
      user: null,
      token: null,
      isAuthenticated: false,
      authError: null,
    });
  },

  setUser: (user) => {
    if (user) {
      localStorage.setItem(STORAGE_KEYS.USER, JSON.stringify(user));
    } else {
      localStorage.removeItem(STORAGE_KEYS.USER);
    }
    set({ user });
  },

  setToken: (token) => {
    if (token) {
      localStorage.setItem(STORAGE_KEYS.TOKEN, token);
      set({ token, isAuthenticated: true });
    } else {
      localStorage.removeItem(STORAGE_KEYS.TOKEN);
      set({ token: null, isAuthenticated: false });
    }
  },

  /**
   * Changes active Tenant ID and reloads available collections for that tenant
   */
  setActiveTenant: async (tenantId: string) => {
    tenantService.setActiveTenant(tenantId);
    set({ activeTenant: tenantId });
    await get().fetchTenantsAndCollections();
  },

  /**
   * Changes active Collection name
   */
  setActiveCollection: (collectionName: string) => {
    tenantService.setActiveCollection(collectionName);
    set({ activeCollection: collectionName });
  },

  /**
   * Changes active Domain ID (e.g. 'godot_4' or 'ue5')
   */
  setActiveDomain: (domainId: string) => {
    tenantService.setActiveDomain(domainId);
    set({ activeDomainId: domainId });
  },

  /**
   * Loads the list of tenants and collections from admin API
   */
  fetchTenantsAndCollections: async () => {
    set({ registriesLoading: true });
    try {
      const { activeTenant } = get();

      // Fetch collections for current active tenant
      const collectionsRes = await tenantService.listCollections({
        tenant_id: activeTenant,
        page_size: 100,
      });

      // Try fetching tenants list if user has admin permissions
      let tenantsList: TenantResponse[] = [];
      try {
        const tenantsRes = await tenantService.listTenants({ page_size: 100 });
        tenantsList = tenantsRes.items || [];
      } catch {
        // Fallback if not admin: provide active tenant in list
        tenantsList = [
          {
            id: 'current',
            slug: activeTenant,
            name: `Tenant (${activeTenant})`,
            description: 'Tenant atualmente selecionado',
            is_active: true,
          },
        ];
      }

      const items = collectionsRes.items || [];
      // Default collection selection if current activeCollection is not in list
      const existingCollection = items.find((c) => c.qdrant_collection_name === get().activeCollection);
      if (!existingCollection && items.length > 0) {
        const first = items[0].qdrant_collection_name;
        tenantService.setActiveCollection(first);
        set({ activeCollection: first });
      }

      set({
        collectionsList: items,
        tenantsList,
        registriesLoading: false,
      });
    } catch {
      set({ registriesLoading: false });
    }
  },

  /**
   * Pings GET /v1/health and updates system status summary for Developer Hub
   */
  checkSystemHealth: async (): Promise<SystemHealthSummary> => {
    set({ isHealthChecking: true });
    try {
      const response = await apiClient.get<HealthResponse>('/health');
      const data = response.data;

      const summary: SystemHealthSummary = {
        status: data.status || 'ok',
        lastChecked: new Date().toISOString(),
        version: data.version,
        environment: data.environment,
        dependencies: data.dependencies || {},
      };

      set({ systemHealth: summary, isHealthChecking: false });
      return summary;
    } catch (err: unknown) {
      const errorMessage =
        err instanceof Error ? err.message : 'Não foi possível conectar ao servidor backend.';

      const degradedSummary: SystemHealthSummary = {
        status: 'error',
        lastChecked: new Date().toISOString(),
        errorMessage,
      };

      set({ systemHealth: degradedSummary, isHealthChecking: false });
      return degradedSummary;
    }
  },

  /**
   * Registers global event listener for 401 Unauthorized interceptor events
   */
  initAuthListener: () => {
    const handleUnauthorized = () => {
      get().logout();
    };
    window.addEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, handleUnauthorized);
    };
  },
}));
