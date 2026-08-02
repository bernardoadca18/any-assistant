/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * DevHubView — Central do Desenvolvedor:
 * - Health Matrix: status individual + latência de cada dependência (GET /v1/health)
 * - Quick Links: Swagger, ReDoc, Qdrant Dashboard, LiteLLM, docs oficiais UE5/Godot
 */

import React, { useEffect } from 'react';
import {
  Activity,
  AlertCircle,
  BookOpen,
  CheckCircle2,
  Cpu,
  Database,
  ExternalLink,
  FileText,
  RefreshCw,
  Wrench,
  XCircle,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { getDevHubLinks } from '@/config/links';
import type { DependencyStatusType } from '@/types';

const ICON_MAP: Record<string, React.ElementType> = {
  FileText,
  Database,
  Cpu,
  BookOpen,
  ExternalLink,
  Activity,
};

function statusVisual(status: DependencyStatusType) {
  switch (status) {
    case 'ok':
      return {
        icon: CheckCircle2,
        color: 'text-emerald-400',
        bg: 'bg-emerald-950/30',
        border: 'border-emerald-800/40',
      };
    case 'degraded':
      return {
        icon: AlertCircle,
        color: 'text-amber-400',
        bg: 'bg-amber-950/30',
        border: 'border-amber-800/40',
      };
    case 'error':
      return {
        icon: XCircle,
        color: 'text-rose-400',
        bg: 'bg-rose-950/30',
        border: 'border-rose-800/40',
      };
    default:
      return {
        icon: AlertCircle,
        color: 'text-zinc-500',
        bg: 'bg-zinc-900/30',
        border: 'border-zinc-700/40',
      };
  }
}

export const DevHubView: React.FC = () => {
  const { systemHealth, isHealthChecking, checkSystemHealth } = useAppStore();

  useEffect(() => {
    checkSystemHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const devHubLinks = getDevHubLinks();
  const dependencies = systemHealth?.dependencies || {};
  const dependencyEntries = Object.entries(dependencies);

  const linksByCategory = devHubLinks.reduce<Record<string, typeof devHubLinks>>((acc, link) => {
    acc[link.category] = acc[link.category] || [];
    acc[link.category].push(link);
    return acc;
  }, {});

  const categoryLabels: Record<string, string> = {
    api_docs: 'Documentação da API',
    vector_db: 'Vector Database',
    llm_proxy: 'LLM Proxy',
    engine_docs: 'Documentação de Engine',
  };

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-8">
      <div className="max-w-5xl mx-auto space-y-8">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
              <Wrench className="w-5 h-5 text-indigo-400" />
              Central do Desenvolvedor
            </h1>
            <p className="text-sm text-zinc-500 mt-1">
              Saúde do sistema, latências e atalhos úteis para o dia a dia.
            </p>
          </div>
          <button
            onClick={() => checkSystemHealth()}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg border border-[#27272A] bg-[#141416] hover:bg-[#18181B] text-zinc-300 text-xs font-medium transition-colors"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isHealthChecking ? 'animate-spin' : ''}`} />
            Atualizar
          </button>
        </div>

        {/* ============================ HEALTH MATRIX ============================ */}
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-600">
              Health Matrix
            </h2>
            {systemHealth && (
              <span className="text-[11px] text-zinc-600">
                Última checagem: {new Date(systemHealth.lastChecked).toLocaleTimeString('pt-BR')}
              </span>
            )}
          </div>

          {systemHealth?.errorMessage ? (
            <div className="flex items-start gap-2 p-4 rounded-xl bg-rose-950/20 border border-rose-800/40 text-rose-300 text-sm">
              <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <div>
                <p className="font-medium">Não foi possível conectar ao backend</p>
                <p className="text-xs text-rose-400/80 mt-1">{systemHealth.errorMessage}</p>
              </div>
            </div>
          ) : dependencyEntries.length === 0 ? (
            <div className="flex items-center justify-center py-10 text-zinc-600 text-sm">
              {isHealthChecking ? 'Verificando dependências...' : 'Nenhuma dependência reportada.'}
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {dependencyEntries.map(([depName, dep]) => {
                const visual = statusVisual(dep.status);
                const Icon = visual.icon;
                return (
                  <div
                    key={depName}
                    className={`rounded-xl border p-4 ${visual.bg} ${visual.border}`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-semibold text-zinc-200 capitalize">{depName}</span>
                      <Icon className={`w-4 h-4 ${visual.color}`} />
                    </div>
                    <div className="mt-2 flex items-baseline gap-1.5">
                      <span className={`text-xs font-medium capitalize ${visual.color}`}>
                        {dep.status}
                      </span>
                      {dep.latency_ms !== null && dep.latency_ms !== undefined && (
                        <span className="text-[11px] text-zinc-500 font-mono">
                          · {dep.latency_ms.toFixed(0)}ms
                        </span>
                      )}
                    </div>
                    {dep.detail && (
                      <p className="text-[11px] text-zinc-500 mt-1.5 truncate" title={dep.detail}>
                        {dep.detail}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {systemHealth?.version && (
            <p className="text-[11px] text-zinc-600">
              versão {systemHealth.version} · ambiente {systemHealth.environment}
            </p>
          )}
        </section>

        {/* ============================ QUICK LINKS ============================ */}
        <section className="space-y-4">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-600">
            Quick Links
          </h2>

          {Object.entries(linksByCategory).map(([category, links]) => (
            <div key={category} className="space-y-2">
              <h3 className="text-xs font-medium text-zinc-500">
                {categoryLabels[category] || category}
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {links.map((link) => {
                  const Icon = ICON_MAP[link.iconName] || ExternalLink;
                  return (
                    <a
                      key={link.id}
                      href={link.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="group flex items-start gap-3 p-4 rounded-xl bg-[#121214] border border-[#27272A] hover:border-indigo-600/50 hover:bg-[#15151A] transition-colors"
                    >
                      <div className="w-8 h-8 rounded-lg bg-indigo-600/15 flex items-center justify-center shrink-0">
                        <Icon className="w-4 h-4 text-indigo-400" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm font-medium text-zinc-200 truncate">
                            {link.title}
                          </span>
                          <ExternalLink className="w-3 h-3 text-zinc-600 group-hover:text-indigo-400 transition-colors shrink-0" />
                        </div>
                        <p className="text-[11px] text-zinc-500 mt-0.5 line-clamp-2">
                          {link.description}
                        </p>
                      </div>
                    </a>
                  );
                })}
              </div>
            </div>
          ))}
        </section>
      </div>
    </div>
  );
};
