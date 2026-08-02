/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * CollectionConfigView — configuração detalhada de coleções vetoriais
 * com ajustes de RAG, Embedding e Ingestão.
 */

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  Database,
  Edit2,
  FileText,
  Loader2,
  Lock,
  RefreshCw,
  Save,
  ShieldOff,
  X,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as tenantService from '@/services/tenantService';
import type { CollectionResponse, CollectionUpdate } from '@/types';

interface ConfigSectionProps {
  title: string;
  icon: React.ElementType;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

const ConfigSection: React.FC<ConfigSectionProps> = ({ title, icon: Icon, defaultOpen = true, children }) => {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="bg-[#121214] border border-[#27272A] rounded-xl overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-[#18181B] transition-colors"
      >
        <div className="flex items-center gap-2">
          <Icon className="w-4 h-4 text-zinc-500" />
          <span className="text-sm font-semibold text-zinc-200">{title}</span>
        </div>
        {open ? (
          <ChevronDown className="w-4 h-4 text-zinc-500" />
        ) : (
          <ChevronRight className="w-4 h-4 text-zinc-500" />
        )}
      </button>
      {open && <div className="px-4 pb-4 space-y-3 border-t border-[#27272A]">{children}</div>}
    </div>
  );
};

export const CollectionConfigView: React.FC = () => {
  const { isAuthenticated, user, activeTenant, activeDomainId } = useAppStore();

  const [collections, setCollections] = useState<CollectionResponse[]>([]);
  const [selectedCollection, setSelectedCollection] = useState<CollectionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Form state for collection update
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [isActive, setIsActive] = useState(true);
  const [defaultProjectSource, setDefaultProjectSource] = useState('');

  const isAdmin = user?.role === 'admin';
  const canManage = isAuthenticated && isAdmin;

  const loadCollections = async () => {
    if (!isAuthenticated) return;
    setLoading(true);
    setError(null);
    try {
      const res = await tenantService.listCollections({
        tenant_id: activeTenant,
        page_size: 100,
        domain_id: activeDomainId,
      });
      setCollections(res.items || []);
    } catch {
      setCollections([]);
      setError('Falha ao carregar coleções.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCollections();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTenant, activeDomainId, isAuthenticated]);

  useEffect(() => {
    if (selectedCollection) {
      setDisplayName(selectedCollection.display_name);
      setDescription(selectedCollection.description);
      setIsActive(selectedCollection.is_active);
      setDefaultProjectSource(selectedCollection.default_project_source_filter || '');
    }
  }, [selectedCollection]);

  const handleSave = async () => {
    if (!selectedCollection || !canManage) return;

    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      const payload: CollectionUpdate = {
        display_name: displayName.trim() || undefined,
        description: description.trim() || undefined,
        is_active: isActive,
        default_project_source_filter: defaultProjectSource.trim() || null,
      };

      const updated = await tenantService.updateCollection(selectedCollection.id, payload);

      // Update local state
      setCollections((prev) =>
        prev.map((c) => (c.id === updated.id ? updated : c))
      );
      setSelectedCollection(updated);
      setSuccess('Coleção atualizada com sucesso.');
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Falha ao atualizar coleção.';
      setError(message);
    } finally {
      setSaving(false);
    }
  };

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
                Faça login para configurar coleções vetoriais.
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
                A configuração de coleções requer privilégios administrativos.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-hidden flex">
      {/* Collections List Sidebar */}
      <div className="w-72 border-r border-[#27272A] bg-[#0C0C0E] flex flex-col">
        <div className="px-4 py-3 border-b border-[#27272A] flex items-center justify-between">
          <h2 className="text-sm font-semibold text-zinc-200">Coleções</h2>
          <button
            type="button"
            onClick={loadCollections}
            disabled={loading}
            className="p-1.5 rounded-md text-zinc-500 hover:text-zinc-300 hover:bg-[#18181B] transition-colors"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && collections.length === 0 ? (
            <div className="flex items-center justify-center py-8 text-zinc-600 text-xs gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              Carregando...
            </div>
          ) : collections.length === 0 ? (
            <div className="px-4 py-8 text-center text-zinc-600 text-xs">
              Nenhuma coleção encontrada.
            </div>
          ) : (
            <div className="space-y-1 p-2">
              {collections.map((coll) => (
                <button
                  key={coll.id}
                  type="button"
                  onClick={() => setSelectedCollection(coll)}
                  className={`w-full text-left px-3 py-2.5 rounded-lg transition-colors ${
                    selectedCollection?.id === coll.id
                      ? 'bg-indigo-600/20 border border-indigo-600/40'
                      : 'hover:bg-[#18181B] border border-transparent'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-zinc-200 truncate">
                      {coll.display_name}
                    </span>
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${
                        coll.is_active ? 'bg-emerald-500' : 'bg-zinc-600'
                      }`}
                    />
                  </div>
                  <div className="text-[10px] text-zinc-500 font-mono truncate mt-0.5">
                    {coll.qdrant_collection_name}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Configuration Panel */}
      <div className="flex-1 overflow-y-auto">
        {!selectedCollection ? (
          <div className="flex flex-col items-center justify-center h-full text-center gap-3 p-6">
            <Database className="w-10 h-10 text-zinc-700" />
            <p className="text-sm text-zinc-500">Selecione uma coleção para configurar.</p>
          </div>
        ) : (
          <div className="p-6 md:p-8 space-y-5">
            {/* Header */}
            <div className="flex items-start justify-between gap-4">
              <div>
                <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
                  <Database className="w-5 h-5 text-indigo-400" />
                  {selectedCollection.display_name}
                </h1>
                <p className="text-sm text-zinc-500 mt-1 font-mono">
                  {selectedCollection.qdrant_collection_name}
                </p>
              </div>

              <div className="flex items-center gap-2">
                <span
                  className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${
                    selectedCollection.is_active
                      ? 'bg-emerald-950/40 text-emerald-400'
                      : 'bg-zinc-800 text-zinc-500'
                  }`}
                >
                  {selectedCollection.is_active ? 'ativa' : 'inativa'}
                </span>
                <span className="px-2 py-0.5 rounded-md text-[10px] font-mono bg-[#1C1C1F] text-zinc-400">
                  {selectedCollection.domain_id}
                </span>
              </div>
            </div>

            {/* Alerts */}
            {error && (
              <div className="flex items-start gap-2 p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
                <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            {success && (
              <div className="flex items-start gap-2 p-3 rounded-lg bg-emerald-950/30 border border-emerald-800/60 text-emerald-300 text-xs">
                <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{success}</span>
              </div>
            )}

            {/* Basic Settings */}
            <ConfigSection title="Configurações Básicas" icon={Database}>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-semibold text-zinc-300 mb-1">
                    Nome de Exibição
                  </label>
                  <input
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none focus:border-indigo-500"
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-zinc-300 mb-1">
                    Descrição
                  </label>
                  <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    rows={2}
                    className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none focus:border-indigo-500 resize-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold text-zinc-300 mb-1">
                    Filtro de Project Source (padrão)
                  </label>
                  <input
                    type="text"
                    value={defaultProjectSource}
                    onChange={(e) => setDefaultProjectSource(e.target.value)}
                    placeholder="Ex: Lyra, EngineSource, GodotDocs..."
                    className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none focus:border-indigo-500 font-mono"
                  />
                  <p className="text-[10px] text-zinc-500 mt-1">
                    Filtra resultados de busca por origem do projeto.
                  </p>
                </div>

                <div className="flex items-center justify-between p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <span className="text-xs font-semibold text-zinc-300">Coleção Ativa</span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isActive}
                    onClick={() => setIsActive(!isActive)}
                    className={`relative w-10 h-5.5 rounded-full transition-colors ${
                      isActive ? 'bg-indigo-600' : 'bg-[#27272A]'
                    }`}
                    style={{ height: '1.375rem' }}
                  >
                    <span
                      className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                        isActive ? 'translate-x-4' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              </div>
            </ConfigSection>

            {/* Domain Info */}
            <ConfigSection title="Domínio" icon={Boxes} defaultOpen={false}>
              <div className="grid grid-cols-2 gap-3">
                <div className="p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">Domain ID</div>
                  <div className="text-sm font-mono text-zinc-300 mt-0.5">
                    {selectedCollection.domain_id}
                  </div>
                </div>
                <div className="p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wide">Tenant</div>
                  <div className="text-sm font-mono text-zinc-300 mt-0.5 truncate">
                    {selectedCollection.tenant_id}
                  </div>
                </div>
              </div>
            </ConfigSection>

            {/* RAG Configuration (read-only for now) */}
            <ConfigSection title="Configuração RAG" icon={Cpu} defaultOpen={false}>
              <div className="text-xs text-zinc-500 p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                <p>
                  As configurações de RAG, Embedding e Ingestão são definidas no nível do tenant
                  ou via configuração personalizada no backend.
                </p>
                <p className="mt-2">
                  Para personalizar estas configurações, entre em contato com o administrador
                  do sistema ou utilize a API diretamente.
                </p>
              </div>
            </ConfigSection>

            {/* Save Button */}
            <div className="flex justify-end pt-4 border-t border-[#27272A]">
              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                className="flex items-center gap-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-60 text-white rounded-lg text-sm font-semibold shadow-md transition-all"
              >
                {saving ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Salvando...
                  </>
                ) : (
                  <>
                    <Save className="w-4 h-4" />
                    Salvar Alterações
                  </>
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
