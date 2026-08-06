import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
        // Unhandled ws-proxy socket errors (EPIPE on client disconnect)
        // crash the dev server without this.
        configure: (proxy) => {
          proxy.on("error", () => {});
        },
      }
    }
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: ["node_modules/**", "dist/**", "tests/e2e/**"]
  }
});
