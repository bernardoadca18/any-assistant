/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 * 
 * Tenant, Collection, and Admin configuration types matching backend Pydantic schemas.
 */

export interface EmbeddingConfig {
  model: string;
  dimension: number;
}

export interface RAGConfig {
  decomposition_enabled: boolean;
  decomposition_model: string;
  decomposition_timeout: number;
  top_k: number;
  retrieval_limit_per_query: number;
  score_threshold: number;
  hybrid_search_enabled: boolean;
  decomposition_max_keywords: number;
  reranker_enabled: boolean;
  reranker_model: string;
  reranker_cache_dir: string;
  rerank_min_score: number;
  system_prompt?: string | null;
}

export interface IngestionConfig {
  documenter_model: string;
  batch_size: number;
  embedding_concurrency: number;
  max_chunk_chars: number;
  max_header_chars: number;
  max_consecutive_failures: number;
  lock_timeout_seconds: number;
  default_ue_version: string;
  source_code_extensions?: string[];
  source_code_sample_extensions?: string[];
  documentation_extensions?: string[];
}

export interface LiteLLMConfig {
  base_url: string;
  master_key?: string | null;
  max_retries: number;
}

export interface CollectionConfig {
  id?: string;
  tenant_id?: string;
  name: string;
  qdrant_collection_name?: string;
  domain_id?: string | null;
  display_name: string;
  description: string;
  embedding?: EmbeddingConfig;
  rag?: RAGConfig;
  ingestion?: IngestionConfig;
  litellm?: LiteLLMConfig | null;
  default_project_source_filter?: string | null;
  is_active: boolean;
  has_custom_config?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface CollectionCreate {
  tenant_id: string;
  name: string;
  qdrant_collection_name: string;
  domain_id?: string;
  display_name: string;
  description?: string;
  default_project_source_filter?: string | null;
}

export interface CollectionUpdate {
  display_name?: string | null;
  description?: string | null;
  is_active?: boolean | null;
  default_project_source_filter?: string | null;
}

export interface CollectionResponse {
  id?: string;
  tenant_id?: string;
  name: string;
  qdrant_collection_name: string;
  domain_id: string;
  display_name: string;
  description: string;
  is_active: boolean;
  default_project_source_filter?: string | null;
  has_custom_config: boolean;
  embedding?: EmbeddingConfig;
  rag?: RAGConfig;
  ingestion?: IngestionConfig;
  litellm?: LiteLLMConfig | null;
  created_at?: string;
  updated_at?: string;
}

export interface CollectionListResponse {
  items: CollectionResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface TenantConfig {
  id: string;
  slug?: string;
  name: string;
  description: string;
  collections?: Record<string, CollectionConfig>;
  default_collection?: string | null;
  default_embedding?: EmbeddingConfig | null;
  default_rag?: RAGConfig | null;
  default_ingestion?: IngestionConfig | null;
  default_litellm?: LiteLLMConfig | null;
  rate_limit_rpm?: number | null;
  rate_limit_tpm?: number | null;
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface TenantCreate {
  slug: string;
  name: string;
  description?: string;
  rate_limit_rpm?: number | null;
  rate_limit_tpm?: number | null;
}

export interface TenantUpdate {
  name?: string | null;
  description?: string | null;
  is_active?: boolean | null;
  rate_limit_rpm?: number | null;
  rate_limit_tpm?: number | null;
}

export interface TenantResponse {
  id: string;
  slug: string;
  name: string;
  description: string;
  is_active: boolean;
  rate_limit_rpm?: number | null;
  rate_limit_tpm?: number | null;
  collections_count?: number;
  api_keys_count?: number;
  created_at?: string;
  updated_at?: string;
  collections?: string[];
}

export interface TenantListResponse {
  items: TenantResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface APIKeyCreate {
  name: string;
  is_admin?: boolean;
  is_read_only?: boolean;
  expires_at?: string | null;
}

export interface APIKeyResponse {
  id: string;
  name: string;
  key_prefix: string;
  is_admin: boolean;
  is_read_only: boolean;
  is_active: boolean;
  expires_at?: string | null;
  last_used_at?: string | null;
  created_at: string;
}

export interface APIKeyCreateResponse extends APIKeyResponse {
  key: string;
}
