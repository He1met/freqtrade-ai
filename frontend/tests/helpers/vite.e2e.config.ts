import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const backendPort = Number(process.env.E2E_BACKEND_PORT);
if (!Number.isInteger(backendPort) || backendPort < 1024 || backendPort > 65535) {
  throw new Error("E2E_BACKEND_PORT must be a valid isolated port.");
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
});
