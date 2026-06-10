import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendTarget = env.BACKEND_URL || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            charts: ["recharts"]
          }
        }
      }
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/api": {
          target: backendTarget,
          changeOrigin: true
        }
      }
    },
    preview: {
      host: "0.0.0.0",
      port: 5173
    }
  };
});
