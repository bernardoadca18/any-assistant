/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_QDRANT_DASHBOARD_URL?: string;
  readonly VITE_LITELLM_PROXY_URL?: string;
  readonly VITE_DEV_AUTH_FALLBACK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
