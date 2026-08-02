/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 *
 * Sessions Service:
 * Lista, recupera e remove sessões de chat persistidas no Postgres,
 * conforme GET /v1/sessions, GET /v1/sessions/{id} e DELETE /v1/sessions/{id}.
 *
 * NOTA: este arquivo não existia no pacote da Fase 1 (o barrel
 * services/index.ts só exportava apiClient/authService/chatService/
 * ingestionService/tenantService). Foi adicionado agora, na Fase 2, seguindo
 * exatamente o mesmo padrão dos demais serviços (apiClient centralizado,
 * sem estado local). Lembre de exportá-lo em services/index.ts.
 */

import { apiClient } from './apiClient';
import type { SessionDetailResponse, SessionSummaryResponse } from '@/types';

/**
 * Lista as sessões do tenant ativo (GET /v1/sessions).
 * O header X-Tenant-ID já é injetado automaticamente pelo apiClient.
 */
export async function listSessions(): Promise<SessionSummaryResponse[]> {
  const response = await apiClient.get<SessionSummaryResponse[]>('/sessions');
  return response.data;
}

/**
 * Obtém detalhes completos de uma sessão, incluindo todas as mensagens
 * e retrieved_chunks associados (GET /v1/sessions/{session_id}).
 */
export async function getSession(sessionId: string): Promise<SessionDetailResponse> {
  const response = await apiClient.get<SessionDetailResponse>(`/sessions/${sessionId}`);
  return response.data;
}

/**
 * Remove uma sessão do tenant ativo (DELETE /v1/sessions/{session_id}).
 * Backend retorna 204 No Content.
 */
export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete(`/sessions/${sessionId}`);
}
