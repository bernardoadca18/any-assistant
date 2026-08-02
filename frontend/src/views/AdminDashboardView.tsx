/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * Admin Dashboard View — painel administrativo com métricas,
 * estatísticas e visão geral do sistema multi-tenant.
 */

import React, { useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Database,
  FileText,
  Key,
  Loader2,
  Lock,
  Server,
  ShieldOff,
  Users,
  Zap,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import * as tenantService from '@/services/tenantService';

interface DashboardStats {
  totalTenants: number;
  totalCollections: number;
  totalApiKeys: number;
  activeTenants: number;
  collectionsByDomain: { godot_4: number; ue5: number };
}

interface SystemStatus {
  database: 'ok' | 'degraded' | 'error';
  qdrant: 'ok' | 'degraded' | 'error';
  redis: 'ok' | 'degraded' | 'error';
  litellm: 'ok' | 'degraded' | 'error';
}

const StatusBadge: React.FC<{ status: 'ok' | 'degraded' | 'error' }> = ({ status }) => {
  const config = {
    ok: { bg: 'bg-emerald-950/40', text: 'text-emerald-400', label: 'Online' },
    degraded: { bg: 'bg-amber-950/40', text: 'text-amber-400', label: 'Degradado' },
    error: { bg: 'bg-rose-950/40', text: 'text-rose-400', label: 'Offline' },
  };
  const c = config[status];
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${c.bg} ${c.text}`}>
      {c.label}
    </span>
  );
};

const StatCard: React.FC<{
  icon: React.ElementType;
  label: string;
  value: number | string;
  subtext?: string;
  color: 'indigo' | 'emerald' | 'amber' | 'rose';
}> = ({ icon: Icon, label, value, subtext, color }) => {
  const colorClasses = {
    indigo: 'bg-indigo-950/30 border-indigo-800/40 text-indigo-400',
    emerald: 'bg-emerald-950/30 border-emerald-800/40 text-emerald-400',
    amber: 'bg-amber-950/30 border-amber-800/40 text-amber-400',
    rose: 'bg-rose-950/30 border-rose-800/40 text-rose-400',
  };

  return (
    <div className="bg-[#121214] border border-[#27272A] rounded-xl p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-400">{label}</span>
        <div className={`p-2 rounded-lg ${colorClasses[color]}`}>
          <Icon className="w-4 h-4" />
        </div>
      </div>
      <div className="text-2xl font-bold text-zinc-100">{value}</div>
      {subtext && <p className="text-[11px] text-zinc-500">{subtext}</p>}
    </div>
  );
};

export const AdminDashboardView: React.FC = () => {
  const { isAuthenticated, user, systemHealth } = useAppStore();

  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [systemStatus, setSystemStatus] = useState<SystemStatus>({
    database: 'ok',
    qdrant: 'ok',
    redis: 'ok',
    litellm: 'ok',
  });
  const [loading, setLoading] = useState(true);

  const isAdmin = user?.role === 'admin';
  const canView = isAuthenticated && isAdmin;

  useEffect(() => {
    if (!canView) return;

    const loadDashboardData = async () => {
      setLoading(true);
      try {
        // Load tenants stats
        const tenantsRes = await tenantService.listTenants({ page_size: 100 });
        const tenants = tenantsRes.items || [];

        // Load collections stats
        const collectionsRes = await tenantService.listCollections({ page_size: 100 });
        const collections = collectionsRes.items || [];

        // Calculate stats
        const totalApiKeys = tenants.reduce((sum, t) => sum + (t.api_keys_count || 0), 0);
        const activeTenants = tenants.filter((t) => t.is_active).length;
        const collectionsByDomain = {
          godot_4: collections.filter((c) => c.domain_id === 'godot_4').length,
          ue5: collections.filter((c) => c.domain_id === 'ue5').length,
        };

        setStats({
          totalTenants: tenants.length,
          totalCollections: collections.length,
          totalApiKeys,
          activeTenants,
          collectionsByDomain,
        });

        // Infer system status from health check
        if (systemHealth) {
          setSystemStatus({
            database: systemHealth.database?.status || 'ok',
            qdrant: systemHealth.qdrant?.status || 'ok',
            redis: systemHealth.redis?.status || 'ok',
            litellm: systemHealth.litellm?.status || 'ok',
          });
        }
      } catch {
        setStats(null);
      } finally {
        setLoading(false);
      }
    };

    loadDashboardData();
  }, [canView, systemHealth]);

  // Not authenticated state
  if (!isAuthenticated) {
    return (
      <div className="flex-1 overflow-y-auto p-6 md:p-8">
        <div className="max-w-6xl mx-auto">
          <div className="flex flex-col items-center justify-center py-16 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-zinc-800/50 flex items-center justify-center">
              <Lock className="w-7 h-7 text-zinc-500" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-zinc-300">Autenticação Necessária</h2>
              <p className="text-sm text-zinc-500 mt-1 max-w-md">
                Faça login com uma API Key de administrador para acessar o painel.
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
        <div className="max-w-6xl mx-auto">
          <div className="flex flex-col items-center justify-center py-16 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-amber-900/30 flex items-center justify-center">
              <ShieldOff className="w-7 h-7 text-amber-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-zinc-300">Privilégios de Admin Necessários</h2>
              <p className="text-sm text-zinc-500 mt-1 max-w-md">
                Este painel requer privilégios administrativos. Use uma API Key com is_admin=true.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-6 md:p-8">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Header */}
        <div>
          <h1 className="text-lg font-semibold text-zinc-100 flex items-center gap-2">
            <Activity className="w-5 h-5 text-indigo-400" />
            Painel Administrativo
          </h1>
          <p className="text-sm text-zinc-500 mt-1">
            Visão geral do sistema multi-tenant RAG.
          </p>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16 text-zinc-600 text-sm gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            Carregando métricas...
          </div>
        ) : (
          <>
            {/* Stats Grid */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard
                icon={Users}
                label="Tenants"
                value={stats?.totalTenants ?? 0}
                subtext={`${stats?.activeTenants ?? 0} ativos`}
                color="indigo"
              />
              <StatCard
                icon={Database}
                label="Coleções"
                value={stats?.totalCollections ?? 0}
                subtext={`Godot: ${stats?.collectionsByDomain.godot_4 ?? 0} · UE5: ${stats?.collectionsByDomain.ue5 ?? 0}`}
                color="emerald"
              />
              <StatCard
                icon={Key}
                label="API Keys"
                value={stats?.totalApiKeys ?? 0}
                subtext="Total de chaves ativas"
                color="amber"
              />
              <StatCard
                icon={Zap}
                label="Sistema"
                value="Operacional"
                subtext="Todos os serviços online"
                color="rose"
              />
            </div>

            {/* System Status */}
            <div className="bg-[#121214] border border-[#27272A] rounded-xl p-5 space-y-4">
              <h2 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
                <Server className="w-4 h-4 text-zinc-400" />
                Status dos Serviços
              </h2>

              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                <div className="flex items-center justify-between p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="flex items-center gap-2">
                    <Database className="w-4 h-4 text-zinc-500" />
                    <span className="text-xs text-zinc-300">PostgreSQL</span>
                  </div>
                  <StatusBadge status={systemStatus.database} />
                </div>

                <div className="flex items-center justify-between p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="flex items-center gap-2">
                    <Boxes className="w-4 h-4 text-zinc-500" />
                    <span className="text-xs text-zinc-300">Qdrant</span>
                  </div>
                  <StatusBadge status={systemStatus.qdrant} />
                </div>

                <div className="flex items-center justify-between p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="flex items-center gap-2">
                    <Zap className="w-4 h-4 text-zinc-500" />
                    <span className="text-xs text-zinc-300">Redis</span>
                  </div>
                  <StatusBadge status={systemStatus.redis} />
                </div>

                <div className="flex items-center justify-between p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A]">
                  <div className="flex items-center gap-2">
                    <Activity className="w-4 h-4 text-zinc-500" />
                    <span className="text-xs text-zinc-300">LiteLLM</span>
                  </div>
                  <StatusBadge status={systemStatus.litellm} />
                </div>
              </div>
            </div>

            {/* Quick Actions */}
            <div className="bg-[#121214] border border-[#27272A] rounded-xl p-5 space-y-4">
              <h2 className="text-sm font-semibold text-zinc-200">Ações Rápidas</h2>

              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                <button
                  type="button"
                  className="flex items-center gap-3 p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A] hover:border-indigo-600 transition-colors text-left"
                >
                  <div className="p-2 bg-indigo-950/40 rounded-lg">
                    <Users className="w-4 h-4 text-indigo-400" />
                  </div>
                  <div>
                    <span className="text-xs font-medium text-zinc-200 block">Criar Tenant</span>
                    <span className="text-[10px] text-zinc-500">Novo namespace isolado</span>
                  </div>
                </button>

                <button
                  type="button"
                  className="flex items-center gap-3 p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A] hover:border-emerald-600 transition-colors text-left"
                >
                  <div className="p-2 bg-emerald-950/40 rounded-lg">
                    <Database className="w-4 h-4 text-emerald-400" />
                  </div>
                  <div>
                    <span className="text-xs font-medium text-zinc-200 block">Nova Coleção</span>
                    <span className="text-[10px] text-zinc-500">Coleção vetorial Qdrant</span>
                  </div>
                </button>

                <button
                  type="button"
                  className="flex items-center gap-3 p-3 bg-[#1C1C1F] rounded-lg border border-[#27272A] hover:border-amber-600 transition-colors text-left"
                >
                  <div className="p-2 bg-amber-950/40 rounded-lg">
                    <FileText className="w-4 h-4 text-amber-400" />
                  </div>
                  <div>
                    <span className="text-xs font-medium text-zinc-200 block">Ingestão ETL</span>
                    <span className="text-[10px] text-zinc-500">Processar documentação</span>
                  </div>
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
};
