import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Frozen contract (docs/API_CONTRACT.md): backend on :8000, dev server on :5173,
// and every call the frontend makes is relative to `/api` so the same code runs
// unchanged behind a reverse proxy in production.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        // 127.0.0.1 rather than localhost: Node resolves localhost to ::1
        // first, while uvicorn binds IPv4 only, so every proxied call would
        // fail with ECONNREFUSED.
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          // force-graph pulls in the whole d3 force/zoom stack; splitting it out
          // keeps the app shell chunk small enough to paint before the canvas boots.
          graph: ['react-force-graph-2d', 'd3-force'],
        },
      },
    },
  },
});
