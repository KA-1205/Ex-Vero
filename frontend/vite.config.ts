/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev-only proxy so relative asset paths (e.g. thumbnail_url) resolve to the
// node being developed against. VITE_API_BASE_URL lets a remote fleet be used
// (its returned thumbnail_url is a root-relative path); falls back to a local node.
const apiTarget = process.env.VITE_API_BASE_URL || 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/devices': apiTarget,
      '/cloud':   apiTarget,
      '/network': apiTarget,
      '/benchmark': apiTarget,
      '/demo':    apiTarget,
      '/consensus': apiTarget,
      '/thumbnails': apiTarget,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
