/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * TenantManagerView — gestão de tenants e API keys:
 * - Tabela paginada (GET /v1/admin/tenants)
 * - Modal de criação (POST /v1/admin/tenants)
 * - Drawer de detalhes com API keys (GET/POST/DELETE /v1/admin/tenants/{id}/api-keys)
 */

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Key,
  Lock,
  Loader2,
  Plus,
  Shield,
  ShieldOff,
  Trash2,
  Users,
  X,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as tenantService from '@/services/tenantService';
import type { APIKeyCreateResponse, APIKeyResponse, TenantCreate, TenantResponse } from '@/types';

const PAGE_SIZE = 10;

// ============================================================================
// Create Tenant Modal
// ============================================================================
const CreateTenantModal: React.FC<{
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}> = ({ isOpen, onClose, onCreated }) => {
  const [slug, setSlug] = useState('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [rateLimitRpm, setRateLimitRpm] = useState('');
  const [rateLimitTpm, setRateLimitTpm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!slug.trim() || !name.trim()) {
      setError('Slug e Nome são obrigatórios.');
      return;
    }
    setSubmitting(true);
    try {
      const payload: TenantCreate = {
        slug: slug.trim(),
        name: name.trim(),
        description: description.trim() || undefined,
        rate_limit_rpm: rateLimitRpm ? Number(rateLimitRpm) : null,
        rate_limit_tpm: rateLimitTpm ? Number(rateLimitTpm) : null,
      };
      await tenantService.createTenant(payload);
      onCreated();
      onClose();
      setSlug('');
      setName('');
      setDescription('');
      setRateLimitRpm('');
      setRateLimitTpm('');
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Falha ao criar tenant.';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-md w-full shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272A]">
          <h2 className="text-sm font-semibold text-zinc-100">Novo Tenant</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300" aria-label="Fechar">
            <X className="w-4 h-4" />
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-5 space-y-4">
          {error && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Slug</label>
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="enterprise-alpha"
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Nome</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Enterprise Alpha"
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Descrição</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500 resize-none"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300">Rate limit (RPM)</label>
              <input
                type="number"
                value={rateLimitRpm}
                onChange={(e) => setRateLimitRpm(e.target.value)}
                placeholder="60"
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300">Rate limit (TPM)</label>
              <input
                type="number"
                value={rateLimitTpm}
                onChange={(e) => setRateLimitTpm(e.target.value)}
                placeholder="100000"
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              />
            </div>
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 rounded-lg text-xs text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              Cancelar
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 disabled:opacity-60 text-white transition-colors"
            >
              {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              Criar Tenant
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

// ============================================================================
// New Secret Key Reveal Modal
// ============================================================================
const NewSecretKeyModal: React.FC<{ secret: string | null; onClose: () => void }> = ({
  secret,
  onClose,
}) => {
  const [copied, setCopied] = useState(false);
  if (!secret) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Falha silenciosa
    }
  };

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-[60] flex items-center justify-center p-4">
      <div className="bg-[#121214] border border-amber-700/50 rounded-xl max-w-md w-full shadow-2xl">
        <div className="px-5 py-4 border-b border-[#27272A] flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-400" />
          <h2 className="text-sm font-semibold text-amber-300">
            Copie sua Secret Key agora
          </h2>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-xs text-zinc-400">
            Esta chave é exibida apenas uma vez. Ela não poderá ser recuperada depois de
            fechar este modal.
          </p>
          <div className="flex items-center gap-2 bg-[#0E0E10] border border-[#27272A] rounded-lg p-3">
            <code className="flex-1 text-xs font-mono text-emerald-400 break-all">{secret}</code>
            <button
              onClick={handleCopy}
              className="shrink-0 flex items-center gap-1 text-xs px-2 py-1 rounded-md bg-[#1C1C1F] hover:bg-[#27272A] text-zinc-300 transition-colors"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              {copied ? 'Copiado' : 'Copiar'}
            </button>
          </div>
          <button
            onClick={onClose}
            className="w-full mt-2 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium transition-colors"
          >
            Já copiei, fechar
          </button>
        </div>
      </div>
    </div>
  );
};

// ============================================================================
// Tenant Detail Drawer (API Keys)
// ============================================================================
const TenantDetailDrawer: React.FC<{
  tenant: TenantResponse | null;
  onClose: () => void;
}> = ({ tenant, onClose }) => {
  const [keys, setKeys] = useState<APIKeyResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [newKeyName, setNewKeyName] = useState('');
  const [newKeyAdmin, setNewKeyAdmin] = useState(false);
  const [newKeyReadOnly, setNewKeyReadOnly] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadKeys = async (tenantId: string) => {
    setLoading(true);
    try {
      const list = await tenantService.listApiKeys(tenantId);
      setKeys(list);
    } catch {
      setKeys([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (tenant) {
      loadKeys(tenant.id);
      setNewKeyName('');
      setError(null);
    }
  }, [tenant]);

  if (!tenant) return null;

  const handleCreateKey = async () => {
    if (!newKeyName.trim()) {
      setError('Informe um nome para identificar a chave.');
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const created: APIKeyCreateResponse = await tenantService.createApiKey(tenant.id, {
        name: newKeyName.trim(),
        is_admin: newKeyAdmin,
        is_read_only: newKeyReadOnly,
      });
      setRevealedSecret(created.key);
      setNewKeyName('');
      setNewKeyAdmin(false);
      setNewKeyReadOnly(false);
      await loadKeys(tenant.id);
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Falha ao gerar API key.';
      setError(message);
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (keyId: string) => {
    try {
      await tenantService.revokeApiKey(tenant.id, keyId);
      setKeys((prev) => prev.filter((k) => k.id !== keyId));
    } catch {
      // Mantém a chave se a revogação falhar
    }
  };

  return (
    <>
      <div className="fixed inset-0 bg-black/60 z-40" onClick={onClose} />
      <div className="fixed top-0 right-0 h-full w-full max-w-md bg-[#0C0C0E] border-l border-[#27272A] z-50 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272A] shrink-0">
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">{tenant.name}</h2>
            <p className="text-[11px] font-mono text-zinc-600">{tenant.slug}</p>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300" aria-label="Fechar">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-5">
          <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3 text-xs text-zinc-400">
            {tenant.description || 'Sem descrição.'}
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-600 mb-2 flex items-center gap-1.5">
              <Key className="w-3.5 h-3.5" />
              API Keys
            </h3>

            {error && (
              <div className="flex items-start gap-2 p-2.5 mb-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
                <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3 space-y-2.5 mb-3">
              <input
                value={newKeyName}
                onChange={(e) => setNewKeyName(e.target.value)}
                placeholder="Nome da chave (ex: CI Pipeline)"
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-1.5 text-xs text-zinc-200 outline-none focus:border-indigo-500"
              />
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                  <input
                    type="checkbox"
                    checked={newKeyAdmin}
                    onChange={(e) => setNewKeyAdmin(e.target.checked)}
                    className="accent-indigo-500"
                  />
                  Admin
                </label>
                <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                  <input
                    type="checkbox"
                    checked={newKeyReadOnly}
                    onChange={(e) => setNewKeyReadOnly(e.target.checked)}
                    className="accent-indigo-500"
                  />
                  Somente leitura
                </label>
              </div>
              <button
                onClick={handleCreateKey}
                disabled={creating}
                className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-60 text-white text-xs font-medium transition-colors"
              >
                {creating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                Gerar nova chave
              </button>
            </div>

            {loading ? (
              <div className="flex items-center justify-center py-6 text-zinc-600 text-xs gap-2">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Carregando chaves...
              </div>
            ) : keys.length === 0 ? (
              <p className="text-xs text-zinc-600 text-center py-4">Nenhuma API key criada ainda.</p>
            ) : (
              <div className="space-y-1.5">
                {keys.map((k) => (
                  <div
                    key={k.id}
                    className="flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-[#121214] border border-[#27272A]"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs font-medium text-zinc-200 truncate">{k.name}</span>
                        {k.is_admin && (
                          <span title="Admin">
                            <Shield className="w-3 h-3 text-amber-400 shrink-0" />
                          </span>
                        )}
                        {k.is_read_only && (
                          <span title="Somente leitura">
                            <ShieldOff className="w-3 h-3 text-zinc-500 shrink-0" />
                          </span>
                        )}
                      </div>
                      <span className="text-[10px] font-mono text-zinc-600">{k.key_prefix}...</span>
                    </div>
                    <button
                      onClick={() => handleRevoke(k.id)}
                      className="shrink-0 text-zinc-600 hover:text-rose-400 transition-colors p-1"
                      title="Revogar chave"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <NewSecretKeyModal secret={revealedSecret} onClose={() => setRevealedSecret(null)} />
    </>
  );
};

// ============================================================================
// Main View
// ============================================================================
export const TenantManagerView: React.FC = () => {
  const { isAuthenticated, user } = useAppStore();

  const [tenants, setTenants] = useState<TenantResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [selectedTenant, setSelectedTenant] = useState<TenantResponse | null>(null);

  // Check if user has admin privileges
  const isAdmin = user?.role === 'admin';
  const canManage = isAuthenticated && isAdmin;

  const loadTenants = async (targetPage: number) => {
    if (!canManage) return;
    setLoading(true);
    try {
      const res = await tenantService.listTenants({ page: targetPage, page_size: PAGE_SIZE });
      setTenants(res.items || []);
      setTotal(res.total || 0);
    } catch {
      setTenants([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTenants(page);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, canManage]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // Not authenticated state
  if (!isAuthenticated) {
    return (
      <div className="flex-1 overflow-y-auto p-6 md:p-8">
        <div className="max-w-5xl mx-auto">
          <div className="flex flex-col items-center justify-center py-16 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-zinc-800/50 flex items-center justify-center">
              <Lock className="w-7 h-7 text-zinc-500" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-zinc-300">Autenticação Necessária</h2>
              <p className="text-sm text-zinc-500 mt-1 max-w-md">
                Faça login com uma API Key válida para gerenciar tenants e chaves de API.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Not admin state
  if (!isAdmin) {
    return (
      <div className="flex-1 overflow-y-auto p-6 md:p-8">
        <div className="max-w-5xl mx-auto">
          <div className="flex flex-col items-center justify-center py-16 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-amber-900/30 flex items-center justify-center">
              <ShieldOff className="w-7 h-7 text-amber-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-zinc-300">Privilégios de Admin Necessários</h2>
              <p className="text-sm text-zinc-500 mt-1 max-w-md">
                Sua API Key não possui privilégios de administrador. A gestão de tenants requer uma chave com is_admin=true.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-8">
      <div className="max-w-5xl mx-auto space-y-5">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
              <Users className="w-5 h-5 text-indigo-400" />
              Gestão de Tenants
            </h1>
            <p className="text-sm text-zinc-500 mt-1">
              Namespaces multi-tenant e chaves de API do backend RAG.
            </p>
          </div>
          <button
            onClick={() => setCreateModalOpen(true)}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition-colors"
          >
            <Plus className="w-4 h-4" />
            Novo Tenant
          </button>
        </div>

        <div className="bg-[#121214] border border-[#27272A] rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#27272A] bg-[#141416] text-left">
                <th className="px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                  Nome
                </th>
                <th className="px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                  Slug
                </th>
                <th className="px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                  Status
                </th>
                <th className="px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                  Coleções
                </th>
                <th className="px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                  API Keys
                </th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-zinc-600 text-xs">
                    <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                    Carregando tenants...
                  </td>
                </tr>
              ) : tenants.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-zinc-600 text-xs">
                    Nenhum tenant encontrado.
                  </td>
                </tr>
              ) : (
                tenants.map((tenant) => (
                  <tr
                    key={tenant.id}
                    onClick={() => setSelectedTenant(tenant)}
                    className="border-b border-[#1F1F22] last:border-0 hover:bg-[#141416] cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-2.5 text-zinc-200 font-medium">{tenant.name}</td>
                    <td className="px-4 py-2.5 text-zinc-500 font-mono text-xs">{tenant.slug}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                          tenant.is_active
                            ? 'bg-emerald-950/40 text-emerald-400'
                            : 'bg-zinc-800 text-zinc-500'
                        }`}
                      >
                        {tenant.is_active ? 'ativo' : 'inativo'}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-zinc-400 text-xs">{tenant.collections_count ?? '—'}</td>
                    <td className="px-4 py-2.5 text-zinc-400 text-xs">{tenant.api_keys_count ?? '—'}</td>
                    <td className="px-4 py-2.5 text-zinc-600">
                      <ChevronRight className="w-4 h-4" />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>

          <div className="flex items-center justify-between px-4 py-3 border-t border-[#27272A] bg-[#0E0E10]">
            <span className="text-xs text-zinc-600">
              Página {page} de {totalPages} · {total} tenant(s)
            </span>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="p-1.5 rounded-md border border-[#27272A] text-zinc-400 hover:text-zinc-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="p-1.5 rounded-md border border-[#27272A] text-zinc-400 hover:text-zinc-200 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>

      <CreateTenantModal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={() => loadTenants(page)}
      />

      <TenantDetailDrawer tenant={selectedTenant} onClose={() => setSelectedTenant(null)} />
    </div>
  );
};
