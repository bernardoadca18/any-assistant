/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * ChatSettingsPopover — popover de parâmetros do chat:
 * modelo (busca dinâmica do LiteLLM), top_k (slider 1-20) e
 * project_source_filter.
 */

import React, { useState, useEffect } from 'react';
import { Filter, Settings2, Sparkles, X, Loader2 } from 'lucide-react';
import { listModels } from '@/services/chatService';
import type { ModelInfo } from '@/types';

export interface ChatSettings {
  model: string;
  top_k: number;
  project_source_filter: string;
}

interface ChatSettingsPopoverProps {
  settings: ChatSettings;
  onChange: (settings: ChatSettings) => void;
}

export const ChatSettingsPopover: React.FC<ChatSettingsPopoverProps> = ({ settings, onChange }) => {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<ChatSettings>(settings);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  const openPopover = () => {
    setDraft(settings);
    setOpen(true);
    // Fetch models when popover opens if not already loaded
    if (models.length === 0 && !modelsLoading) {
      fetchModels();
    }
  };

  const fetchModels = async () => {
    setModelsLoading(true);
    setModelsError(null);
    try {
      const modelList = await listModels();
      setModels(modelList);
      // Auto-select first model if none in draft
      if (modelList.length > 0 && !draft.model) {
        setDraft((d) => ({ ...d, model: modelList[0].id }));
      }
    } catch (err) {
      setModelsError('Falha ao carregar modelos do LiteLLM');
      console.error('Failed to fetch models:', err);
    } finally {
      setModelsLoading(false);
    }
  };

  const apply = () => {
    onChange(draft);
    setOpen(false);
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={openPopover}
        className="flex items-center gap-1.5 px-2.5 py-2 rounded-lg border border-[#27272A] bg-[#141416] hover:bg-[#18181B] text-zinc-400 hover:text-zinc-200 transition-colors text-xs"
        title="Parâmetros do chat"
      >
        <Settings2 className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Parâmetros</span>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full right-0 mb-2 w-80 bg-[#121214] border border-[#27272A] rounded-xl shadow-2xl z-20 overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#27272A]">
              <span className="text-sm font-semibold text-zinc-100 flex items-center gap-1.5">
                <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
                Parâmetros do chat
              </span>
              <button type="button" onClick={() => setOpen(false)} className="text-zinc-500 hover:text-zinc-300" aria-label="Fechar">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="p-4 space-y-4">
              {/* Model */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">Modelo (LiteLLM)</label>
                {modelsLoading && (
                  <div className="flex items-center gap-2 text-xs text-zinc-500">
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-indigo-400" />
                    Carregando modelos...
                  </div>
                )}
                {modelsError && (
                  <div className="text-xs text-rose-400 flex items-center gap-1">
                    <span>{modelsError}</span>
                    <button
                      type="button"
                      onClick={fetchModels}
                      className="text-indigo-400 hover:underline"
                    >
                      Tentar novamente
                    </button>
                  </div>
                )}
                <select
                  value={draft.model}
                  onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
                  disabled={modelsLoading}
                >
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id}
                    </option>
                  ))}
                  {!modelsLoading && models.length === 0 && (
                    <option value="" disabled>
                      Nenhum modelo disponível
                    </option>
                  )}
                  {!modelsLoading && models.length > 0 && !models.some(m => m.id === draft.model) && (
                    <option value={draft.model}>
                      {draft.model} (atual)
                    </option>
                  )}
                </select>
              </div>

              {/* top_k slider */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-semibold text-zinc-300">top_k (chunks recuperados)</label>
                  <span className="text-xs font-mono text-indigo-400">{draft.top_k}</span>
                </div>
                <input
                  type="range"
                  min={1}
                  max={20}
                  step={1}
                  value={draft.top_k}
                  onChange={(e) => setDraft((d) => ({ ...d, top_k: Number(e.target.value) }))}
                  className="w-full accent-indigo-500"
                />
                <div className="flex justify-between text-[10px] text-zinc-600">
                  <span>1</span>
                  <span>20</span>
                </div>
              </div>

              {/* project_source_filter */}
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1">
                  <Filter className="w-3 h-3" />
                  Filtro de project_source (opcional)
                </label>
                <input
                  type="text"
                  value={draft.project_source_filter}
                  onChange={(e) => setDraft((d) => ({ ...d, project_source_filter: e.target.value }))}
                  placeholder="ex: Lyra, EngineSource, GodotDocs"
                  className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                />
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-[#27272A] bg-[#0E0E10]">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="px-3 py-1.5 rounded-lg text-xs text-zinc-400 hover:text-zinc-200 transition-colors"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={apply}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
              >
                Aplicar
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
};
