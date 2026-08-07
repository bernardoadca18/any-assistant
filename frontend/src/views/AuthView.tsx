/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * AuthView — página de autenticação para desenvolvimento e produção.
 *
 * Permite autenticar usando:
 * - X-Tenant-ID header (dev mode)
 * - API Key
 * - Username/Password (futuro)
 */

import React, { useState } from 'react';
import { AlertCircle, Key, Loader2, Shield, User } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { STORAGE_KEYS, apiClient } from '@/services/apiClient';

export const AuthView: React.FC = () => {
  const {
    activeTenant,
    tenantsList,
    fetchTenantsAndCollections,
    setActiveTenant,
    setToken,
    logout,
    isAuthenticated,
    user,
  } = useAppStore();

  const [authMode, setAuthMode] = useState<'tenant-header' | 'api-key'>('tenant-header');
  const [tenantInput, setTenantInput] = useState(activeTenant);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [isAuthenticating, setIsAuthenticating] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authSuccess, setAuthSuccess] = useState<string | null>(null);

  const handleTenantHeaderAuth = async () => {
    setIsAuthenticating(true);
    setAuthError(null);
    setAuthSuccess(null);

    try {
      localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantInput);
      logout();
      await fetchTenantsAndCollections();
      await apiClient.get('/sessions');
      setActiveTenant(tenantInput);
      setAuthSuccess(`Autenticado como tenant "${tenantInput}" (modo dev)`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Falha na autenticação';
      setAuthError(message);
    } finally {
      setIsAuthenticating(false);
    }
  };

  const handleApiKeyAuth = async () => {
    if (!apiKeyInput.trim()) {
      setAuthError('API Key é obrigatória');
      return;
    }

    setIsAuthenticating(true);
    setAuthError(null);
    setAuthSuccess(null);

    try {
      setToken(apiKeyInput.trim());
      await fetchTenantsAndCollections();
      await apiClient.get('/sessions');
      setAuthSuccess('Autenticado com API Key');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Falha na autenticação';
      setAuthError(message);
      logout();
    } finally {
      setIsAuthenticating(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-[#0C0C0E] overflow-auto">
      <div className="max-w-2xl mx-auto w-full p-8">
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-2">
            <div className="flex items-center justify-center w-10 h-10 rounded-lg bg-indigo-600/10">
              <Shield className="w-5 h-5 text-indigo-400" />
            </div>
            <div>
              <h1 className="text-xl font-semibold text-zinc-100">Autenticação</h1>
              <p className="text-sm text-zinc-500">Configure suas credenciais de acesso</p>
            </div>
          </div>
        </div>

        {/* Current Auth Status */}
        {isAuthenticated && user && (
          <div className="mb-6 p-4 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
            <div className="flex items-center gap-2 mb-2">
              <User className="w-4 h-4 text-emerald-400" />
              <span className="text-sm font-medium text-emerald-300">Autenticado</span>
            </div>
            <p className="text-xs text-emerald-200">
              Logado como <strong>{user.username || 'API Key User'}</strong> no tenant <strong>{activeTenant}</strong>
            </p>
          </div>
        )}

        {/* Warning banner */}
        <div className="mb-6 flex items-start gap-3 p-4 rounded-lg bg-amber-500/10 border border-amber-500/20">
          <AlertCircle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <div className="text-xs text-amber-300">
            <strong>Nota:</strong> Este painel permite autenticação via API Key ou X-Tenant-ID header (dev mode).
            Configure suas credenciais para acessar o assistente.
          </div>
        </div>

        {/* Success message */}
        {authSuccess && (
          <div className="mb-6 p-4 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-sm text-emerald-300">
            {authSuccess}
          </div>
        )}

        {/* Error message */}
        {authError && (
          <div className="mb-6 p-4 rounded-lg bg-rose-500/10 border border-rose-500/20 text-sm text-rose-300">
            {authError}
          </div>
        )}

        {/* Auth method selector */}
        <div className="flex gap-2 mb-6">
          <button
            type="button"
            onClick={() => setAuthMode('tenant-header')}
            className={`flex-1 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
              authMode === 'tenant-header'
                ? 'bg-indigo-600 text-white'
                : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200'
            }`}
          >
            X-Tenant-ID Header (Dev)
          </button>
          <button
            type="button"
            onClick={() => setAuthMode('api-key')}
            className={`flex-1 px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${
              authMode === 'api-key'
                ? 'bg-indigo-600 text-white'
                : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200'
            }`}
          >
            API Key
          </button>
        </div>

        {/* Tenant Header Auth */}
        {authMode === 'tenant-header' && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-zinc-300">Tenant ID ou Slug</label>
              <input
                type="text"
                value={tenantInput}
                onChange={(e) => setTenantInput(e.target.value)}
                placeholder="default, ue5-official, ou UUID"
                className="w-full bg-[#141416] border border-[#27272A] rounded-lg px-4 py-3 text-sm text-zinc-200 outline-none focus:border-indigo-500 font-mono"
              />
              <p className="text-xs text-zinc-500">
                Enviado como header <code className="text-indigo-400">X-Tenant-ID</code>. Funciona apenas em desenvolvimento.
              </p>
            </div>

            {/* Quick tenant selection */}
            {tenantsList.length > 0 && (
              <div className="space-y-2">
                <label className="text-sm font-medium text-zinc-400">Tenants disponíveis:</label>
                <div className="flex flex-wrap gap-2">
                  {tenantsList.map((tenant) => (
                    <button
                      key={tenant.id}
                      type="button"
                      onClick={() => setTenantInput(tenant.slug || tenant.id)}
                      className={`px-3 py-1.5 rounded text-xs transition-colors ${
                        tenantInput === tenant.slug || tenantInput === tenant.id
                          ? 'bg-indigo-600 text-white'
                          : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200'
                      }`}
                    >
                      {tenant.name}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <button
              type="button"
              onClick={handleTenantHeaderAuth}
              disabled={isAuthenticating || !tenantInput.trim()}
              className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-indigo-600 hover:bg-indigo-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded-lg text-sm font-semibold text-white transition-colors"
            >
              {isAuthenticating ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Autenticando...
                </>
              ) : (
                <>
                  <Shield className="w-4 h-4" />
                  Autenticar (Dev Mode)
                </>
              )}
            </button>
          </div>
        )}

        {/* API Key Auth */}
        {authMode === 'api-key' && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-zinc-300">API Key</label>
              <input
                type="password"
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                placeholder="ue5_..."
                className="w-full bg-[#141416] border border-[#27272A] rounded-lg px-4 py-3 text-sm text-zinc-200 outline-none focus:border-indigo-500 font-mono"
              />
              <p className="text-xs text-zinc-500">
                API Keys começam com <code className="text-indigo-400">ue5_</code> e são enviadas como header{' '}
                <code className="text-indigo-400">X-API-Key</code>.
              </p>
            </div>

            <button
              type="button"
              onClick={handleApiKeyAuth}
              disabled={isAuthenticating || !apiKeyInput.trim()}
              className="w-full flex items-center justify-center gap-2 px-4 py-3 bg-indigo-600 hover:bg-indigo-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded-lg text-sm font-semibold text-white transition-colors"
            >
              {isAuthenticating ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Autenticando...
                </>
              ) : (
                <>
                  <Key className="w-4 h-4" />
                  Autenticar com API Key
                </>
              )}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
