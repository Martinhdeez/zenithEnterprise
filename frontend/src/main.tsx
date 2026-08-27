import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Bundled, never a CDN. This product is installed inside networks with no egress, and a
// typeface fetched from a font host would silently fall back to a system face on exactly
// the installations that matter most.
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@fontsource-variable/source-serif-4";

import { App } from "./App";
import "./styles/index.css";

const root = document.getElementById("root");
if (!root) throw new Error("index.html must contain #root");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
