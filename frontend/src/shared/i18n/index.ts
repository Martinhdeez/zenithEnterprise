/**
 * Two languages, one build.
 *
 * A build per language was the other option and it is worse for the reason that matters
 * here: the demonstration is given in Spanish to a Spanish buyer, and being able to switch
 * in front of them says more about the product than showing them a Spanish screenshot does.
 * It is also one branch instead of two divergent ones across forty-four files.
 *
 * No library. For 383 strings and two languages `react-i18next` brings a dependency that has
 * to clear the licence gate in `make check`, a provider, a hook and a loader, to replace what
 * fits here in sixty lines. `Intl.PluralRules` is in the browser already and is the only part
 * that is genuinely hard to get right by hand.
 *
 * Keys are English sentences, not dotted tokens. `t("Search your corpus")` reads at the call
 * site as what it renders; `t("search.empty.title")` needs a second file open to review. The
 * cost is that changing the English text changes the key, which the parity test catches
 * immediately rather than silently dropping a translation.
 */
import { es } from "./es";

export type Language = "en" | "es";

const KEY = "zenith.language";

/** Every access guarded: reading `localStorage` throws in a private window, not returns null. */
export function storedLanguage(): Language {
  try {
    const value = localStorage.getItem(KEY);
    if (value === "en" || value === "es") return value;
  } catch {
    // Nothing is reachable, so nothing was chosen.
  }
  return typeof navigator !== "undefined" && navigator.language.startsWith("es") ? "es" : "en";
}

export function rememberLanguage(language: Language): void {
  try {
    localStorage.setItem(KEY, language);
  } catch {
    // It still applies for this session; it just will not survive a reload.
  }
}

/**
 * Interpolation is `{name}`, and a missing variable renders the brace form rather than
 * "undefined": a visible `{count}` on screen is a bug report, and the word "undefined" is a
 * bug that looks like content.
 */
function fill(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

/**
 * Plurals come from `Intl.PluralRules`, not from `n === 1`. Spanish and English happen to
 * agree on one-versus-many, so the hand-rolled version would have worked and would have been
 * wrong for the first language after these two.
 *
 * A plural entry is `{ one, other }`; a plain entry is a string.
 */
type Entry = string | { one: string; other: string };
export type Catalogue = Record<string, Entry>;

function choose(entry: Entry, language: Language, vars?: Record<string, string | number>): string {
  if (typeof entry === "string") return entry;
  const count = Number(vars?.count ?? 0);
  const rule = new Intl.PluralRules(language).select(count);
  return rule === "one" ? entry.one : entry.other;
}

export function translate(
  language: Language,
  key: string,
  vars?: Record<string, string | number>,
): string {
  // English is the source. Its catalogue is the keys themselves, so a missing English entry
  // is impossible by construction and only Spanish can be incomplete — which is what the
  // parity test asserts.
  if (language === "en") return fill(key, vars);
  const entry = es[key];
  if (entry === undefined) {
    // The English text, never an empty string or the raw key in brackets. An untranslated
    // sentence is a blemish; a blank space where a button's label belongs is a broken screen.
    return fill(key, vars);
  }
  return fill(choose(entry, language, vars), vars);
}
