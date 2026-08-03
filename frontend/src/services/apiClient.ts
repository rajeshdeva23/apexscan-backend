/**
 * HTTP API client.
 *
 * Thin, typed wrapper around fetch that centralises the backend base URL and
 * error handling. Feature services and TanStack Query hooks build on this
 * rather than calling fetch directly. No endpoints are implemented in Phase 1.
 */

// Base URL for the backend API, injected at build time by Vite.
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1';

/**
 * Perform a JSON request against the backend API.
 *
 * @param path - Path relative to the API base URL (e.g. "/health").
 * @param init - Optional fetch init overrides.
 * @returns Parsed JSON response typed as T.
 * @throws Error when the response status is not OK.
 */
export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });

  if (!response.ok) {
    throw new Error(`API request failed: ${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}
