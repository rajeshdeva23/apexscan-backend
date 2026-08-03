/// <reference types="vite/client" />

/**
 * Type declarations for Vite-injected environment variables.
 * Extend this interface as new VITE_* variables are introduced.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string;
  readonly VITE_WS_BASE_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
