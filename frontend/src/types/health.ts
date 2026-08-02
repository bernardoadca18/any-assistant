/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Health and System Status types matching OpenAPI /v1/health specification.
 */

export type DependencyStatusType = 'ok' | 'degraded' | 'error';

export interface DependencyStatus {
  status: DependencyStatusType;
  latency_ms?: number | null;
  detail?: string | null;
}

export interface HealthResponse {
  status: DependencyStatusType;
  version: string;
  environment: string;
  dependencies: Record<string, DependencyStatus>;
}

export interface SystemHealthSummary {
  status: DependencyStatusType;
  lastChecked: string;
  version?: string;
  environment?: string;
  dependencies?: Record<string, DependencyStatus>;
  errorMessage?: string;
}
