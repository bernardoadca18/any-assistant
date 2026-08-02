/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import {
  Check,
  Database,
  Layers,
  Plus,
  RefreshCw,
  Settings,
  X,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as tenantService from '@/services/tenantService';

interface TenantModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const TenantModal: React.FC<TenantModalProps> = ({ isOpen, onClose }) => {
  const {
    activeTenant,
    activeCollection,
    activeDomainId,
    setActiveTenant,
    setActiveCollection,
    setActiveDomain,
    collectionsList,
    tenantsList,
    registriesLoading,
    fetchTenantsAndCollections,
  } = useAppStore();

  const [isCreatingCollection, setIsCreatingCollection] = useState(false);
  const [newCollName, setNewCollName] = useState('');
  const [newCollDisplayName, setNewCollDisplayName] = useState('');
  const [newCollDomain, setNewCollDomain] = useState('godot_4');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleCreateCollection = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newCollName.trim() || !newCollDisplayName.trim()) return;

    setIsSubmitting(true);
    setErrorMessage(null);

    try {
      // Resolve tenant UUID from slug
      const tenant = tenantsList.find(t => t.slug === activeTenant || t.id === activeTenant);
      const tenantUuid = tenant?.id;

      if (!tenantUuid) {
        setErrorMessage(`Tenant não encontrado: ${activeTenant}`);
        setIsSubmitting(false);
        return;
      }

      await tenantService.createCollection({
        tenant_id: tenantUuid,
        name: newCollName.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '_'),
        qdrant_collection_name: newCollName.trim(),
        display_name: newCollDisplayName.trim(),
        domain_id: newCollDomain,
      });

      await fetchTenantsAndCollections();
      setActiveCollection(newCollName.trim());
      setIsCreatingCollection(false);
      setNewCollName('');
      setNewCollDisplayName('');
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : 'Falha ao criar nova coleção Qdrant.';
      setErrorMessage(msg);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 select-none">
      <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-lg w-full overflow-hidden shadow-2xl text-[#E4E4E7]">
        {/* Header */}
        <div className="px-6 py-4 border-b border-[#27272A] flex items-center justify-between bg-[#1C1C1F]">
          <div className="flex items-center gap-2">
            <Settings className="w-4 h-4 text-indigo-400" />
            <h2 className="font-semibold text-sm text-zinc-100">
              Sessão Multi-Tenant & Coleções Qdrant
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

        <div className="p-6 space-y-6">
          {errorMessage && (
            <div className="p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
              {errorMessage}
            </div>
          )}

          {/* Active Tenant Selection */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold text-zinc-300">
                Tenant Ativo (X-Tenant-ID)
              </label>
              <button
                type="button"
                onClick={() => fetchTenantsAndCollections()}
                disabled={registriesLoading}
                className="text-[11px] text-indigo-400 hover:text-indigo-300 flex items-center gap-1"
              >
                <RefreshCw className={`w-3 h-3 ${registriesLoading ? 'animate-spin' : ''}`} />
                <span>Recarregar</span>
              </button>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
              {tenantsList.length === 0 ? (
                <div className="col-span-2 sm:col-span-3 py-6 text-center text-zinc-500 text-xs">
                  {registriesLoading ? 'Carregando tenants...' : 'Nenhum tenant disponível. Faça login com uma API Key de admin.'}
                </div>
              ) : (
                tenantsList.map((tenant) => {
                  const isActive = activeTenant === tenant.id || activeTenant === tenant.slug;
                  return (
                    <button
                      key={tenant.id}
                      onClick={() => setActiveTenant(tenant.slug || tenant.id)}
                      type="button"
                      className={`px-3 py-2 rounded-lg border text-left transition-all ${
                        isActive
                          ? 'bg-indigo-950/80 border-indigo-600 text-indigo-200 font-semibold'
                          : 'bg-[#1C1C1F] border-[#27272A] text-zinc-400 hover:text-zinc-200 hover:border-zinc-700'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-xs truncate font-mono">
                          {tenant.name || tenant.id}
                        </span>
                        {isActive && <Check className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />}
                      </div>
                    </button>
                  );
                })
              )}
            </div>
          </div>

          {/* Active Domain Switcher */}
          <div className="space-y-2">
            <label className="text-xs font-semibold text-zinc-300">
              Domain Target (X-Domain-ID)
            </label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setActiveDomain('godot_4')}
                className={`px-3 py-2 rounded-lg border text-left transition-all ${
                  activeDomainId === 'godot_4'
                    ? 'bg-indigo-950/80 border-indigo-600 text-indigo-200 font-semibold'
                    : 'bg-[#1C1C1F] border-[#27272A] text-zinc-400 hover:text-zinc-200'
                }`}
              >
                <span className="text-xs font-mono block">godot_4</span>
                <span className="text-[10px] text-zinc-500">Godot Engine 4.x</span>
              </button>
              <button
                type="button"
                onClick={() => setActiveDomain('ue5')}
                className={`px-3 py-2 rounded-lg border text-left transition-all ${
                  activeDomainId === 'ue5'
                    ? 'bg-indigo-950/80 border-indigo-600 text-indigo-200 font-semibold'
                    : 'bg-[#1C1C1F] border-[#27272A] text-zinc-400 hover:text-zinc-200'
                }`}
              >
                <span className="text-xs font-mono block">ue5</span>
                <span className="text-[10px] text-zinc-500">Unreal Engine 5.4+</span>
              </button>
            </div>
          </div>

          {/* Active Qdrant Collection Selection */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold text-zinc-300">
                Coleção / Namespace Qdrant Ativa
              </label>
              <button
                type="button"
                onClick={() => setIsCreatingCollection(!isCreatingCollection)}
                className="text-[11px] text-indigo-400 hover:text-indigo-300 flex items-center gap-1 font-medium"
              >
                <Plus className="w-3 h-3" />
                <span>Nova Coleção</span>
              </button>
            </div>

            {isCreatingCollection ? (
              <form
                onSubmit={handleCreateCollection}
                className="p-4 bg-[#1C1C1F] border border-[#27272A] rounded-lg space-y-3"
              >
                <h4 className="text-xs font-semibold text-zinc-200">
                  Criar Coleção Qdrant para {activeTenant}
                </h4>
                <div className="grid grid-cols-2 gap-2">
                  <input
                    type="text"
                    value={newCollName}
                    onChange={(e) => setNewCollName(e.target.value)}
                    placeholder="ue5_gameplay"
                    className="bg-[#121214] border border-[#27272A] rounded px-2.5 py-1.5 text-xs text-zinc-200 font-mono outline-none focus:border-indigo-500"
                    required
                  />
                  <input
                    type="text"
                    value={newCollDisplayName}
                    onChange={(e) => setNewCollDisplayName(e.target.value)}
                    placeholder="UE5 Gameplay Docs"
                    className="bg-[#121214] border border-[#27272A] rounded px-2.5 py-1.5 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                    required
                  />
                </div>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setIsCreatingCollection(false)}
                    className="px-3 py-1 bg-[#121214] hover:bg-[#252529] border border-[#27272A] rounded text-[11px] text-zinc-300"
                  >
                    Cancelar
                  </button>
                  <button
                    type="submit"
                    disabled={isSubmitting}
                    className="px-3 py-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded text-[11px] font-semibold"
                  >
                    {isSubmitting ? 'Criando...' : 'Criar Coleção'}
                  </button>
                </div>
              </form>
            ) : (
              <div className="grid grid-cols-2 gap-2 max-h-40 overflow-y-auto pr-1">
                {collectionsList.length === 0 ? (
                  <div className="col-span-2 py-4 text-center text-zinc-500 text-xs">
                    Nenhuma coleção disponível. Crie uma nova coleção para começar.
                  </div>
                ) : (
                  collectionsList.map((coll) => {
                    const isActive =
                      activeCollection === coll.name || activeCollection === coll.qdrant_collection_name;
                    return (
                      <button
                        key={coll.name || coll.qdrant_collection_name}
                        onClick={() =>
                          setActiveCollection(coll.name || coll.qdrant_collection_name)
                        }
                        type="button"
                        className={`p-3 rounded-lg border text-left transition-all ${
                          isActive
                            ? 'bg-indigo-950/80 border-indigo-600 text-indigo-200 font-semibold'
                            : 'bg-[#1C1C1F] border-[#27272A] text-zinc-400 hover:text-zinc-200 hover:border-zinc-700'
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <span className="text-xs truncate font-mono">
                            {coll.name || coll.qdrant_collection_name}
                          </span>
                          {isActive && <Check className="w-3.5 h-3.5 text-indigo-400 flex-shrink-0" />}
                        </div>
                        <span className="text-[10px] text-zinc-500 block truncate mt-0.5">
                          {coll.display_name}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
          </div>

          <div className="pt-2 flex justify-end border-t border-[#27272A]">
            <button
              onClick={onClose}
              type="button"
              className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold shadow-md transition-all cursor-pointer"
            >
              Concluir & Aplicar
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
