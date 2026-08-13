/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Overrides the API origin. Defaults to `/api`, which Vite proxies to :8000. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
