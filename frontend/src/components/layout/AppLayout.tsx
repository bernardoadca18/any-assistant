/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * AppLayout — layout principal da aplicação (Fase 2):
 * - Sidebar retrátil com navegação entre seções
 * - Header superior: seletor de domínio, seletor de tenant, health badge,
 *   atalho para o Dev Hub
 * - Viewport central que renderiza a página ativa via <Outlet />-like prop
 *
 * Consome useAppStore (Fase 1) para tenant ativo, domínio ativo e healthcheck.
 * Não depende de react-router: a navegação é controlada por estado local
 * (activeView) e passada para as views via App.tsx.
 */

import React, { useEffect, useState } from 'react';
import {
  Activity,
  BookOpen,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Database,
  ExternalLink,
  Gamepad2,
  MessageSquare,
  Settings2,
  Users,
  Wrench,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { getDevHubLinks } from '@/config/links';
import type { DependencyStatusType } from '@/types';

export type AppView = 'chat' | 'ingestion' | 'tenants' | 'collections' | 'devhub';

interface NavItem {
  id: AppView;
  label: string;
  icon: React.ElementType;
  description: string;
}

const NAV_ITEMS: NavItem[] = [
  { id: 'chat', label: 'Chat Workspace', icon: MessageSquare, description: 'Converse com o assistente RAG' },
  { id: 'ingestion', label: 'Ingestão ETL', icon: Boxes, description: 'Processar pastas de código/docs' },
  { id: 'tenants', label: 'Tenants', icon: Users, description: 'Gerenciar tenants e API keys' },
  { id: 'collections', label: 'Coleções', icon: Database, description: 'Coleções vetoriais Qdrant' },
  { id: 'devhub', label: 'Dev Hub', icon: Wrench, description: 'Saúde do sistema e atalhos' },
];

const DOMAIN_OPTIONS: { id: string; label: string; icon: React.ElementType }[] = [
  { id: 'godot_4', label: 'Godot 4.x', icon: Gamepad2 },
  { id: 'ue5', label: 'Unreal Engine 5', icon: Boxes },
];

function statusColor(status: DependencyStatusType | undefined): string {
  switch (status) {
    case 'ok':
      return 'bg-emerald-500';
    case 'degraded':
      return 'bg-amber-500';
    case 'error':
      return 'bg-rose-500';
    default:
      return 'bg-zinc-600';
  }
}

function statusRing(status: DependencyStatusType | undefined): string {
  switch (status) {
    case 'ok':
      return 'ring-emerald-500/30';
    case 'degraded':
      return 'ring-amber-500/30';
    case 'error':
      return 'ring-rose-500/30';
    default:
      return 'ring-zinc-600/30';
  }
}

interface AppLayoutProps {
  activeView: AppView;
  onChangeView: (view: AppView) => void;
  children: React.ReactNode;
}

export const AppLayout: React.FC<AppLayoutProps> = ({ activeView, onChangeView, children }) => {
  const [collapsed, setCollapsed] = useState(false);
  const [devHubMenuOpen, setDevHubMenuOpen] = useState(false);

  const {
    activeTenant,
    activeDomainId,
    tenantsList,
    systemHealth,
    isHealthChecking,
    setActiveTenant,
    setActiveDomain,
    checkSystemHealth,
  } = useAppStore();

  useEffect(() => {
    checkSystemHealth();
    const interval = setInterval(() => {
      checkSystemHealth();
    }, 30_000);
    return () => clearInterval(interval);
  }, [checkSystemHealth]);

  const devHubLinks = getDevHubLinks();

  return (
    <div className="flex h-screen w-full bg-[#0A0A0B] text-[#E4E4E7] font-sans overflow-hidden">
      {/* ============================== SIDEBAR ============================== */}
      <aside
        className={`flex flex-col border-r border-[#1F1F22] bg-[#0C0C0E] transition-all duration-200 ${
          collapsed ? 'w-[64px]' : 'w-[248px]'
        }`}
      >
        <div className="h-14 flex items-center px-4 border-b border-[#1F1F22] shrink-0">
          {!collapsed ? (
            <div className="flex items-center gap-2 min-w-0">
              <div className="w-6 h-6 rounded-md bg-indigo-600 flex items-center justify-center shrink-0">
                <Gamepad2 className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="text-sm font-semibold text-zinc-100 truncate">GameDev RAG</span>
            </div>
          ) : (
            <div className="w-6 h-6 rounded-md bg-indigo-600 flex items-center justify-center mx-auto">
              <Gamepad2 className="w-3.5 h-3.5 text-white" />
            </div>
          )}
        </div>

        <nav className="flex-1 py-3 px-2 space-y-0.5 overflow-y-auto">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = activeView === item.id;
            return (
              <button
                key={item.id}
                type="button"
                title={collapsed ? item.label : undefined}
                onClick={() => onChangeView(item.id)}
                className={`w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors group ${
                  active
                    ? 'bg-indigo-600/15 text-indigo-300'
                    : 'text-zinc-400 hover:bg-[#18181B] hover:text-zinc-200'
                } ${collapsed ? 'justify-center' : ''}`}
              >
                <Icon className={`w-4 h-4 shrink-0 ${active ? 'text-indigo-400' : 'text-zinc-500 group-hover:text-zinc-300'}`} />
                {!collapsed && <span className="truncate">{item.label}</span>}
              </button>
            );
          })}
        </nav>

        <div className="p-2 border-t border-[#1F1F22]">
          <button
            type="button"
            onClick={() => setCollapsed((c) => !c)}
            className="w-full flex items-center justify-center gap-2 rounded-lg px-2.5 py-2 text-xs text-zinc-500 hover:bg-[#18181B] hover:text-zinc-300 transition-colors"
          >
            {collapsed ? <ChevronRight className="w-4 h-4" /> : (
              <>
                <ChevronLeft className="w-4 h-4" />
                <span>Recolher</span>
              </>
            )}
          </button>
        </div>
      </aside>

      {/* ============================== MAIN ============================== */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* ---------------------------- HEADER ---------------------------- */}
        <header className="h-14 shrink-0 border-b border-[#1F1F22] bg-[#0C0C0E] flex items-center justify-between px-4 gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-sm font-medium text-zinc-200 truncate">
              {NAV_ITEMS.find((n) => n.id === activeView)?.label}
            </span>
            <span className="hidden md:inline text-xs text-zinc-600 truncate">
              {NAV_ITEMS.find((n) => n.id === activeView)?.description}
            </span>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {/* Domain selector */}
            <div className="flex items-center bg-[#141416] border border-[#27272A] rounded-lg p-0.5">
              {DOMAIN_OPTIONS.map((opt) => {
                const Icon = opt.icon;
                const active = activeDomainId === opt.id;
                return (
                  <button
                    key={opt.id}
                    type="button"
                    onClick={() => setActiveDomain(opt.id)}
                    className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
                      active ? 'bg-indigo-600 text-white' : 'text-zinc-400 hover:text-zinc-200'
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5" />
                    <span className="hidden lg:inline">{opt.label}</span>
                  </button>
                );
              })}
            </div>

            {/* Tenant selector */}
            <div className="relative">
              <select
                value={activeTenant}
                onChange={(e) => setActiveTenant(e.target.value)}
                className="appearance-none bg-[#141416] border border-[#27272A] rounded-lg pl-3 pr-7 py-1.5 text-xs font-medium text-zinc-200 outline-none focus:border-indigo-500 cursor-pointer max-w-[140px]"
              >
                {tenantsList.length === 0 && <option value={activeTenant}>{activeTenant}</option>}
                {tenantsList.map((t) => (
                  <option key={t.id} value={t.slug || t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <Users className="w-3 h-3 text-zinc-500 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" />
            </div>

            {/* Health badge */}
            <button
              type="button"
              onClick={() => checkSystemHealth()}
              title={systemHealth?.errorMessage || `Status: ${systemHealth?.status ?? 'desconhecido'}`}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border border-[#27272A] bg-[#141416] hover:bg-[#18181B] transition-colors`}
            >
              <span className="relative flex h-2 w-2">
                {isHealthChecking && (
                  <span
                    className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-60 ${statusColor(
                      systemHealth?.status
                    )}`}
                  />
                )}
                <span className={`relative inline-flex rounded-full h-2 w-2 ${statusColor(systemHealth?.status)}`} />
              </span>
              <span className="hidden md:inline text-zinc-300 capitalize">
                {systemHealth?.status ?? '...'}
              </span>
            </button>

            {/* Dev Hub quick links */}
            <div className="relative">
              <button
                type="button"
                onClick={() => setDevHubMenuOpen((v) => !v)}
                className="flex items-center justify-center w-8 h-8 rounded-lg border border-[#27272A] bg-[#141416] hover:bg-[#18181B] text-zinc-400 hover:text-zinc-200 transition-colors"
                title="Atalhos do Dev Hub"
              >
                <Settings2 className="w-4 h-4" />
              </button>

              {devHubMenuOpen && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setDevHubMenuOpen(false)} />
                  <div className="absolute right-0 mt-2 w-64 bg-[#121214] border border-[#27272A] rounded-xl shadow-2xl z-20 overflow-hidden">
                    <div className="px-3 py-2 border-b border-[#27272A] text-[11px] font-semibold uppercase tracking-wider text-zinc-500">
                      Atalhos rápidos
                    </div>
                    <div className="max-h-72 overflow-y-auto">
                      {devHubLinks.map((link) => (
                        <a
                          key={link.id}
                          href={link.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="flex items-center gap-2 px-3 py-2 text-xs text-zinc-300 hover:bg-[#1C1C1F] transition-colors"
                        >
                          {link.category === 'vector_db' ? (
                            <Database className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
                          ) : link.category === 'engine_docs' ? (
                            <BookOpen className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
                          ) : (
                            <Activity className="w-3.5 h-3.5 text-zinc-500 shrink-0" />
                          )}
                          <span className="truncate flex-1">{link.title}</span>
                          <ExternalLink className="w-3 h-3 text-zinc-600 shrink-0" />
                        </a>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </header>

        {/* -------------------------- VIEWPORT -------------------------- */}
        <main className="flex-1 flex overflow-hidden bg-[#0A0A0B]">{children}</main>
      </div>
    </div>
  );
};

export { statusColor, statusRing };
