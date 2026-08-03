// ---------------------------------------------------------------------------
// ApexScan frontend — Vite build/dev configuration.
// Wires the React and Tailwind v4 plugins and the "@/" path alias.
// ---------------------------------------------------------------------------
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: true, // expose on the container network for Docker
    port: 5173,
  },
});
