import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // The API is served next to the bundle in production, so development proxies rather than
  // hard-coding a host anywhere in the source. Nothing in `src/` knows where the API lives.
  server: { proxy: { "/auth": "http://localhost:8000", "/query": "http://localhost:8000", "/documents": "http://localhost:8000", "/tenant": "http://localhost:8000", "/search": "http://localhost:8000" } },
  test: { environment: "jsdom", globals: true },
});
