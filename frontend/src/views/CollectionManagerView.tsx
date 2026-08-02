/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * CollectionManagerView — visualização em grid das coleções vetoriais Qdrant
 * por tenant (GET /v1/admin/collections) + modal de criação
 * (POST /v1/admin/collections).
 */

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  Boxes,
  Database,
  Gamepad2,
  Loader2,
  Plus,
  X,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as tenantService from '@/services/tenantService';
import type { CollectionCreate, CollectionResponse } from '@/types';

const CreateCollectionModal: React.FC<{
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
  defaultTenantId: string;
}> = ({ isOpen, onClose, onCreated, defaultTenantId }) => {
  const [name, setName] = useState('');
  const [qdrantCollectionName, setQdrantCollectionName] = useState('');
  const [domainId, setDomainId] = useState('godot_4');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!name.trim() || !qdrantCollectionName.trim() || !displayName.trim()) {
      setError('Nome, qdrant_collection_name e display_name são obrigatórios.');
      return;
    }
    setSubmitting(true);
    try {
      const payload: CollectionCreate = {
        tenant_id: defaultTenantId,
        name: name.trim(),
        qdrant_collection_name: qdrantCollectionName.trim(),
        domain_id: domainId,
        display_name: displayName.trim(),
        description: description.trim() || undefined,
      };
      await tenantService.createCollection(payload);
      onCreated();
      onClose();
      setName('');
      setQdrantCollectionName('');
      setDisplayName('');
      setDescription('');
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Falha ao criar coleção.';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-[#121214] border border-[#27272A] rounded-xl max-w-md w-full shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272A]">
          <h2 className="text-sm font-semibold text-zinc-100">Nova Coleção Vetorial</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
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
            <label className="text-xs font-semibold text-zinc-300">Nome (interno)</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="godot4-official-docs"
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Qdrant Collection Name</label>
            <input
              value={qdrantCollectionName}
              onChange={(e) => setQdrantCollectionName(e.target.value)}
              placeholder="godot4_docs_v1"
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Domain ID</label>
            <select
              value={domainId}
              onChange={(e) => setDomainId(e.target.value)}
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
            >
              <option value="godot_4">godot_4</option>
              <option value="ue5">ue5</option>
            </select>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Display Name</label>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Godot 4.x — Documentação Oficial"
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
              Criar Coleção
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

const CollectionCard: React.FC<{ collection: CollectionResponse }> = ({ collection }) => {
  const isUE5 = collection.domain_id === 'ue5';
  return (
    <div className="bg-[#121214] border border-[#27272A] rounded-xl p-4 space-y-3 hover:border-[#3F3F46] transition-colors">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-zinc-100 truncate">{collection.display_name}</h3>
          <p className="text-[11px] font-mono text-zinc-600 truncate">{collection.qdrant_collection_name}</p>
        </div>
        <span
          className={`shrink-0 text-[10px] px-2 py-0.5 rounded-full font-medium ${
            collection.is_active ? 'bg-emerald-950/40 text-emerald-400' : 'bg-zinc-800 text-zinc-500'
          }`}
        >
          {collection.is_active ? 'ativa' : 'inativa'}
        </span>
      </div>

      {collection.description && (
        <p className="text-xs text-zinc-500 line-clamp-2">{collection.description}</p>
      )}

      <div className="flex items-center gap-2 pt-1">
        <span
          className={`flex items-center gap-1 text-[10px] px-2 py-1 rounded-md ${
            isUE5 ? 'bg-indigo-950/40 text-indigo-300' : 'bg-emerald-950/30 text-emerald-300'
          }`}
        >
          {isUE5 ? <Boxes className="w-3 h-3" /> : <Gamepad2 className="w-3 h-3" />}
          {collection.domain_id}
        </span>
        {collection.embedding && (
          <span className="text-[10px] px-2 py-1 rounded-md bg-[#1C1C1F] text-zinc-500 font-mono">
            {collection.embedding.dimension}d
          </span>
        )}
      </div>
    </div>
  );
};

export const CollectionManagerView: React.FC = () => {
  const { activeTenant } = useAppStore();
  const [collections, setCollections] = useState<CollectionResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [domainFilter, setDomainFilter] = useState<string>('all');

  const loadCollections = async () => {
    setLoading(true);
    try {
      const res = await tenantService.listCollections({
        tenant_id: activeTenant,
        page_size: 100,
        domain_id: domainFilter === 'all' ? undefined : domainFilter,
      });
      setCollections(res.items || []);
    } catch {
      setCollections([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCollections();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTenant, domainFilter]);

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-8">
      <div className="max-w-5xl mx-auto space-y-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
              <Database className="w-5 h-5 text-indigo-400" />
              Coleções Vetoriais
            </h1>
            <p className="text-sm text-zinc-500 mt-1">
              Coleções Qdrant do tenant <span className="font-mono text-zinc-400">{activeTenant}</span>.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <select
              value={domainFilter}
              onChange={(e) => setDomainFilter(e.target.value)}
              className="bg-[#141416] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-300 outline-none focus:border-indigo-500"
            >
              <option value="all">Todos os domínios</option>
              <option value="godot_4">Godot 4.x</option>
              <option value="ue5">Unreal Engine 5</option>
            </select>
            <button
              onClick={() => setCreateModalOpen(true)}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition-colors"
            >
              <Plus className="w-4 h-4" />
              Nova Coleção
            </button>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16 text-zinc-600 text-sm gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            Carregando coleções...
          </div>
        ) : collections.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center gap-2">
            <Database className="w-8 h-8 text-zinc-700" />
            <p className="text-sm text-zinc-500">Nenhuma coleção encontrada para este tenant.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {collections.map((c) => (
              <CollectionCard key={c.id || c.qdrant_collection_name} collection={c} />
            ))}
          </div>
        )}
      </div>

      <CreateCollectionModal
        isOpen={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        onCreated={loadCollections}
        defaultTenantId={activeTenant}
      />
    </div>
  );
};
