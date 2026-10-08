import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// No dev proxy here on purpose. The browser calls the API directly through
// API_BASE in src/api.ts (VITE_API_URL), so a proxy would be unused — and a
// prefix like '/node' also matches '/node_modules/...', which forwards React's
// own chunks to the API and leaves the page blank.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    // The source arrives through a bind mount, and Docker Desktop's filesystem
    // events do not reach the container's watcher: without polling, edits on
    // the host keep serving the previously transformed module until the
    // container is restarted.
    watch: {
      usePolling: true,
      interval: 300,
    },
  }
})