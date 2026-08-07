/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * DevAuthPanel — painel de autenticação para desenvolvimento.
 *
 * Permite autenticar facilmente em ambiente de desenvolvimento usando
 * os mecanismos de dev-mode do backend (X-Tenant-ID header, API Key).
 *
 * IMPORTANTE: Este componente deve ser usado APENAS em desenvolvimento.
 * Deve ser removido/desabilitado quando um fluxo de auth real for implementado.
 */

import React, { useState, useEffect } from 'react';
import { AlertCircle, Key, Loader2, Shield, X } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { STORAGE_KEYS, apiClient } from '@/services/apiClient';

interface DevAuthPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

export const DevAuthPanel: React.FC<DevAuthPanelProps> = ({ isOpen, onClose }) => {
  const {
    activeTenant,
    tenantsList,
    fetchTenantsAndCollections,
    setActiveTenant,
    setToken,
    logout,
  } = useAppStore();

  const [devMode, setDevMode] = useState<'tenant-header' | 'api-key'>('tenant-header');
  const [tenantInput, setTenantInput] = useState(activeTenant);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [isAuthenticating, setIsAuthenticating] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authSuccess, setAuthSuccess] = useState<string | null>(null);

  // Sync tenant input with store
  useEffect(() => {
    setTenantInput(activeTenant);
  }, [activeTenant]);

  if (!isOpen) return null;

  const handleTenantHeaderAuth = async () => {
    setIsAuthenticating(true);
    setAuthError(null);
    setAuthSuccess(null);

    try {
      // Save tenant to localStorage
      localStorage.setItem(STORAGE_KEYS.ACTIVE_TENANT, tenantInput);

      // Clear any existing token (dev mode uses X-Tenant-ID header)
      logout();

      // Refresh tenants list
      await fetchTenantsAndCollections();

      // Verify auth by making a test call
      await apiClient.get('/sessions');

      setActiveTenant(tenantInput);
      setAuthSuccess(`Autenticado como tenant "${tenantInput}" em modo dev (X-Tenant-ID header)`);
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
      // Save API key as token (apiClient will detect it's an API key and use X-API-Key header)
      setToken(apiKeyInput.trim());

      // Refresh tenants list
      await fetchTenantsAndCollections();

      // Verify auth
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative w-full max-w-lg bg-[#121214] border border-[#27272A] rounded-xl shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272A]">
          <div className="flex items-center gap-2">
            <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-amber-500/10">
              <Shield className="w-4 h-4 text-amber-400" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-zinc-100">Dev Mode Auth</h2>
              <p className="text-xs text-zinc-500">Autenticação de desenvolvimento</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-200 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Content */}
        <div className="p-5 space-y-4">
          {/* Warning banner */}
          <div className="flex items-start gap-2.5 p-3 rounded-lg bg-amber-500/10 border border-amber-500/20">
            <AlertCircle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
            <div className="text-xs text-amber-300">
              <strong>Atenção:</strong> Este painel é apenas para desenvolvimento local.
              Não use em produção. Quando um fluxo de autenticação real for implementado,
              este painel deve ser removido.
            </div>
          </div>

          {/* Success message */}
          {authSuccess && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs text-emerald-300">
              {authSuccess}
            </div>
          )}

          {/* Error message */}
          {authError && (
            <div className="p-3 rounded-lg bg-rose-500/10 border border-rose-500/20 text-xs text-rose-300">
              {authError}
            </div>
          )}

          {/* Auth method selector */}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setDevMode('tenant-header')}
              className={`flex-1 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                devMode === 'tenant-header'
                  ? 'bg-indigo-600 text-white'
                  : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200'
              }`}
            >
              X-Tenant-ID Header
            </button>
            <button
              type="button"
              onClick={() => setDevMode('api-key')}
              className={`flex-1 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
                devMode === 'api-key'
                  ? 'bg-indigo-600 text-white'
                  : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200'
              }`}
            >
              API Key
            </button>
          </div>

          {/* Tenant Header Auth */}
          {devMode === 'tenant-header' && (
            <div className="space-y-3">
              <div className="space-y-2">
                <label className="text-xs font-medium text-zinc-300">
                  Tenant ID ou Slug
                </label>
                <input
                  type="text"
                  value={tenantInput}
                  onChange={(e) => setTenantInput(e.target.value)}
                  placeholder="default, ue5-official, ou UUID"
                  className="w-full bg-[#141416] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500 font-mono"
                />
                <p className="text-[11px] text-zinc-500">
                  Este valor será enviado como header <code className="text-indigo-400">X-Tenant-ID</code> em todas as requisições.
                  Funciona apenas em modo desenvolvimento (APP_ENV=development).
                </p>
              </div>

              {/* Quick tenant selection */}
              {tenantsList.length > 0 && (
                <div className="space-y-2">
                  <label className="text-xs font-medium text-zinc-400">Tenants disponíveis:</label>
                  <div className="flex flex-wrap gap-1.5">
                    {tenantsList.map((tenant) => (
                      <button
                        key={tenant.id}
                        type="button"
                        onClick={() => setTenantInput(tenant.slug || tenant.id)}
                        className={`px-2 py-1 rounded text-[11px] transition-colors ${
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
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded-lg text-xs font-semibold text-white transition-colors"
              >
                {isAuthenticating ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Autenticando...
                  </>
                ) : (
                  <>
                    <Shield className="w-3.5 h-3.5" />
                    Autenticar (Dev Mode)
                  </>
                )}
              </button>
            </div>
          )}

          {/* API Key Auth */}
          {devMode === 'api-key' && (
            <div className="space-y-3">
              <div className="space-y-2">
                <label className="text-xs font-medium text-zinc-300">
                  API Key
                </label>
                <input
                  type="password"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder="ue5_..."
                  className="w-full bg-[#141416] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500 font-mono"
                />
                <p className="text-[11px] text-zinc-500">
                  API Keys começam com <code className="text-indigo-400">ue5_</code> e são enviadas
                  como header <code className="text-indigo-400">X-API-Key</code>.
                  Crie uma API key via endpoint <code className="text-zinc-400">POST /v1/admin/api-keys</code> ou use uma key existente.
                </p>
              </div>

              <button
                type="button"
                onClick={handleApiKeyAuth}
                disabled={isAuthenticating || !apiKeyInput.trim()}
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded-lg text-xs font-semibold text-white transition-colors"
              >
                {isAuthenticating ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    Autenticando...
                  </>
                ) : (
                  <>
                    <Key className="w-3.5 h-3.5" />
                    Autenticar com API Key
                  </>
                )}
              </button>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-[#27272A] bg-[#0C0C0E]">
          <p className="text-[11px] text-zinc-500 text-center">
            Mecanismos de autenticação disponíveis no backend:
            <br />
            <code className="text-indigo-400">X-Tenant-ID</code> (dev),{' '}
            <code className="text-indigo-400">X-API-Key</code>,{' '}
            <code className="text-zinc-600">JWT Bearer</code> (não implementado)
          </p>
        </div>
      </div>
    </div>
  );
};
