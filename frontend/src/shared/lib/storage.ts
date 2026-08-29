/**
 * Browser storage that cannot take the application down with it.
 *
 * `localStorage` and `sessionStorage` are not always there. Safari in private browsing,
 * enterprise policies that block site data, and a full quota all make them absent or make
 * every access throw — and the calls sit in places where a throw is not a lost preference.
 *
 * Three of them, found together:
 *
 * - `App.tsx` read `localStorage` unguarded in the render path for a *sidebar preference*,
 *   so a browser refusing site data meant the product did not load at all;
 * - it read `sessionStorage` the same way for the access token, in a `useState` initialiser,
 *   which is the same failure with the same blast radius;
 * - `Search.tsx` guarded its read and its write and left `removeItem` bare, so clearing the
 *   recent-search list threw out of a click handler.
 *
 * The pattern is always the same and so is the right answer: **the caller gets on with its
 * job.** A signed-in session that cannot be written survives until the tab closes rather than
 * never starting; a preference that cannot be read falls back to its default. Neither is
 * worth an error message, and none of it is worth the alternative.
 */

type Kind = "local" | "session";

/**
 * The store, or a throw.
 *
 * Deliberately *not* guarded here. The property access can throw as readily as the method
 * call — some hardened configurations make the getter itself raise — but every caller below
 * already wraps this call, so a second `try` would catch nothing that the first does not.
 * Removing it changed no test, which is how it was found: a guard that cannot be observed is
 * indistinguishable from no guard, and this file is about the difference.
 */
function backing(kind: Kind): Storage {
  return kind === "local" ? localStorage : sessionStorage;
}

export function read(kind: Kind, key: string): string | null {
  try {
    return backing(kind).getItem(key);
  } catch {
    return null;
  }
}

export function write(kind: Kind, key: string, value: string): void {
  try {
    backing(kind).setItem(key, value);
  } catch {
    // Quota exhausted, or a browser refusing storage outright.
  }
}

export function forget(kind: Kind, key: string): void {
  try {
    backing(kind).removeItem(key);
  } catch {
    // Nothing to do, and nothing worth telling anybody: the value is unreachable either way,
    // which is what the caller wanted.
  }
}
