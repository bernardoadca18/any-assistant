/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Chat Request, Response, Retrieved Chunks, Hyperparameters, and SSE Streaming Event types.
 */

export interface ChatHyperparameters {
  model: string;
  top_k: number;
  temperature?: number;
  top_p?: number;
  system_prompt?: string | null;
  domain_id?: string | null;
  project_source_filter?: string | null;
}

export interface ChatRequest {
  session_id: string;
  message: string;
  model?: string;
  top_k?: number;
  stream?: boolean;
  project_source_filter?: string | null;
  domain_id?: string | null;
  temperature?: number;
  top_p?: number;
  system_prompt?: string | null;
}

export interface RetrievedChunk {
  score: number;
  summary_markdown: string;
  title: string;
  description?: string;
  source_file: string;
  chunk_id: number;
  ue_version?: string;
  doc_type?: string;
  project_source?: string;
  raw_content: string;
  rank_position?: number;
}

export interface ChatResponse {
  answer: string;
  model_used: string;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  retrieved_chunks: RetrievedChunk[];
}

// SSE Streaming Events emitted by POST /v1/chat when stream: true
export interface SSEEventSources {
  type: 'sources';
  retrieved_chunks: RetrievedChunk[];
}

export interface SSEEventToken {
  type: 'token';
  content: string;
}

export interface SSEEventDone {
  type: 'done';
  session_id: string;
  model_used: string;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  created_at: string;
}

export interface SSEEventError {
  type: 'error';
  detail: string;
}

export type ChatSSEEvent =
  | SSEEventSources
  | SSEEventToken
  | SSEEventDone
  | SSEEventError;

// Inline Context / Attached File for Chat
export interface ChatAttachment {
  id: string;
  name: string;
  size: number;
  content_type: string;
  raw_content?: string;
  uploaded_at: string;
}
