/**
 * Every string the interface asks for has a Spanish sentence.
 *
 * This reads the source rather than comparing two catalogues, and that is deliberate: with
 * English keys there is no English catalogue to compare against, so the only honest question
 * is "does every `t(...)` in the tree resolve?". A key wrapped and never translated is the
 * failure this exists to catch — it renders in English, quietly, in the middle of a Spanish
 * screen, and nobody notices until it is on a projector.
 *
 * The sources come from `import.meta.glob`, which Vite resolves at build time. Reading them
 * with `node:fs` needed `@types/node`, and one dependency avoided is one licence the gate in
 * `make check` does not have to clear.
 */
import { describe, expect, it } from "vitest";

import { es } from "./es";

const sources = import.meta.glob("/src/**/*.{ts,tsx}", {
  query: "?raw",
  eager: true,
  import: "default",
}) as Record<string, string>;

/**
 * Every string literal inside a `t(...)` call.
 *
 * Scanned rather than matched with one regular expression, for a reason a regex cannot fix:
 * `t("(optional)")` has a closing parenthesis *inside* the key, so any pattern that ends the
 * call at the first `)` truncates the literal and reports a translated string as unused. The
 * scanner tracks whether it is inside quotes, which is the only way to know which
 * parenthesis is the real one.
 *
 * Also takes every literal in the call, not just the first: `t(n === 1 ? "a" : "b")` is an
 * ordinary way to write a plural whose count is already on screen.
 */
function keysIn(source: string): string[] {
  const keys: string[] = [];
  for (const call of source.matchAll(/\bt\(/g)) {
    let depth = 1;
    let quoted = false;
    let literal: string | null = null;
    for (let i = call.index + call[0].length; i < source.length && depth > 0; i += 1) {
      const ch = source[i]!;
      if (quoted) {
        if (ch === "\\") i += 1;
        else if (ch === '"') {
          keys.push(literal!);
          literal = null;
          quoted = false;
        } else literal += ch;
        continue;
      }
      if (ch === '"') {
        quoted = true;
        literal = "";
      } else if (ch === "(") depth += 1;
      else if (ch === ")") depth -= 1;
      else if (ch === "\n" && depth === 1) break;
    }
  }
  return keys;
}

function keysUsedInSource(): Map<string, string[]> {
  const used = new Map<string, string[]>();
  for (const [path, source] of Object.entries(sources)) {
    if (path.includes(".test.") || path.includes("/i18n/")) continue;
    for (const key of keysIn(source)) {
      used.set(key, [...(used.get(key) ?? []), path]);
    }
  }
  return used;
}

describe("the Spanish catalogue", () => {
  it("has a sentence for every key the interface asks for", () => {
    const missing = [...keysUsedInSource()]
      .filter(([key]) => !(key in es))
      .map(([key, files]) => `  "${key}"  — ${files.join(", ")}`);
    expect(missing, `no Spanish for:\n${missing.join("\n")}`).toEqual([]);
  });

  it("carries no entry nothing asks for", () => {
    // Dead translations are not harmless: they are the ones that stay when the English
    // changes, so the next person reads them as current and translates around them.
    const used = new Set(keysUsedInSource().keys());
    const orphans = Object.keys(es).filter((key) => !used.has(key));
    expect(orphans, `translated but unused:\n  ${orphans.join("\n  ")}`).toEqual([]);
  });

  it("keeps both forms of every plural", () => {
    for (const [key, entry] of Object.entries(es)) {
      if (typeof entry === "string") continue;
      expect(entry.one, `${key} has no singular`).toBeTruthy();
      expect(entry.other, `${key} has no plural`).toBeTruthy();
    }
  });
});
