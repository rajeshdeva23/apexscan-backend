/**
 * ApexScan frontend bootstrap.
 *
 * Mounts the React application into the DOM and installs global providers:
 * TanStack Query (server state) and the client-side router. Keep this file
 * thin — it is the composition root, not a place for UI logic.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router/dom';

import { router } from '@/routes/router';
import '@/styles/index.css';

// Single QueryClient for the app lifetime.
const queryClient = new QueryClient();

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found');
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
