/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * IngestionJobsView — monitoramento de jobs de ingestão ETL
 * com histórico, status e métricas de processamento.
 */

import React, { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileText,
  Loader2,
  Lock,
  RefreshCw,
  ShieldOff,
  XCircle,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import type { IngestionJobStatus } from '@/types';

const JOBS_STORAGE_KEY = 'ue5_ingestion_jobs';

const loadJobs = (): IngestionJobStatus[] => {
  try {
    const raw = localStorage.getItem(JOBS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
};

const saveJobs = (jobs: IngestionJobStatus[]) => {
  localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(jobs));
};

const StatusBadge: React.FC<{ status: IngestionJobStatus['status'] }> = ({ status }) => {
  const config = {
    pending: { bg: 'bg-zinc-800', text: 'text-zinc-400', icon: Clock },
    running: { bg: 'bg-indigo-950/40', text: 'text-indigo-400', icon: Loader2 },
    completed: { bg: 'bg-emerald-950/40', text: 'text-emerald-400', icon: CheckCircle2 },
    failed: { bg: 'bg-rose-950/40', text: 'text-rose-400', icon: XCircle },
  };
  const c = config[status];
  const Icon = c.icon;

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${c.bg} ${c.text}`}>
      <Icon className={`w-3 h-3 ${status === 'running' ? 'animate-spin' : ''}`} />
      {status}
    </span>
  );
};

export const IngestionJobsView: React.FC = () => {
  const { isAuthenticated, user } = useAppStore();

  const [jobs, setJobs] = useState<IngestionJobStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<'all' | 'completed' | 'failed' | 'running'>('all');

  const isAdmin = user?.role === 'admin';
  const canView = isAuthenticated;

  useEffect(() => {
    if (!canView) return;
    setLoading(true);
    setJobs(loadJobs());
    setLoading(false);
  }, [canView]);

  const handleRefresh = () => {
    setLoading(true);
    setJobs(loadJobs());
    setLoading(false);
  };

  const handleClearHistory = () => {
    if (confirm('Limpar todo o histórico de jobs? Esta ação não pode ser desfeita.')) {
      saveJobs([]);
      setJobs([]);
    }
  };

  const filteredJobs = filter === 'all' ? jobs : jobs.filter((j) => j.status === filter);

  // Stats
  const stats = {
    total: jobs.length,
    completed: jobs.filter((j) => j.status === 'completed').length,
    failed: jobs.filter((j) => j.status === 'failed').length,
    running: jobs.filter((j) => j.status === 'running').length,
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
                Faça login para visualizar o histórico de ingestão.
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
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
              <FileText className="w-5 h-5 text-indigo-400" />
              Jobs de Ingestão
            </h1>
            <p className="text-sm text-zinc-500 mt-1">
              Histórico de processamento ETL de código e documentação.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleRefresh}
              disabled={loading}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-[#27272A] text-zinc-400 hover:text-zinc-200 hover:border-zinc-600 transition-colors text-sm"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              Atualizar
            </button>
            {isAdmin && jobs.length > 0 && (
              <button
                onClick={handleClearHistory}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-rose-800/40 text-rose-400 hover:bg-rose-950/30 transition-colors text-sm"
              >
                Limpar Histórico
              </button>
            )}
          </div>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3">
            <div className="text-xs text-zinc-500">Total</div>
            <div className="text-xl font-semibold text-zinc-200">{stats.total}</div>
          </div>
          <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3">
            <div className="text-xs text-zinc-500">Concluídos</div>
            <div className="text-xl font-semibold text-emerald-400">{stats.completed}</div>
          </div>
          <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3">
            <div className="text-xs text-zinc-500">Falharam</div>
            <div className="text-xl font-semibold text-rose-400">{stats.failed}</div>
          </div>
          <div className="bg-[#121214] border border-[#27272A] rounded-lg p-3">
            <div className="text-xs text-zinc-500">Em Progresso</div>
            <div className="text-xl font-semibold text-indigo-400">{stats.running}</div>
          </div>
        </div>

        {/* Filters */}
        <div className="flex items-center gap-2">
          {(['all', 'completed', 'failed', 'running'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                filter === f
                  ? 'bg-indigo-600 text-white'
                  : 'bg-[#1C1C1F] text-zinc-400 hover:text-zinc-200 border border-[#27272A]'
              }`}
            >
              {f === 'all' ? 'Todos' : f === 'completed' ? 'Concluídos' : f === 'failed' ? 'Falharam' : 'Em Progresso'}
            </button>
          ))}
        </div>

        {/* Jobs List */}
        {loading ? (
          <div className="flex items-center justify-center py-16 text-zinc-600 text-sm gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            Carregando jobs...
          </div>
        ) : filteredJobs.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center gap-2">
            <FileText className="w-8 h-8 text-zinc-700" />
            <p className="text-sm text-zinc-500">Nenhum job encontrado.</p>
            <p className="text-xs text-zinc-600">
              Dispare uma ingestão na aba "Ingestão ETL" para começar.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {filteredJobs.map((job) => (
              <div
                key={job.id}
                className="bg-[#121214] border border-[#27272A] rounded-lg p-4 hover:border-[#3F3F46] transition-colors"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-sm font-medium text-zinc-200 truncate">
                        {job.project_source}
                      </span>
                      <StatusBadge status={job.status} />
                    </div>
                    <p className="text-xs font-mono text-zinc-500 truncate">{job.folder_path}</p>
                    <div className="flex items-center gap-3 mt-2 text-[10px] text-zinc-600">
                      <span>{job.content_type}</span>
                      <span>·</span>
                      <span>{job.domain_id}</span>
                      {job.sample_mode && (
                        <>
                          <span>·</span>
                          <span className="text-amber-400">sample_mode</span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-[10px] text-zinc-600">
                      {new Date(job.started_at).toLocaleString('pt-BR')}
                    </div>
                    {job.completed_at && (
                      <div className="text-[10px] text-zinc-500 mt-0.5">
                        Duração: {Math.round((new Date(job.completed_at).getTime() - new Date(job.started_at).getTime()) / 1000)}s
                      </div>
                    )}
                  </div>
                </div>

                {job.error_message && (
                  <div className="mt-3 p-2 rounded bg-rose-950/30 border border-rose-800/40 text-xs text-rose-300 flex items-start gap-2">
                    <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    <span className="break-all">{job.error_message}</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
