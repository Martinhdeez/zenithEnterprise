import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Bundled, never a CDN. This product is installed inside networks with no egress, and a
// typeface fetched from a font host would silently fall back to a system face on exactly
// the installations that matter most.
import "@fontsource-variable/instrument-sans";
// Upright only: the package's entry point is the `wght` axis on its own, and the italic
// lives in a separate file this product never asks for. Every subset is declared, but a
// browser fetches only the ones whose `unicode-range` it actually needs — the Cyrillic and
// Vietnamese cuts are two lines of CSS here and no bytes over the wire.
import "@fontsource-variable/cormorant-garamond";
import "@fontsource-variable/montserrat";
import "@fontsource-variable/jetbrains-mono";
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
