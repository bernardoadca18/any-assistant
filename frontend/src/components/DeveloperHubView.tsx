/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState } from 'react';
import {
  Activity,
  BookOpen,
  CheckCircle2,
  Cpu,
  Database,
  ExternalLink,
  FileText,
  Key,
  Layers,
  RefreshCw,
  Server,
  Terminal,
  XCircle,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { getDevHubLinks } from '@/config/links';
import { apiClient } from '@/services/apiClient';

export const DeveloperHubView: React.FC = () => {
  const {
    systemHealth,
    isHealthChecking,
    checkSystemHealth,
    activeTenant,
    activeCollection,
    activeDomainId,
    token,
  } = useAppStore();

  const [testEndpoint, setTestEndpoint] = useState('/health');
  const [testMethod, setTestMethod] = useState<'GET' | 'POST'>('GET');
  const [testResponse, setTestResponse] = useState<string | null>(null);
  const [testStatus, setTestStatus] = useState<number | null>(null);
  const [isLoadingTest, setIsLoadingTest] = useState(false);

  useEffect(() => {
    if (!systemHealth) {
      checkSystemHealth();
    }
  }, [systemHealth, checkSystemHealth]);

  const devLinks = getDevHubLinks();

  const getIcon = (name: string) => {
    switch (name) {
      case 'FileText':
        return <FileText className="w-5 h-5 text-indigo-400" />;
      case 'BookOpen':
        return <BookOpen className="w-5 h-5 text-indigo-400" />;
      case 'Database':
        return <Database className="w-5 h-5 text-indigo-400" />;
      case 'Cpu':
        return <Cpu className="w-5 h-5 text-indigo-400" />;
      default:
        return <ExternalLink className="w-5 h-5 text-indigo-400" />;
    }
  };

  const handleTestApiCall = async () => {
    setIsLoadingTest(true);
    setTestResponse(null);
    setTestStatus(null);

    try {
      let res;
      if (testMethod === 'GET') {
        res = await apiClient.get(testEndpoint);
      } else {
        res = await apiClient.post(testEndpoint, {});
      }
      setTestStatus(res.status);
      setTestResponse(JSON.stringify(res.data, null, 2));
    } catch (err: unknown) {
      if (typeof err === 'object' && err !== null && 'status' in err) {
        const enhanced = err as { status: number; message: string; detail?: unknown };
        setTestStatus(enhanced.status);
        setTestResponse(
          JSON.stringify(
            {
              error: enhanced.message,
              detail: enhanced.detail,
            },
            null,
            2
          )
        );
      } else {
        setTestStatus(500);
        setTestResponse(
          JSON.stringify(
            {
              error: err instanceof Error ? err.message : 'Falha na requisição.',
            },
            null,
            2
          )
        );
      }
    } finally {
      setIsLoadingTest(false);
    }
  };

  const isOnline = systemHealth?.status === 'ok';
  const isDegraded = systemHealth?.status === 'degraded';

  return (
    <div className="flex-1 overflow-y-auto bg-[#0A0A0B] text-[#E4E4E7] p-8 space-y-8 select-none">
      {/* Top Banner */}
      <div className="max-w-5xl mx-auto space-y-2">
        <div className="flex items-center gap-2 text-indigo-400 text-xs font-mono uppercase tracking-wider">
          <Layers className="w-4 h-4" />
          <span>Developer Central // OpenAPI & Swagger</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-100">
          Developer Hub & API Explorer
        </h1>
        <p className="text-sm text-zinc-400 max-w-3xl leading-relaxed">
          Substitua o uso de cURL e Swagger bruto através dos utilitários centralizados abaixo. Monitore os microsserviços do backend (FastAPI, Qdrant Vector DB, LiteLLM Proxy), gerencie tokens JWT ou inspecione rotas ao vivo.
        </p>
      </div>

      <div className="max-w-5xl mx-auto space-y-8">
        {/* Backend Healthcheck Indicator Card */}
        <div className="bg-[#121214] border border-[#27272A] rounded-xl p-6">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#27272A] pb-5">
            <div className="flex items-center gap-3">
              <div
                className={`w-10 h-10 rounded-xl flex items-center justify-center ${
                  isOnline
                    ? 'bg-emerald-950/80 border border-emerald-800 text-emerald-400'
                    : isDegraded
                    ? 'bg-amber-950/80 border border-amber-800 text-amber-400'
                    : 'bg-rose-950/80 border border-rose-800 text-rose-400'
                }`}
              >
                <Activity className="w-5 h-5" />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="text-base font-semibold text-zinc-100">
                    System Status & Dependencies (/v1/health)
                  </h3>
                  <span
                    className={`text-[10px] px-2 py-0.5 rounded font-mono uppercase font-bold ${
                      isOnline
                        ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                        : isDegraded
                        ? 'bg-amber-950 text-amber-300 border border-amber-800'
                        : 'bg-rose-950 text-rose-300 border border-rose-800'
                    }`}
                  >
                    {systemHealth?.status || 'desconectado'}
                  </span>
                </div>
                <p className="text-xs text-zinc-400 mt-0.5 font-mono">
                  Última checagem: {systemHealth?.lastChecked || 'Nunca'} // Env:{' '}
                  {systemHealth?.environment || 'dev'}
                </p>
              </div>
            </div>

            <button
              onClick={() => checkSystemHealth()}
              disabled={isHealthChecking}
              type="button"
              className="px-4 py-2 bg-[#1C1C1F] hover:bg-[#252529] border border-[#27272A] rounded-lg text-xs font-semibold text-zinc-200 flex items-center gap-2 transition-colors self-start md:self-auto cursor-pointer"
            >
              <RefreshCw
                className={`w-3.5 h-3.5 text-indigo-400 ${
                  isHealthChecking ? 'animate-spin' : ''
                }`}
              />
              <span>{isHealthChecking ? 'Verificando...' : 'Atualizar Status'}</span>
            </button>
          </div>

          {/* Dependencies breakdown */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 pt-5">
            <div className="bg-[#1C1C1F] border border-[#27272A] rounded-lg p-3.5 space-y-1">
              <span className="text-[10px] font-mono uppercase text-zinc-500">
                Active Tenant
              </span>
              <p className="text-sm font-semibold text-zinc-200 truncate">
                {activeTenant}
              </p>
              <span className="text-[10px] text-indigo-400 font-mono">
                X-Tenant-ID Header
              </span>
            </div>

            <div className="bg-[#1C1C1F] border border-[#27272A] rounded-lg p-3.5 space-y-1">
              <span className="text-[10px] font-mono uppercase text-zinc-500">
                Qdrant Collection
              </span>
              <p className="text-sm font-semibold text-zinc-200 truncate">
                {activeCollection}
              </p>
              <span className="text-[10px] text-indigo-400 font-mono">
                Vector Dimension: 1024
              </span>
            </div>

            <div className="bg-[#1C1C1F] border border-[#27272A] rounded-lg p-3.5 space-y-1">
              <span className="text-[10px] font-mono uppercase text-zinc-500">
                Domain Target
              </span>
              <p className="text-sm font-semibold text-zinc-200 truncate font-mono">
                {activeDomainId}
              </p>
              <span className="text-[10px] text-indigo-400 font-mono">
                X-Domain-ID Header
              </span>
            </div>

            <div className="bg-[#1C1C1F] border border-[#27272A] rounded-lg p-3.5 space-y-1">
              <span className="text-[10px] font-mono uppercase text-zinc-500">
                Auth Security
              </span>
              <p className="text-sm font-semibold text-zinc-200 truncate">
                {token ? 'Bearer Token OK' : 'Sem Autenticação'}
              </p>
              <span className="text-[10px] text-indigo-400 font-mono">
                {token ? 'Authorization: Bearer ***' : 'Usando Fallback API'}
              </span>
            </div>
          </div>
        </div>

        {/* Quick Links Grid */}
        <div className="space-y-4">
          <h2 className="text-base font-semibold text-zinc-100 flex items-center gap-2">
            <BookOpen className="w-4 h-4 text-indigo-400" />
            <span>Developer Hub Quick Links</span>
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {devLinks.map((link) => (
              <a
                key={link.id}
                href={link.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group bg-[#121214] hover:bg-[#1C1C1F] border border-[#27272A] hover:border-indigo-600/60 rounded-xl p-5 flex flex-col justify-between transition-all duration-200 shadow-sm"
              >
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="w-10 h-10 rounded-lg bg-[#1C1C1F] border border-[#27272A] flex items-center justify-center group-hover:border-indigo-500/50 transition-colors">
                      {getIcon(link.iconName)}
                    </div>
                    <span className="text-[10px] font-mono uppercase px-2 py-0.5 rounded bg-[#27272A] text-zinc-400">
                      {link.category}
                    </span>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-zinc-100 group-hover:text-indigo-400 transition-colors">
                      {link.title}
                    </h3>
                    <p className="text-xs text-zinc-400 mt-1 leading-relaxed line-clamp-2">
                      {link.description}
                    </p>
                  </div>
                </div>
                <div className="flex items-center justify-between pt-4 mt-4 border-t border-[#27272A] text-xs text-indigo-400 font-medium">
                  <span>Abrir Documentação</span>
                  <ExternalLink className="w-3.5 h-3.5 opacity-0 group-hover:opacity-100 transition-opacity" />
                </div>
              </a>
            ))}
          </div>
        </div>

        {/* Live OpenAPI Endpoint Tester */}
        <div className="bg-[#121214] border border-[#27272A] rounded-xl p-6 space-y-5">
          <div className="flex items-center justify-between border-b border-[#27272A] pb-4">
            <div className="flex items-center gap-2">
              <Terminal className="w-4 h-4 text-indigo-400" />
              <h2 className="text-base font-semibold text-zinc-100">
                OpenAPI Live Endpoint Tester (substitui cURL)
              </h2>
            </div>
            <span className="text-xs font-mono text-zinc-500">
              Axios Wrapper Configurado
            </span>
          </div>

          <div className="flex flex-col md:flex-row gap-3">
            <select
              value={testMethod}
              onChange={(e) => setTestMethod(e.target.value as 'GET' | 'POST')}
              className="bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-indigo-400 font-bold outline-none focus:border-indigo-500 w-24"
            >
              <option value="GET">GET</option>
              <option value="POST">POST</option>
            </select>

            <input
              type="text"
              value={testEndpoint}
              onChange={(e) => setTestEndpoint(e.target.value)}
              placeholder="/health or /admin/tenants or /sessions"
              className="flex-1 bg-[#1C1C1F] border border-[#27272A] rounded-lg px-3 py-2 text-xs font-mono text-zinc-200 outline-none focus:border-indigo-500"
            />

            <button
              onClick={handleTestApiCall}
              disabled={isLoadingTest}
              type="button"
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-5 py-2 rounded-lg text-xs font-semibold flex items-center justify-center gap-2 transition-all cursor-pointer"
            >
              <Terminal className="w-3.5 h-3.5" />
              <span>{isLoadingTest ? 'Disparando...' : 'Enviar Request'}</span>
            </button>
          </div>

          {testResponse && (
            <div className="space-y-2 pt-2">
              <div className="flex items-center justify-between text-xs font-mono">
                <span className="text-zinc-400">Resposta da API Backend:</span>
                <span
                  className={`px-2 py-0.5 rounded font-bold ${
                    testStatus && testStatus < 400
                      ? 'bg-emerald-950 text-emerald-400 border border-emerald-800'
                      : 'bg-rose-950 text-rose-400 border border-rose-800'
                  }`}
                >
                  HTTP {testStatus}
                </span>
              </div>
              <pre className="bg-[#0E0E10] border border-[#27272A] rounded-lg p-4 font-mono text-xs text-zinc-300 overflow-x-auto max-h-72 whitespace-pre">
                {testResponse}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
