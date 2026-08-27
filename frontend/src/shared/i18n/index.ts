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

/**
 * Dates and numbers follow the chosen language, not the browser's.
 *
 * Every call site used `toLocaleDateString()` with no locale, which resolves to
 * `navigator.language` — the machine's setting, not the application's. So the switch moved
 * the words and left `8,273` and `27/08/2026` reading in whichever language the laptop
 * happened to be set to. On a Spanish screen on an English machine that is `8,273` where
 * `8.273` belongs, in a product whose own evaluation reports are quoted to four figures.
 *
 * `en-GB` rather than `en-US`: this is deployed on-premise in Europe over a corpus of
 * Spanish and EU law, and day-month ordering is what its readers expect on both sides of the
 * switch. It is one line to change if that ever stops being true.
 */
const LOCALE: Record<Language, string> = { en: "en-GB", es: "es-ES" };

/** Grouped thousands: `8.273` in Spanish, `8,273` in English. */
export function formatNumber(language: Language, value: number): string {
  return new Intl.NumberFormat(LOCALE[language]).format(value);
}

/** Day only, for a row that answers "when", not "exactly when". */
export function formatDate(language: Language, value: string | Date): string {
  return new Intl.DateTimeFormat(LOCALE[language], {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(new Date(value));
}

/** Day and time, for a log entry where the order of two events matters. */
export function formatDateTime(language: Language, value: string | Date): string {
  return new Intl.DateTimeFormat(LOCALE[language], {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

/**
 * The compact stamp the audit trail, the document panel and the invitation all used:
 * `27 ago, 14:05`. Kept as one function rather than three copies of the same options object,
 * which is what they were.
 */
export function formatStamp(
  language: Language,
  value: string | Date,
  { year = false }: { year?: boolean } = {},
): string {
  return new Intl.DateTimeFormat(LOCALE[language], {
    day: "2-digit",
    month: "short",
    ...(year ? { year: "numeric" as const } : {}),
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

/**
 * The keys whose English form changes with the count.
 *
 * English keys are their own translation, which works for every string except a plural: a
 * key cannot be both "1 answer" and "3 answers". Before this, `{count} answer` rendered
 * "3 answer" on an English screen — the Spanish was right and the source language was not,
 * which is the one direction nobody thinks to check.
 *
 * Deliberately small, and it should stay that way. A key that needs an entry here is a key
 * whose English is doing work, and most sentences can be written so the count sits apart
 * from the noun instead.
 */
const EN_PLURALS: Record<string, { one: string; other: string }> = {
  "{count} answer": { one: "{count} answer", other: "{count} answers" },
  "{count} question reported no usage — not counted": {
    one: "{count} question reported no usage — not counted",
    other: "{count} questions reported no usage — not counted",
  },
  "Delete {group}? Its {count} member(s) lose whatever it opened.": {
    one: "Delete {group}? Its one member loses whatever it opened.",
    other: "Delete {group}? Its {count} members lose whatever it opened.",
  },
};

export function translate(
  language: Language,
  key: string,
  vars?: Record<string, string | number>,
): string {
  // English is the source. Its catalogue is the keys themselves, so a missing English entry
  // is impossible by construction and only Spanish can be incomplete — which is what the
  // parity test asserts. The one exception is a plural: a key cannot be its own singular
  // *and* its own plural, so those few carry an English entry like any other language.
  if (language === "en") {
    const plural = EN_PLURALS[key];
    return plural ? fill(choose(plural, language, vars), vars) : fill(key, vars);
  }
  const entry = es[key];
  if (entry === undefined) {
    // The English text, never an empty string or the raw key in brackets. An untranslated
    // sentence is a blemish; a blank space where a button's label belongs is a broken screen.
    return fill(key, vars);
  }
  return fill(choose(entry, language, vars), vars);
}
