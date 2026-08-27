/**
 * The language as an external store rather than a React context.
 *
 * A context would need a provider wrapping the tree and would re-render everything under it
 * on every change, which is correct but heavy for a value that changes twice a demonstration.
 * `useSyncExternalStore` gives the same correctness — no tearing, no stale read in concurrent
 * rendering — without a provider, so a component deep in `features/` calls `useT()` and
 * nothing above it has to know.
 */
import { useCallback, useSyncExternalStore } from "react";

import { rememberLanguage, storedLanguage, translate, type Language } from "./index";

let current: Language = storedLanguage();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): Language {
  return current;
}

export function setLanguage(language: Language): void {
  if (language === current) return;
  current = language;
  rememberLanguage(language);
  // `lang` on the document is not decoration: it is what a screen reader uses to pick a
  // voice, and what the browser uses to hyphenate. A Spanish interface announced in an
  // English voice is worse than an English one.
  document.documentElement.lang = language;
  for (const listener of listeners) listener();
}

export function useLanguage(): Language {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export type T = (key: string, vars?: Record<string, string | number>) => string;

export function useT(): T {
  const language = useLanguage();
  return useCallback(
    (key: string, vars?: Record<string, string | number>) => translate(language, key, vars),
    [language],
  );
}
