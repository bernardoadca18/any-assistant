/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Key, Loader2, Lock, ShieldCheck, User, X, AlertTriangle } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const AuthModal: React.FC<AuthModalProps> = ({ isOpen, onClose }) => {
  const { loginWithApiKey, loginWithCredentials, authLoading, authError } = useAppStore();

  const [mode, setMode] = useState<'apikey' | 'credentials'>('apikey');
  const [apiKey, setApiKey] = useState('');
  const [tenantSlug, setTenantSlug] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    try {
      if (mode === 'apikey') {
        if (!apiKey.trim()) {
          setErrorMessage('Informe uma API Key válida para autenticar.');
          return;
        }
        if (!apiKey.trim().startsWith('ue5_')) {
          setErrorMessage('Formato inválido. A API Key deve começar com "ue5_".');
          return;
        }
        await loginWithApiKey(apiKey.trim(), tenantSlug.trim() || 'default', username.trim() || 'Developer');
      } else {
        if (!username.trim() || !password) {
          setErrorMessage('Informe usuário e senha para autenticação.');
          return;
        }
        await loginWithCredentials(username.trim(), password, tenantSlug.trim() || 'default');
      }
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Falha na autenticação.';
      setErrorMessage(msg);
    }
  };

  const handleSwitchMode = (newMode: 'apikey' | 'credentials') => {
    setMode(newMode);
    setErrorMessage(null);
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-md w-full shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272A]">
          <div className="flex items-center gap-2">
            <Lock className="w-4 h-4 text-indigo-400" />
            <h2 className="text-sm font-semibold text-zinc-100">Autenticação</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-300 transition-colors"
            aria-label="Fechar"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Mode Tabs */}
        <div className="flex border-b border-[#27272A]">
          <button
            type="button"
            onClick={() => handleSwitchMode('apikey')}
            className={`flex-1 py-2.5 text-xs font-semibold uppercase tracking-wider border-b-2 transition-all ${
              mode === 'apikey'
                ? 'border-indigo-600 text-indigo-400 bg-[#121214]'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Key className="w-3.5 h-3.5 inline-block mr-1.5" />
            API Key
          </button>
          <button
            type="button"
            onClick={() => handleSwitchMode('credentials')}
            className={`flex-1 py-2.5 text-xs font-semibold uppercase tracking-wider border-b-2 transition-all ${
              mode === 'credentials'
                ? 'border-indigo-600 text-indigo-400 bg-[#121214]'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <User className="w-3.5 h-3.5 inline-block mr-1.5" />
            JWT Credentials
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {/* Error Display */}
          {(errorMessage || authError) && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{errorMessage || authError}</span>
            </div>
          )}

          {mode === 'apikey' ? (
            <>
              {/* API Key Input */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  API Key
                </label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="ue5_xxxxxxxxxxxxxxxx"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                  required
                  autoFocus
                />
                <p className="text-[10px] text-zinc-500">
                  A chave será enviada no header <code className="text-zinc-400">X-API-Key</code> em todas as requisições.
                </p>
              </div>

              {/* Tenant Slug (Optional) */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Tenant Slug <span className="text-zinc-600">(opcional)</span>
                </label>
                <input
                  type="text"
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="default"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                />
                <p className="text-[10px] text-zinc-500">
                  Deixe em branco para usar o tenant padrão.
                </p>
              </div>

              {/* Display Name (Optional) */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Nome de Exibição <span className="text-zinc-600">(opcional)</span>
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Developer"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                />
              </div>
            </>
          ) : (
            <>
              {/* Username */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Usuário
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="developer"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                  required
                  autoFocus
                />
              </div>

              {/* Password */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Senha
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
              </div>

              {/* Tenant Slug */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Tenant Slug
                </label>
                <input
                  type="text"
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="default"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                />
              </div>
            </>
          )}

          {/* Actions */}
          <div className="pt-3 flex items-center justify-end gap-3 border-t border-[#27272A]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-[#1C1C1F] hover:bg-[#252529] border border-[#27272A] rounded-lg text-xs font-medium text-zinc-300 transition-colors"
            >
              Cancelar
            </button>
            <button
              type="submit"
              disabled={authLoading}
              className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-60 disabled:cursor-not-allowed text-white rounded-lg text-xs font-semibold flex items-center gap-2 shadow-md transition-all cursor-pointer"
            >
              {authLoading ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Autenticando...</span>
                </>
              ) : (
                <>
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>Autenticar</span>
                </>
              )}
            </button>
          </div>
        </form>

        {/* Dev Mode Notice */}
        {import.meta.env.DEV && (
          <div className="px-5 py-3 border-t border-[#27272A] bg-[#0E0E10]">
            <p className="text-[10px] text-zinc-600 text-center">
              Modo de desenvolvimento ativo. Credenciais vazias usarão fallback de dev.
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
