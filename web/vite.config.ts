import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` with the API on :8600 (uvicorn dhadkan.live:create_app --factory --port 8600).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8600",
      "/ws": { target: "ws://127.0.0.1:8600", ws: true },
    },
  },
  build: { chunkSizeWarningLimit: 1500 },
});
