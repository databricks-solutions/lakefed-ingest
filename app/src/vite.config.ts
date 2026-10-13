import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The React app lives in ./frontend and builds into ./static, which main.py (FastAPI) serves.
// `npm run dev` serves it with hot reload and proxies /api to the backend (`python main.py`).
export default defineConfig({
  root: "frontend",
  plugins: [react()],
  build: { outDir: "../static", emptyOutDir: true, chunkSizeWarningLimit: 2000 },
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
});
