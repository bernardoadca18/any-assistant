/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import {
  ChevronRight,
  Database,
  Key,
  LogOut,
  RefreshCw,
  ShieldAlert,
  User as UserIcon,
} from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import type { ActiveTab } from './Sidebar';

interface HeaderProps {
  activeTab: ActiveTab;
  sessionId: string;
  onOpenAuthModal: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  activeTab,
  sessionId,
  onOpenAuthModal,
}) => {
  const { user, isAuthenticated, activeDomainId, logout } = useAppStore();

  const getTabTitle = (tab: ActiveTab): string => {
    switch (tab) {
      case 'chat':
        return 'AI Assistant';
      case 'knowledge':
        return 'Knowledge Base & Ingestion';
      case 'devhub':
        return 'Developer Hub & API Specs';
    }
  };

  const activeDomainLabel = activeDomainId === 'godot_4' ? 'Godot 4.x' : 'Unreal Engine 5';

  return (
    <header className="h-14 border-b border-[#27272A] flex items-center justify-between px-6 bg-[#0E0E10] select-none flex-shrink-0">
      {/* Breadcrumb / Session Viewport */}
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <span className="font-medium text-zinc-300">{getTabTitle(activeTab)}</span>
        {activeTab === 'chat' && (
          <>
            <ChevronRight className="w-3.5 h-3.5 text-zinc-600" />
            <span className="text-zinc-100 font-mono text-xs bg-[#1C1C1F] border border-[#27272A] px-2 py-0.5 rounded">
              {sessionId}
            </span>
          </>
        )}
        <span className="ml-2 text-[10px] font-mono uppercase bg-indigo-950/60 text-indigo-300 border border-indigo-800/50 px-2 py-0.5 rounded">
          {activeDomainLabel}
        </span>
      </div>

      {/* Right User Actions & Tenant Profile */}
      <div className="flex items-center gap-3">
        {isAuthenticated && user ? (
          <div className="flex items-center gap-3">
            <div className="hidden sm:flex flex-col text-right">
              <span className="text-xs font-medium text-zinc-200">{user.name}</span>
              <span className="text-[10px] text-indigo-400 font-mono uppercase">
                {user.role} // {user.tenant_id}
              </span>
            </div>
            <button
              onClick={onOpenAuthModal}
              type="button"
              className="w-8 h-8 rounded-full bg-indigo-900 border border-indigo-700 flex items-center justify-center text-xs font-bold text-white hover:border-indigo-500 transition-colors cursor-pointer"
              title="Switch Credentials or API Key"
            >
              {user.name.slice(0, 2).toUpperCase()}
            </button>
            <button
              onClick={logout}
              type="button"
              className="p-1.5 rounded text-zinc-400 hover:text-rose-400 hover:bg-[#27272A] transition-colors"
              title="Logout"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        ) : (
          <button
            onClick={onOpenAuthModal}
            type="button"
            className="flex items-center gap-2 px-3 py-1.5 bg-[#1C1C1F] hover:bg-[#252529] border border-[#27272A] text-zinc-200 rounded-md text-xs font-medium transition-colors"
          >
            <Key className="w-3.5 h-3.5 text-indigo-400" />
            <span>Set API Key / Auth</span>
          </button>
        )}
      </div>
    </header>
  );
};
