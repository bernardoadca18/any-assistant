/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Aggregated TypeScript interfaces for all backend request and response payloads:
 * - Auth & User Session
 * - Tenants & Collections (Multi-Tenant RAG Configs)
 * - Chat Payloads, Hyperparameters & SSE Streaming
 * - Document Ingestion (Folder ETL Pipeline & Multipart Upload)
 * - System Status & Health Check
 * - Sessions & Chat Message History
 */

export * from './auth';
export * from './chat';
export * from './health';
export * from './ingestion';
export * from './sessions';
export * from './tenant';

// Common OpenAPI & Validation Error Schemas
export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface HTTPValidationError {
  detail: ValidationError[];
}

export interface ApiErrorResponse {
  status: number;
  message: string;
  detail?: string | ValidationError[] | Record<string, unknown>;
  code?: string;
}

export interface PaginatedQueryParams {
  page?: number;
  page_size?: number;
  is_active?: boolean;
}
