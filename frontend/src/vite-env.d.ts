/// <reference types="vite/client" />

// Vite's `?url` imports return a string. Declared here because `pdfjs-dist` ships the
// worker as a file to be loaded by URL rather than imported, and it must come from the
// bundle: this product is installed inside networks with no egress, where a viewer that
// silently failed to render because a CDN was unreachable would look like a broken
// document rather than a missing script.
declare module "*?url" {
  const url: string;
  export default url;
}
