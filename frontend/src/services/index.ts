/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * Barrel export for all API & Backend services.
 *
 * NOTA: `sessionsService` foi adicionado na Fase 2 (não existia no barrel
 * original da Fase 1) para suportar o painel de histórico de sessões do
 * ChatWorkspace.
 */

export * from './apiClient';
export * from './authService';

// Chat service exports (excluding session-related functions to avoid conflict)
export {
  sendChatMessageStream,
  createChatReadableStream,
  sendChatMessageSync,
  uploadChatContextAttachment,
  type ChatStreamCallbacks,
} from './chatService';

// Session-related functions from chatService (renamed to avoid conflict)
export {
  listSessions as listSessionsFromChat,
  getSessionDetail,
  deleteSession as deleteSessionFromChat,
} from './chatService';

export * from './ingestionService';
export * from './tenantService';

// Sessions service is the primary source for session operations
export * from './sessionsService';
