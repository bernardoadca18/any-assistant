/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Key, Loader2, Lock, ShieldCheck, User, X } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';

interface AuthModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const AuthModal: React.FC<AuthModalProps> = ({ isOpen, onClose }) => {
  const { loginWithApiKey, loginWithCredentials, authLoading, authError } = useAppStore();

  const [mode, setMode] = useState<'apikey' | 'credentials'>('apikey');
  const [apiKey, setApiKey] = useState('');
  const [tenantSlug, setTenantSlug] = useState('Enterprise_Alpha');
  const [username, setUsername] = useState('developer');
  const [password, setPassword] = useState('');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMessage(null);

    try {
      if (mode === 'apikey') {
        if (!apiKey.trim()) {
          setErrorMessage('Informe uma API Key válida para se autenticar.');
          return;
        }
        await loginWithApiKey(apiKey.trim(), tenantSlug.trim(), username.trim() || 'API Key Developer');
      } else {
        await loginWithCredentials(username.trim(), password, tenantSlug.trim());
      }
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Falha na autenticação.';
      setErrorMessage(msg);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 select-none">
      <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-md w-full overflow-hidden shadow-2xl text-[#E4E4E7]">
        {/* Header */}
        <div className="px-6 py-4 border-b border-[#27272A] flex items-center justify-between bg-[#1C1C1F]">
          <div className="flex items-center gap-2">
            <Key className="w-4 h-4 text-indigo-400" />
            <h2 className="font-semibold text-sm text-zinc-100">
              Autenticação e Sessão (JWT / API Key)
            </h2>
          </div>
          <button
            onClick={onClose}
            type="button"
            className="text-zinc-400 hover:text-zinc-200 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tabs */}
        <div className="grid grid-cols-2 border-b border-[#27272A] bg-[#0E0E10]">
          <button
            type="button"
            onClick={() => {
              setMode('apikey');
              setErrorMessage(null);
            }}
            className={`py-2.5 text-xs font-semibold uppercase tracking-wider border-b-2 transition-all ${
              mode === 'apikey'
                ? 'border-indigo-600 text-indigo-400 bg-[#121214]'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            API Key Header
          </button>
          <button
            type="button"
            onClick={() => {
              setMode('credentials');
              setErrorMessage(null);
            }}
            className={`py-2.5 text-xs font-semibold uppercase tracking-wider border-b-2 transition-all ${
              mode === 'credentials'
                ? 'border-indigo-600 text-indigo-400 bg-[#121214]'
                : 'border-transparent text-zinc-500 hover:text-zinc-300'
            }`}
          >
            JWT Credentials
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {(errorMessage || authError) && (
            <div className="p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
              {errorMessage || authError}
            </div>
          )}

          {mode === 'apikey' ? (
            <>
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  API Key (X-API-Key / Bearer)
                </label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="rk-live-xxxx-xxxx"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
                <p className="text-[10px] text-zinc-500">
                  Será injetada automaticamente em todas as requisições via interceptador Axios & Fetch.
                </p>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Tenant Slug / ID Ativo
                </label>
                <input
                  type="text"
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="Enterprise_Alpha, ue5-official, default"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Nome do Desenvolvedor
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="JD // Lead Engineer"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                />
              </div>
            </>
          ) : (
            <>
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">Usuário / Email</label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="dev@ai.studio"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">Senha</label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••••••"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">
                  Tenant Slug / ID Ativo
                </label>
                <input
                  type="text"
                  value={tenantSlug}
                  onChange={(e) => setTenantSlug(e.target.value)}
                  placeholder="Enterprise_Alpha, ue5-official"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                  required
                />
              </div>
            </>
          )}

          <div className="pt-2 flex items-center justify-end gap-3 border-t border-[#27272A]">
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
              className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold flex items-center gap-2 shadow-md transition-all cursor-pointer"
            >
              {authLoading ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span>Conectando...</span>
                </>
              ) : (
                <>
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>Salvar & Autenticar</span>
                </>
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
