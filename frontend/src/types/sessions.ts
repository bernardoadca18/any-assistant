/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Chat Sessions & Message history types matching OpenAPI /v1/sessions specification.
 */

import type { RetrievedChunk } from './chat';

export interface ProjectRef {
  code: string;
}

export interface DocTypeRef {
  code: string;
}

export interface RetrievedChunkResponse {
  score: number;
  title: string;
  source_file: string;
  chunk_id: number;
  ue_version: string;
  summary_markdown: string;
  raw_content: string;
  rank_position: number;
  project?: ProjectRef | null;
  doc_type?: DocTypeRef | null;
}

export type MessageRole = 'user' | 'assistant' | 'system';
export type MessageStatus = 'success' | 'error' | 'pending' | 'streaming';

export interface MessageResponse {
  id: string;
  role: MessageRole | string;
  status: MessageStatus | string;
  content: string;
  error_detail?: string | null;
  model_used?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  sequence_number: number;
  created_at: string;
  retrieved_chunks?: RetrievedChunkResponse[] | RetrievedChunk[];
}

export interface SessionSummaryResponse {
  id: string;
  title: string;
  default_model: string;
  project_source_filter?: string | null;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface SessionDetailResponse extends SessionSummaryResponse {
  messages: MessageResponse[];
}
