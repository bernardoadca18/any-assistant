/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Chat Service:
 * - Streaming chat via browser standard Fetch API & ReadableStream (SSE token-by-token)
 * - Synchronous chat requests (stream: false)
 * - Inline context / file attachments for chat sessions
 * - Session history retrieval & deletion matching /v1/sessions
 */

import { apiClient, getAuthHeaders } from './apiClient';
import { getApiBaseUrl } from '@/config/links';
import type {
  ChatAttachment,
  ChatRequest,
  ChatResponse,
  ChatSSEEvent,
  RetrievedChunk,
  SessionDetailResponse,
  SessionSummaryResponse,
} from '@/types';

export interface ChatStreamCallbacks {
  onSources?: (chunks: RetrievedChunk[]) => void;
  onToken?: (token: string) => void;
  onDone?: (summary: {
    session_id: string;
    model_used: string;
    prompt_tokens?: number | null;
    completion_tokens?: number | null;
    created_at: string;
  }) => void;
  onError?: (errorDetail: string) => void;
}

/**
 * Sends a chat message with Server-Sent Events (SSE) Streaming enabled.
 * Uses native browser Fetch API and ReadableStream to consume text token by token.
 * 
 * Automatically buffers partial SSE chunks across TCP frames and invokes
 * typed callbacks for 'sources', 'token', 'done', and 'error' events.
 */
export async function sendChatMessageStream(
  request: ChatRequest,
  callbacks: ChatStreamCallbacks,
  abortSignal?: AbortSignal
): Promise<void> {
  const url = `${getApiBaseUrl()}/chat`;
  const headers = getAuthHeaders();

  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      ...request,
      stream: true,
    }),
    signal: abortSignal,
  });

  if (!response.ok) {
    let errorMessage = `HTTP Error ${response.status}: ${response.statusText}`;
    try {
      const errorJson = await response.json();
      if (errorJson.detail) {
        errorMessage = typeof errorJson.detail === 'string'
          ? errorJson.detail
          : JSON.stringify(errorJson.detail);
      }
    } catch {
      const errorText = await response.text();
      if (errorText) errorMessage = errorText;
    }
    callbacks.onError?.(errorMessage);
    throw new Error(errorMessage);
  }

  if (!response.body) {
    const err = 'O servidor não retornou uma stream de dados compatível com ReadableStream.';
    callbacks.onError?.(err);
    throw new Error(err);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Split SSE data lines separated by double linebreaks or newlines
      const lines = buffer.split(/\r?\n/);
      // Keep trailing incomplete line in the buffer
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.startsWith('data: ')) continue;

        const jsonStr = trimmed.slice(6).trim();
        if (!jsonStr || jsonStr === '[DONE]') continue;

        try {
          const event = JSON.parse(jsonStr) as ChatSSEEvent;
          switch (event.type) {
            case 'sources':
              callbacks.onSources?.(event.retrieved_chunks || []);
              break;
            case 'token':
              callbacks.onToken?.(event.content);
              break;
            case 'done':
              callbacks.onDone?.({
                session_id: event.session_id,
                model_used: event.model_used,
                prompt_tokens: event.prompt_tokens,
                completion_tokens: event.completion_tokens,
                created_at: event.created_at,
              });
              break;
            case 'error':
              callbacks.onError?.(event.detail);
              break;
          }
        } catch (parseError) {
          console.warn('Erro ao decodificar evento SSE no chat:', parseError, jsonStr);
        }
      }
    }
  } catch (error: unknown) {
    if (abortSignal?.aborted) {
      // User or component aborted the stream gracefully
      return;
    }
    const msg = error instanceof Error ? error.message : 'Falha na conexão SSE de chat.';
    callbacks.onError?.(msg);
    throw error;
  } finally {
    reader.releaseLock();
  }
}

/**
 * Creates and returns a standard browser ReadableStream<ChatSSEEvent>
 * that transforms the raw response body bytes into parsed SSE event objects.
 * Ideal for advanced React hooks or custom async iterators.
 */
export async function createChatReadableStream(
  request: ChatRequest,
  abortSignal?: AbortSignal
): Promise<ReadableStream<ChatSSEEvent>> {
  const url = `${getApiBaseUrl()}/chat`;
  const headers = getAuthHeaders();

  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      ...request,
      stream: true,
    }),
    signal: abortSignal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Falha ao iniciar ReadableStream: HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  return new ReadableStream<ChatSSEEvent>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          controller.close();
          return;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data: ')) continue;

          const jsonStr = trimmed.slice(6).trim();
          if (!jsonStr || jsonStr === '[DONE]') continue;

          try {
            const event = JSON.parse(jsonStr) as ChatSSEEvent;
            controller.enqueue(event);
          } catch {
            // ignore unparseable partial event
          }
        }
      } catch (err) {
        controller.error(err);
      }
    },
    cancel() {
      reader.cancel();
    },
  });
}

/**
 * Sends a chat message synchronously (stream: false) and returns complete ChatResponse.
 */
export async function sendChatMessageSync(request: ChatRequest): Promise<ChatResponse> {
  const response = await apiClient.post<ChatResponse>('/chat', {
    ...request,
    stream: false,
  });
  return response.data;
}

/**
 * Uploads an inline context file / attachment to be associated with a chat session.
 */
export async function uploadChatContextAttachment(
  file: File,
  sessionId: string,
  onProgress?: (percent: number) => void
): Promise<ChatAttachment> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('session_id', sessionId);

  try {
    const response = await apiClient.post<ChatAttachment>('/chat/attachments', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      onUploadProgress: (event) => {
        if (onProgress && event.total) {
          onProgress(Math.round((event.loaded * 100) / event.total));
        }
      },
    });
    return response.data;
  } catch {
    // If backend endpoint for attachment upload isn't present, read file locally
    // and provide temporary attachment preview item for developer hub testing
    const textContent = await file.text();
    return {
      id: `att-${Date.now()}`,
      name: file.name,
      size: file.size,
      content_type: file.type || 'text/plain',
      raw_content: textContent.slice(0, 10000), // First 10k chars for prompt preview
      uploaded_at: new Date().toISOString(),
    };
  }
}

// ============================================================================
// SESSION HISTORY OPERATIONS (/v1/sessions)
// ============================================================================

/**
 * Lists all persisted chat sessions for the active tenant.
 */
export async function listSessions(): Promise<SessionSummaryResponse[]> {
  const response = await apiClient.get<SessionSummaryResponse[]>('/sessions');
  return response.data;
}

/**
 * Retrieves full message history and retrieved chunks for a specific session.
 */
export async function getSessionDetail(sessionId: string): Promise<SessionDetailResponse> {
  const response = await apiClient.get<SessionDetailResponse>(`/sessions/${sessionId}`);
  return response.data;
}

/**
 * Deletes a chat session permanently.
 */
export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete(`/sessions/${sessionId}`);
}
