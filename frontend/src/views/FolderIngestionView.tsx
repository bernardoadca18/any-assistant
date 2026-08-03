/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * FolderIngestionView — dispara ingestão ETL de pastas de código/documentação
 * via POST /v1/ingest/process-folder (retorna 202 Accepted, processamento em
 * background no Redis/Qdrant).
 */

import React, { useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  FolderInput,
  HelpCircle,
  Lock,
  Loader2,
  Package,
  Tag,
  Zap,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { processFolder, getIngestionJobHistory } from '@/services/ingestionService';
import type { IngestAcceptedResponse, IngestionContentType, IngestionJobStatus } from '@/types';

export const FolderIngestionView: React.FC = () => {
  const { activeDomainId, isAuthenticated } = useAppStore();

  // All useState hooks must be declared BEFORE any early returns (React Hooks rules)
  const [folderPath, setFolderPath] = useState('');
  const [contentType, setContentType] = useState<IngestionContentType>('source_code');
  const [projectSource, setProjectSource] = useState('');
  const [ueVersion, setUeVersion] = useState('');
  const [sampleMode, setSampleMode] = useState(false);
  const [showSampleTooltip, setShowSampleTooltip] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastResponse, setLastResponse] = useState<IngestAcceptedResponse | null>(null);
  const [recentJobs, setRecentJobs] = useState<IngestionJobStatus[]>(() => getIngestionJobHistory());

  const isUE5 = activeDomainId === 'ue5';

  // Not authenticated state - rendered AFTER all hooks are declared
  if (!isAuthenticated) {
    return (
      <div className="flex-1 overflow-y-auto p-6 md:p-8">
        <div className="max-w-3xl mx-auto">
          <div className="flex flex-col items-center justify-center py-16 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-zinc-800/50 flex items-center justify-center">
              <Lock className="w-7 h-7 text-zinc-500" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-zinc-300">Autenticação Necessária</h2>
              <p className="text-sm text-zinc-500 mt-1 max-w-md">
                Faça login com uma API Key válida para executar pipelines de ingestão de código e documentação.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!folderPath.trim()) {
      setError('Informe o caminho absoluto da pasta dentro do container.');
      return;
    }
    if (!projectSource.trim()) {
      setError('Informe o project_source (ex: Lyra, EngineSource, GodotDocs).');
      return;
    }

    setSubmitting(true);
    try {
      const response = await processFolder({
        folder_path: folderPath.trim(),
        content_type: contentType,
        project_source: projectSource.trim(),
        ue_version: ueVersion.trim() || undefined,
        sample_mode: sampleMode,
        domain_id: activeDomainId,
      });
      setLastResponse(response);
      setRecentJobs(getIngestionJobHistory());
    } catch (err: unknown) {
      const message =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Falha ao disparar a ingestão.';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-8">
      <div className="max-w-2xl mx-auto space-y-6">
        <div>
          <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
            <FolderInput className="w-5 h-5 text-indigo-400" />
            Ingestão de Pastas (ETL)
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            Processa código-fonte ou documentação de uma pasta montada no container e
            popula a coleção vetorial Qdrant do domínio ativo (
            <span className="font-mono text-zinc-400">{activeDomainId}</span>).
          </p>
        </div>

        <form onSubmit={handleSubmit} className="bg-[#121214] border border-[#27272A] rounded-xl p-5 space-y-5">
          {error && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-rose-950/30 border border-rose-800/60 text-rose-300 text-xs">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">
              Caminho da pasta (absoluto, dentro do container)
            </label>
            <input
              type="text"
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
              placeholder="/data/projects/Lyra/Source"
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300">Tipo de conteúdo</label>
              <select
                value={contentType}
                onChange={(e) => setContentType(e.target.value as IngestionContentType)}
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
              >
                <option value="source_code">source_code</option>
                <option value="documentation">documentation</option>
              </select>
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1">
                <Tag className="w-3 h-3" />
                UE Version (opcional)
              </label>
              <input
                type="text"
                value={ueVersion}
                onChange={(e) => setUeVersion(e.target.value)}
                placeholder="5.4"
                disabled={!isUE5}
                className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300 flex items-center gap-1">
              <Package className="w-3 h-3" />
              Project Source
            </label>
            <input
              type="text"
              value={projectSource}
              onChange={(e) => setProjectSource(e.target.value)}
              placeholder="Lyra, EngineSource, GodotDocs..."
              className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs text-zinc-200 outline-none focus:border-indigo-500"
              required
            />
          </div>

          <div className="flex items-center justify-between p-3 rounded-lg bg-[#0E0E10] border border-[#27272A]">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-zinc-300">sample_mode</span>
              <div className="relative">
                <button
                  type="button"
                  onMouseEnter={() => setShowSampleTooltip(true)}
                  onMouseLeave={() => setShowSampleTooltip(false)}
                  className="text-zinc-500 hover:text-zinc-300"
                >
                  <HelpCircle className="w-3.5 h-3.5" />
                </button>
                {showSampleTooltip && (
                  <div className="absolute left-0 bottom-full mb-2 w-64 p-2.5 rounded-lg bg-[#1C1C1F] border border-[#27272A] text-[11px] text-zinc-400 shadow-xl z-10">
                    Ative para projetos grandes de exemplo (Lyra, City Sample) — processa
                    uma amostra representativa dos arquivos em vez do projeto inteiro,
                    útil para testes rápidos de pipeline.
                  </div>
                )}
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={sampleMode}
              onClick={() => setSampleMode((v) => !v)}
              className={`relative w-10 h-5.5 rounded-full transition-colors ${
                sampleMode ? 'bg-indigo-600' : 'bg-[#27272A]'
              }`}
              style={{ height: '1.375rem' }}
            >
              <span
                className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                  sampleMode ? 'translate-x-4' : 'translate-x-0'
                }`}
              />
            </button>
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="w-full flex items-center justify-center gap-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-60 text-white text-sm font-medium py-2.5 transition-colors"
          >
            {submitting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Disparando ingestão...
              </>
            ) : (
              <>
                <Zap className="w-4 h-4" />
                Iniciar Ingestão
              </>
            )}
          </button>
        </form>

        {/* HTTP 202 confirmation card */}
        {lastResponse && (
          <div className="bg-emerald-950/20 border border-emerald-800/40 rounded-xl p-5 space-y-3">
            <div className="flex items-center gap-2 text-emerald-400">
              <CheckCircle2 className="w-4 h-4" />
              <span className="text-sm font-semibold">HTTP 202 Accepted — job em background</span>
            </div>
            <p className="text-xs text-zinc-400">{lastResponse.message}</p>
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="bg-[#0E0E10] border border-[#27272A] rounded-lg p-2.5">
                <div className="text-[10px] text-zinc-600 uppercase tracking-wide">Pasta</div>
                <div className="font-mono text-zinc-300 truncate">{lastResponse.folder_path}</div>
              </div>
              <div className="bg-[#0E0E10] border border-[#27272A] rounded-lg p-2.5">
                <div className="text-[10px] text-zinc-600 uppercase tracking-wide">Tipo</div>
                <div className="font-mono text-zinc-300">{lastResponse.content_type}</div>
              </div>
              <div className="bg-[#0E0E10] border border-[#27272A] rounded-lg p-2.5">
                <div className="text-[10px] text-zinc-600 uppercase tracking-wide">Project Source</div>
                <div className="font-mono text-zinc-300">{lastResponse.project_source}</div>
              </div>
              <div className="bg-[#0E0E10] border border-[#27272A] rounded-lg p-2.5">
                <div className="text-[10px] text-zinc-600 uppercase tracking-wide">Domain / Sample</div>
                <div className="font-mono text-zinc-300">
                  {lastResponse.domain_id} · {lastResponse.sample_mode ? 'sample' : 'full'}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Recent jobs history */}
        {recentJobs.length > 0 && (
          <div className="space-y-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-600">
              Jobs recentes (local)
            </h2>
            <div className="space-y-1.5">
              {recentJobs.slice(0, 8).map((job) => (
                <div
                  key={job.id}
                  className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg bg-[#121214] border border-[#27272A] text-xs"
                >
                  <span className="font-mono text-zinc-400 truncate flex-1">{job.folder_path}</span>
                  <span className="text-zinc-600 shrink-0">{job.project_source}</span>
                  <span
                    className={`shrink-0 px-2 py-0.5 rounded-full text-[10px] font-medium ${
                      job.status === 'completed'
                        ? 'bg-emerald-950/40 text-emerald-400'
                        : job.status === 'failed'
                        ? 'bg-rose-950/40 text-rose-400'
                        : 'bg-amber-950/40 text-amber-400'
                    }`}
                  >
                    {job.status}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
