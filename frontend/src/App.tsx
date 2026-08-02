/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * App — ponto de montagem da Fase 2: conecta AppLayout (sidebar + header)
 * às 5 views (Chat, Ingestão, Tenants, Coleções, Dev Hub) via estado local
 * de navegação (sem react-router, seguindo o padrão simples já usado na
 * Fase 1 pelo App.tsx original com activeTab).
 */

import React, { useState } from 'react';
import { AppLayout, type AppView } from '@/components/layout/AppLayout';
import { ChatWorkspace } from '@/views/ChatWorkspace';
import { FolderIngestionView } from '@/views/FolderIngestionView';
import { TenantManagerView } from '@/views/TenantManagerView';
import { CollectionManagerView } from '@/views/CollectionManagerView';
import { DevHubView } from '@/views/DevHubView';

export default function App() {
  const [activeView, setActiveView] = useState<AppView>('chat');

  return (
    <AppLayout activeView={activeView} onChangeView={setActiveView}>
      {activeView === 'chat' && <ChatWorkspace />}
      {activeView === 'ingestion' && <FolderIngestionView />}
      {activeView === 'tenants' && <TenantManagerView />}
      {activeView === 'collections' && <CollectionManagerView />}
      {activeView === 'devhub' && <DevHubView />}
    </AppLayout>
  );
}