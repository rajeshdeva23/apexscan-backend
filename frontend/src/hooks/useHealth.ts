/**
 * Example data hook.
 *
 * Demonstrates the TanStack Query + apiClient pattern by reading the backend
 * health endpoint. Serves as the template for future feature hooks. This is
 * the only network call wired in Phase 1.
 */
import { useQuery } from '@tanstack/react-query';

import { apiRequest } from '@/services/apiClient';

interface HealthResponse {
  status: string;
}

/** Query the backend liveness endpoint. */
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => apiRequest<HealthResponse>('/health'),
  });
}
