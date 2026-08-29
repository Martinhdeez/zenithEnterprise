/**
 * Reading an `@` mention out of what someone is typing.
 *
 * Kept away from React so the rule can be stated once and tested directly. The rule is
 * narrower than "the text contains an @": the menu should open while you are typing a
 * mention and stay shut the rest of the time, and getting that wrong in either direction is
 * what makes this kind of input feel broken — a menu over an email address, or no menu
 * because the cursor moved back a word.
 */

/** How much of a filename someone will type before giving up on the menu. */
const MAX_QUERY = 60;

export interface Mention {
  /** Index of the `@`. */
  start: number;
  /** Index just past the cursor — what a completion replaces, together with `start`. */
  end: number;
  /** What has been typed after the `@`, used to search documents. */
  query: string;
}

/**
 * The mention being typed at `cursor`, if there is one.
 *
 * Scans back from the cursor to the nearest `@` and rejects the result unless:
 *
 * - the `@` opens a word — preceded by the start of the text or whitespace, so `user@host`
 *   is an address rather than a mention;
 * - nothing between it and the cursor is a newline, which ends a mention the way it ends a
 *   line;
 * - the typed part is short enough to still be a filename fragment.
 *
 * Spaces *are* allowed inside the query, and they have to be: real filenames have spaces in
 * them, and stopping at the first one makes every multi-word document unreachable by
 * exactly the mechanism that exists to reach it. The cost is a menu that stays open while
 * typing an ordinary sentence after an unmatched `@` — bounded by `MAX_QUERY`, and closed
 * by Escape.
 */
export function mentionAt(text: string, cursor: number): Mention | null {
  const before = text.slice(0, cursor);
  const start = before.lastIndexOf("@");
  if (start === -1) return null;

  const preceding = start === 0 ? "" : (before[start - 1] ?? "");
  if (preceding !== "" && !/\s/.test(preceding)) return null;

  const query = before.slice(start + 1);
  if (query.includes("\n") || query.length > MAX_QUERY) return null;

  return { start, end: cursor, query };
}

/**
 * The text with the mention replaced by the chosen filename.
 *
 * The filename goes in, not the id. The id travels separately, in `documents` on the
 * request; putting a UUID in the composer would show the user a string that means nothing
 * to them and that they cannot correct.
 *
 * A trailing space, so the next word does not extend the mention that was just completed.
 */
export function complete(text: string, mention: Mention, filename: string): string {
  return `${text.slice(0, mention.start)}@${filename} ${text.slice(mention.end)}`;
}

/**
 * Which mentioned documents survive in the text.
 *
 * Called after every edit, because a mention that has been deleted must not go on scoping
 * the question. Matching on the filename is what makes the composer the single source of
 * truth: the chips, the request, and the words on screen cannot disagree, because the text
 * decides all three.
 */
export function mentioned<T extends { id: string; filename: string }>(
  text: string,
  candidates: T[],
): T[] {
  return candidates.filter((candidate) => text.includes(`@${candidate.filename}`));
}
