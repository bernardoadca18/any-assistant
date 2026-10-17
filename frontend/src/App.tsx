/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * App — ponto de montagem da Fase 2: conecta AppLayout (sidebar + header)
 * às 5 views (Chat, Ingestão, Tenants, Coleções, Dev Hub) via estado local
 * de navegação (sem react-router, seguindo o padrão simples já usado na
 * Fase 1 pelo App.tsx original com activeTab).
 */

import React, { useState, useMemo } from 'react';
import { AppLayout, type AppView } from '@/components/layout/AppLayout';
import { ChatWorkspace } from '@/views/ChatWorkspace';
import { FolderIngestionView } from '@/views/FolderIngestionView';
import { IngestionJobsView } from '@/views/IngestionJobsView';
import { TenantManagerView } from '@/views/TenantManagerView';
import { CollectionManagerView } from '@/views/CollectionManagerView';
import { CollectionConfigView } from '@/views/CollectionConfigView';
import { AdminDashboardView } from '@/views/AdminDashboardView';
import { DevHubView } from '@/views/DevHubView';
import { AuthView } from '@/views/AuthView';
import { useAppStore } from '@/store/useAppStore';

export default function App() {
  const [activeView, setActiveView] = useState<AppView>('chat');
  const { activeTenant, activeDomainId, token } = useAppStore();
  // Clear in-memory chat when credentials change without placing credentials in React keys.
  const credentialContext = useMemo(() => crypto.randomUUID(), [token]);

  return (
    <>
      <AppLayout activeView={activeView} onChangeView={setActiveView}>
        <div className={activeView === 'chat' ? 'chat-view-container' : 'hidden'}>
          <ChatWorkspace key={`${activeTenant}:${activeDomainId}:${credentialContext}`} />
        </div>
        {activeView === 'ingestion' && <FolderIngestionView />}
        {activeView === 'jobs' && <IngestionJobsView />}
        {activeView === 'tenants' && <TenantManagerView />}
        {activeView === 'collections' && <CollectionManagerView />}
        {activeView === 'collection-config' && <CollectionConfigView />}
        {activeView === 'admin' && <AdminDashboardView />}
        {activeView === 'devhub' && <DevHubView />}
        {activeView === 'auth' && <AuthView />}
      </AppLayout>
    </>
  );
}
