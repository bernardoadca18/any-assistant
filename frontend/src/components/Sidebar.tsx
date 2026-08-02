/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect } from 'react';
import {
  Activity,
  BookOpen,
  CheckCircle2,
  Cpu,
  Database,
  FileText,
  FolderUp,
  Layers,
  MessageSquare,
  RefreshCw,
  Server,
  Settings,
  XCircle,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';

export type ActiveTab = 'chat' | 'knowledge' | 'devhub';

interface SidebarProps {
  activeTab: ActiveTab;
  setActiveTab: (tab: ActiveTab) => void;
  onOpenTenantModal: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  activeTab,
  setActiveTab,
  onOpenTenantModal,
}) => {
  const {
    activeTenant,
    activeCollection,
    activeDomainId,
    systemHealth,
    isHealthChecking,
    checkSystemHealth,
  } = useAppStore();

  useEffect(() => {
    checkSystemHealth();
    const interval = setInterval(() => {
      checkSystemHealth();
    }, 30000);
    return () => clearInterval(interval);
  }, [checkSystemHealth]);

  const navItems = [
    {
      id: 'chat' as ActiveTab,
      label: 'AI Assistant',
      icon: MessageSquare,
      badge: activeDomainId === 'godot_4' ? 'Godot' : 'UE5',
    },
    {
      id: 'knowledge' as ActiveTab,
      label: 'Knowledge Base',
      icon: FolderUp,
      badge: 'RAG',
    },
    {
      id: 'devhub' as ActiveTab,
      label: 'Developer Hub',
      icon: Layers,
      badge: 'API',
    },
  ];

  const isOnline = systemHealth?.status === 'ok';
  const isDegraded = systemHealth?.status === 'degraded';

  return (
    <aside className="w-64 flex-shrink-0 bg-[#121214] border-r border-[#27272A] flex flex-col h-full select-none">
      {/* Brand Header */}
      <div className="p-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center shadow-md shadow-indigo-600/20">
            <Cpu className="w-5 h-5 text-white" />
          </div>
          <div className="flex flex-col">
            <span className="font-bold text-lg tracking-tight text-[#E4E4E7] leading-none">
              RAG.CORE
            </span>
            <span className="text-[10px] font-mono uppercase text-indigo-400 tracking-wider mt-0.5">
              Code-to-Doc ETL
            </span>
          </div>
        </div>
      </div>

      {/* Tenant Switcher Card */}
      <div className="px-4 mb-6">
        <button
          onClick={onOpenTenantModal}
          type="button"
          className="w-full bg-[#1C1C1F] border border-[#27272A] rounded-md p-2.5 flex items-center justify-between cursor-pointer hover:bg-[#252529] hover:border-zinc-700 transition-all text-left group"
        >
          <div className="flex flex-col min-w-0">
            <span className="text-[10px] text-zinc-500 uppercase font-semibold tracking-wider">
              Active Tenant
            </span>
            <span className="text-sm font-medium text-zinc-200 truncate">
              {activeTenant}
            </span>
            <span className="text-[11px] text-indigo-400/90 font-mono truncate">
              {activeCollection}
            </span>
          </div>
          <Settings className="w-4 h-4 text-zinc-500 group-hover:text-indigo-400 transition-colors flex-shrink-0 ml-2" />
        </button>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 px-3 space-y-1 overflow-y-auto">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500 px-3 py-1">
          Navigation
        </div>
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              type="button"
              className={`w-full flex items-center justify-between px-3 py-2.5 text-sm font-medium rounded-md transition-all ${
                isActive
                  ? 'bg-[#1C1C1F] text-indigo-400 border-l-2 border-indigo-600 shadow-sm'
                  : 'text-zinc-400 hover:bg-[#1C1C1F] hover:text-zinc-200 border-l-2 border-transparent'
              }`}
            >
              <div className="flex items-center gap-3">
                <Icon className={`w-4 h-4 ${isActive ? 'text-indigo-400' : 'text-zinc-400'}`} />
                <span>{item.label}</span>
              </div>
              {item.badge && (
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded font-mono uppercase tracking-wide ${
                    isActive
                      ? 'bg-indigo-950/80 text-indigo-300 border border-indigo-800/60'
                      : 'bg-[#27272A] text-zinc-400'
                  }`}
                >
                  {item.badge}
                </span>
              )}
            </button>
          );
        })}

        {/* Quick External Links Section */}
        <div className="pt-6">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500 px-3 py-1">
            Engine Docs
          </div>
          <a
            href="https://dev.epicgames.com/documentation/en-us/unreal-engine"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-3 px-3 py-2 text-xs font-medium rounded-md text-zinc-400 hover:bg-[#1C1C1F] hover:text-zinc-200 transition-colors"
          >
            <BookOpen className="w-3.5 h-3.5 text-zinc-500" />
            <span>Unreal Engine 5</span>
          </a>
          <a
            href="https://docs.godotengine.org/en/stable/"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-3 px-3 py-2 text-xs font-medium rounded-md text-zinc-400 hover:bg-[#1C1C1F] hover:text-zinc-200 transition-colors"
          >
            <FileText className="w-3.5 h-3.5 text-zinc-500" />
            <span>Godot 4.x Docs</span>
          </a>
        </div>
      </nav>

      {/* Backend Status Footer */}
      <div className="p-4 mt-auto border-t border-[#27272A] bg-[#0E0E10]">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            {isHealthChecking ? (
              <RefreshCw className="w-3 h-3 text-indigo-400 animate-spin" />
            ) : isOnline ? (
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
            ) : isDegraded ? (
              <span className="w-2 h-2 rounded-full bg-amber-500" />
            ) : (
              <span className="w-2 h-2 rounded-full bg-rose-500" />
            )}
            <span className="text-[11px] font-medium text-zinc-300">
              {isHealthChecking
                ? 'Checking API...'
                : isOnline
                ? 'Backend Online (24ms)'
                : isDegraded
                ? 'Backend Degraded'
                : 'Dev Mode / Disconnected'}
            </span>
          </div>
          <button
            onClick={() => checkSystemHealth()}
            type="button"
            className="p-1 rounded text-zinc-500 hover:text-zinc-300 hover:bg-[#27272A] transition-colors"
            title="Refresh System Health"
          >
            <Activity className="w-3.5 h-3.5" />
          </button>
        </div>

        <div className="flex gap-2 mb-1.5">
          <div className="w-full h-1 bg-[#27272A] rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                isOnline
                  ? 'w-3/4 bg-indigo-500'
                  : isDegraded
                  ? 'w-1/2 bg-amber-500'
                  : 'w-1/4 bg-zinc-600'
              }`}
            />
          </div>
        </div>

        <div className="flex items-center justify-between text-[10px] text-zinc-500 uppercase tracking-tight">
          <span>Qdrant: ue5_docs</span>
          <span>LiteLLM Proxy</span>
        </div>
      </div>
    </aside>
  );
};
