import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev` talks to the backend on :8000 (e.g. from `docker compose up`).
    proxy: {
      '/api': { target: 'http://localhost:8000', ws: true },
    },
  },
})
