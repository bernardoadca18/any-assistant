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
export * from './chatService';
export * from './ingestionService';
export * from './tenantService';
export * from './sessionsService';
