/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Ingestion pipeline request/response schemas and job monitoring types.
 */

export type IngestionContentType = 'source_code' | 'documentation';

export interface IngestRequest {
  folder_path: string;
  content_type: IngestionContentType;
  ue_version?: string | null;
  project_source: string;
  sample_mode?: boolean;
  domain_id?: string | null;
}

export interface IngestAcceptedResponse {
  message: string;
  folder_path: string;
  content_type: IngestionContentType | string;
  project_source: string;
  sample_mode: boolean;
  domain_id: string;
}

export interface FileIngestionPayload {
  file: File;
  content_type: IngestionContentType;
  project_source: string;
  sample_mode?: boolean;
  domain_id?: string;
  ue_version?: string;
}

export type IngestionJobStatusType = 'pending' | 'running' | 'completed' | 'failed';

export interface IngestionJobStatus {
  id: string;
  folder_path: string;
  content_type: string;
  project_source: string;
  sample_mode: boolean;
  domain_id: string;
  status: IngestionJobStatusType;
  started_at: string;
  completed_at?: string;
  error_message?: string;
  message?: string;
  processed_files?: number;
  total_files?: number;
}
