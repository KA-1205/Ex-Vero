import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/devices': 'http://localhost:8000',
      '/cloud':   'http://localhost:8000',
      '/network': 'http://localhost:8000',
      '/benchmark': 'http://localhost:8000',
      '/demo':    'http://localhost:8000',
      '/consensus': 'http://localhost:8000',
      '/thumbnails': 'http://localhost:8000',
    },
  },
})
