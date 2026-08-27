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
import { translate } from "./index";

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

  /**
   * The other half, and the one the catalogue test cannot see.
   *
   * A green catalogue proves every `t(…)` resolves. It proves nothing about a string that
   * never became a `t(…)` — and that is where every seam found by walking the screens
   * actually lived: `label="Pages"`, `aria-label="Cancel"`, `empty="No roles assigned"`.
   * They render in English on a Spanish screen, they are invisible to a reviewer reading a
   * diff of the catalogue, and half of them are read aloud rather than drawn, so nobody
   * sees them at all.
   *
   * Only the props that are unambiguously read by a person. `title` is included because on
   * a control it is the tooltip; `alt` because it is the image for anyone who cannot see it.
   */
  const VISIBLE = ["label", "placeholder", "aria-label", "title", "hint", "empty", "headline", "alt"];

  /** Values that are deliberately the same in every language. */
  const LITERAL = new Set(["Zenith", "true", "false", "none", "editor", "you@company.com"]);

  it("wraps every user-visible attribute in t()", () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(sources)) {
      if (path.includes(".test.") || path.includes("/i18n/") || path.includes("/components/ui/"))
        continue;
      // Blank out the inside of every `t(…)` so its own literals are not reported.
      const outside = source.split("\n").map((line) => line.replace(/\bt\([^\n]*/g, ""));
      outside.forEach((line, index) => {
        for (const match of line.matchAll(/\b([a-zA-Z-]+)="([^"]{2,})"/g)) {
          const [, prop, value] = match;
          if (!VISIBLE.includes(prop!) || LITERAL.has(value!)) continue;
          // A className or a token list, not a sentence.
          if (!/[a-z]{3}/.test(value!) || /^[a-z0-9:/[\]-]+(\s+[a-z0-9:/[\]-]+)+$/.test(value!))
            continue;
          offenders.push(`  ${path}:${index + 1}  ${prop}="${value}"`);
        }
      });
    }
    expect(offenders, `not translated:\n${offenders.join("\n")}`).toEqual([]);
  });

  it("counts correctly in English too, not only in Spanish", () => {
    // The direction nobody checks: English keys are their own translation, so a plural key
    // rendered its singular form for every count until it carried an English entry.
    expect(translate("en", "{count} answer", { count: 1 })).toBe("1 answer");
    expect(translate("en", "{count} answer", { count: 3 })).toBe("3 answers");
    expect(translate("es", "{count} answer", { count: 1 })).toBe("1 respuesta");
    expect(translate("es", "{count} answer", { count: 3 })).toBe("3 respuestas");
  });

  it("keeps both forms of every plural", () => {
    for (const [key, entry] of Object.entries(es)) {
      if (typeof entry === "string") continue;
      expect(entry.one, `${key} has no singular`).toBeTruthy();
      expect(entry.other, `${key} has no plural`).toBeTruthy();
    }
  });
});
