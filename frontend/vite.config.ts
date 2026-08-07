import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  // The API is served next to the bundle in production, so development proxies rather than
  // hard-coding a host anywhere in the source. Nothing in `src/` knows where the API lives.
  server: {
    proxy: {
      "/auth": "http://localhost:8000",
      "/query": "http://localhost:8000",
      "/documents": "http://localhost:8000",
      "/tenant": "http://localhost:8000",
      "/search": "http://localhost:8000",
      "/labels": "http://localhost:8000",
      // M3 admin surfaces (F16), added once Admin.tsx actually exercised them in dev — until
      // then these silently fell through to Vite's own server and returned index.html,
      // which `request()` in api/client.ts saw as "Unexpected token '<'".
      "/roles": "http://localhost:8000",
      "/llm-config": "http://localhost:8000",
      "/users": "http://localhost:8000",
    },
  },
  test: { environment: "jsdom", globals: true },
});
