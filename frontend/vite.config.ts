import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const apiProxy = process.env.KUBEML_API_PROXY ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiProxy,
      "/metrics": apiProxy,
    },
  },
  preview: {
    proxy: {
      "/api": apiProxy,
      "/metrics": apiProxy,
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
