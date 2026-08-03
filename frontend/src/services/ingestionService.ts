/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Ingestion Service:
 * Handles triggering background ETL folder ingestions (POST /v1/ingest/process-folder),
 * multipart/form-data single file/folder uploads, and job status tracking.
 */

import { apiClient } from './apiClient';
import type {
  FileIngestionPayload,
  IngestAcceptedResponse,
  IngestRequest,
  IngestionJobStatus,
} from '@/types';

// In-memory or local storage registry of triggered background jobs for Dev Hub tracking
const INGESTION_JOBS_STORAGE_KEY = 'ue5_rag_ingestion_jobs';

/**
 * Dispatches an asynchronous Code-to-Doc ETL ingestion pipeline on a folder path
 * mounted inside the container (POST /v1/ingest/process-folder).
 * Returns HTTP 202 Accepted immediately while ingestion runs in the background.
 */
export async function processFolder(payload: IngestRequest): Promise<IngestAcceptedResponse> {
  const response = await apiClient.post<IngestAcceptedResponse>('/ingest/process-folder', {
    folder_path: payload.folder_path,
    content_type: payload.content_type,
    project_source: payload.project_source,
    sample_mode: payload.sample_mode ?? false,
    domain_id: payload.domain_id || undefined,
    ue_version: payload.ue_version || undefined,
  });

  // Track triggered job locally for Developer Hub UI status indicator
  const job: IngestionJobStatus = {
    id: `job-${Date.now()}`,
    folder_path: payload.folder_path,
    content_type: payload.content_type,
    project_source: payload.project_source,
    sample_mode: payload.sample_mode ?? false,
    domain_id: payload.domain_id || 'godot_4',
    status: 'processing',
    started_at: new Date().toISOString(),
    message: response.data.message,
  };
  saveIngestionJobToHistory(job);

  return response.data;
}

/**
 * Multipart / Form-Data File Upload helper:
 * Uploads single files or folder archives (FormData) to the ingestion pipeline.
 * Supports progress callback for progress bars in the UI.
 */
export async function uploadFileMultipart(
  payload: FileIngestionPayload,
  onProgress?: (percentCompleted: number) => void
): Promise<IngestAcceptedResponse> {
  const formData = new FormData();
  formData.append('file', payload.file);
  formData.append('content_type', payload.content_type);
  formData.append('project_source', payload.project_source);
  formData.append('sample_mode', String(payload.sample_mode ?? false));
  if (payload.domain_id) {
    formData.append('domain_id', payload.domain_id);
  }
  if (payload.ue_version) {
    formData.append('ue_version', payload.ue_version);
  }

  const response = await apiClient.post<IngestAcceptedResponse>('/ingest/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    onUploadProgress: (progressEvent) => {
      if (onProgress && progressEvent.total) {
        const percent = Math.round((progressEvent.loaded * 100) / progressEvent.total);
        onProgress(percent);
      }
    },
  });

  const job: IngestionJobStatus = {
    id: `file-${Date.now()}`,
    folder_path: payload.file.name,
    content_type: payload.content_type,
    project_source: payload.project_source,
    sample_mode: payload.sample_mode ?? false,
    domain_id: payload.domain_id || 'godot_4',
    status: 'processing',
    started_at: new Date().toISOString(),
    message: `File uploaded: ${payload.file.name}`,
  };
  saveIngestionJobToHistory(job);

  return response.data;
}

/**
 * Retrieves the history of ingestion jobs tracked by the client.
 */
export function getIngestionJobHistory(): IngestionJobStatus[] {
  try {
    const data = localStorage.getItem(INGESTION_JOBS_STORAGE_KEY);
    if (!data) return [];
    return JSON.parse(data) as IngestionJobStatus[];
  } catch {
    return [];
  }
}

/**
 * Persists a new or updated ingestion job to localStorage.
 */
export function saveIngestionJobToHistory(job: IngestionJobStatus): void {
  try {
    const existing = getIngestionJobHistory();
    const index = existing.findIndex((j) => j.id === job.id);
    if (index >= 0) {
      existing[index] = job;
    } else {
      existing.unshift(job);
    }
    // Limit history to 30 recent jobs
    const trimmed = existing.slice(0, 30);
    localStorage.setItem(INGESTION_JOBS_STORAGE_KEY, JSON.stringify(trimmed));
  } catch {
    // Ignore storage quota warnings
  }
}

/**
 * Clears recorded ingestion job history.
 */
export function clearIngestionJobHistory(): void {
  localStorage.removeItem(INGESTION_JOBS_STORAGE_KEY);
}
