import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// A disposable local API may use a distinct port; deployed routing is unaffected.
const apiTarget = process.env.VITE_API_PROXY_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  // Disposable multi-origin acceptance gives each Vite process its own optimizer cache.
  cacheDir: process.env.ZENITH_VITE_CACHE_DIR || undefined,
  // The API is served next to the bundle in production, so development proxies rather than
  // hard-coding a host anywhere in the source. Nothing in `src/` knows where the API lives.
  server: {
    proxy: {
      "/auth": apiTarget,
      "/query": apiTarget,
      "/documents": apiTarget,
      "/tenant": apiTarget,
      "/search": apiTarget,
      "/labels": apiTarget,
      // M3 admin surfaces (F16), added once Admin.tsx actually exercised them in dev — until
      // then these silently fell through to Vite's own server and returned index.html,
      // which `request()` in api/client.ts saw as "Unexpected token '<'".
      "/roles": apiTarget,
      "/groups": apiTarget,
      "/system": apiTarget,
      "/analytics": apiTarget,
      "/llm-config": apiTarget,
      "/users": apiTarget,
    },
  },
  test: { environment: "jsdom", globals: true, maxWorkers: 2 },
});
