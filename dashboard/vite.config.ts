import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In production nginx serves the build and proxies /api/ to cp-api:8000.
// In dev there is no nginx, so the same path is proxied here instead — which
// keeps the app's fetch URLs identical in both, with no environment switch in
// the client code.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
