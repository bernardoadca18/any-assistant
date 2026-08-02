/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Centralized configuration of Developer Hub quick links, API base URLs,
 * Swagger documentation, Qdrant Vector DB Dashboard, and LiteLLM Proxy targets.
 */

export interface DevHubLink {
  id: string;
  title: string;
  description: string;
  url: string;
  category: 'api_docs' | 'vector_db' | 'llm_proxy' | 'engine_docs';
  iconName: 'FileText' | 'Database' | 'Cpu' | 'BookOpen' | 'ExternalLink' | 'Activity';
  isExternal?: boolean;
}

/**
 * Returns the resolved backend API Base URL.
 * In production or Docker compose, can be configured via VITE_API_BASE_URL.
 * Fallbacks to '/v1' (for Vite proxying or relative path) or 'http://localhost:8000/v1'.
 */
export function getApiBaseUrl(): string {
  const envUrl = import.meta.env.VITE_API_BASE_URL;
  if (envUrl && envUrl.trim() !== '') {
    return envUrl.replace(/\/+$/, '');
  }
  // Default development fallback
  return 'http://localhost:8000/v1';
}

/**
 * Returns the root backend URL (without /v1 prefix) for Swagger/Redoc docs.
 */
export function getBackendRootUrl(): string {
  const baseUrl = getApiBaseUrl();
  return baseUrl.replace(/\/v1\/?$/, '');
}

/**
 * Returns the Qdrant Dashboard UI URL.
 */
export function getQdrantDashboardUrl(): string {
  const envQdrant = import.meta.env.VITE_QDRANT_DASHBOARD_URL;
  return envQdrant || 'http://localhost:6333/dashboard';
}

/**
 * Returns the LiteLLM Proxy Dashboard URL.
 */
export function getLiteLLMProxyUrl(): string {
  const envLiteLLM = import.meta.env.VITE_LITELLM_PROXY_URL;
  return envLiteLLM || 'http://localhost:4000';
}

/**
 * Quick Links displayed in the Developer Hub center.
 */
export function getDevHubLinks(): DevHubLink[] {
  const root = getBackendRootUrl();

  return [
    {
      id: 'swagger-ui',
      title: 'Swagger UI (/docs)',
      description: 'Interactive OpenAPI Swagger documentation for testing API endpoints live.',
      url: `${root}/docs`,
      category: 'api_docs',
      iconName: 'FileText',
      isExternal: true,
    },
    {
      id: 'redoc-ui',
      title: 'ReDoc Documentation',
      description: 'Clean, responsive OpenAPI documentation and schema browser.',
      url: `${root}/redoc`,
      category: 'api_docs',
      iconName: 'BookOpen',
      isExternal: true,
    },
    {
      id: 'openapi-json',
      title: 'OpenAPI Schema (.json)',
      description: 'Raw OpenAPI 3.1.0 specification JSON file for SDK generators.',
      url: `${root}/openapi.json`,
      category: 'api_docs',
      iconName: 'FileText',
      isExternal: true,
    },
    {
      id: 'qdrant-dashboard',
      title: 'Qdrant Vector DB UI',
      description: 'Inspect vector embeddings, collections (ue5_docs), and search performance.',
      url: getQdrantDashboardUrl(),
      category: 'vector_db',
      iconName: 'Database',
      isExternal: true,
    },
    {
      id: 'litellm-proxy',
      title: 'LiteLLM Proxy & Models',
      description: 'Monitor LLM routing, tokens, rate limits, and Ollama/Gemini/Qwen backends.',
      url: getLiteLLMProxyUrl(),
      category: 'llm_proxy',
      iconName: 'Cpu',
      isExternal: true,
    },
    {
      id: 'ue5-official-docs',
      title: 'Unreal Engine 5 Reference',
      description: 'Epic Games official C++ & Blueprint documentation portal.',
      url: 'https://dev.epicgames.com/documentation/en-us/unreal-engine',
      category: 'engine_docs',
      iconName: 'ExternalLink',
      isExternal: true,
    },
    {
      id: 'godot-official-docs',
      title: 'Godot 4.x Reference',
      description: 'Official GDScript and C++ engine documentation for Godot 4.',
      url: 'https://docs.godotengine.org/en/stable/',
      category: 'engine_docs',
      iconName: 'ExternalLink',
      isExternal: true,
    },
  ];
}
