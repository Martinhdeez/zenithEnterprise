/**
 * Files waiting in the browser, and the tags somebody is deciding for them.
 *
 * The old flow uploaded the moment a file was dropped, which is right for one file and
 * wrong for a migration: a thousand documents all land under the tenant's default label
 * before anybody has had the chance to say what any of them are, and unpicking that
 * afterwards means editing a thousand documents.
 *
 * So dropping stages. Nothing leaves the browser until "Confirm & Process", and in between
 * the interesting operation is **subset tagging** — select forty invoices, tag them, select
 * the contracts, tag those. That is why labels live per row rather than once for the batch.
 *
 * Only labels. There is deliberately nothing here for users, roles or groups: who can read
 * a document is decided by which labels it carries and by the group and clearance matrix
 * those labels sit in. An uploader naming a *person* would be a second access model beside
 * the one RLS enforces, and the two would disagree.
 */

import type { SuggestionOutcome } from "../api";

/**
 * How the suggestion for one row ended — the server's four, plus one only a client has.
 *
 * `unreachable` is the call itself not completing: no reply, a 500, an expired token. The
 * server cannot report it, because a server that could report it would have answered. It is
 * named after the same ending in `uploadWatch.ts`, which exists for the same reason.
 *
 * It is deliberately not folded into `failed`. `failed` is the *model* breaking, which is a
 * statement about the installation; `unreachable` is this browser's request breaking, which
 * is a statement about this browser. Somebody reading the row can act on the second one.
 */
export type StagedOutcome = SuggestionOutcome | "unreachable";

export interface StagedFile {
  /** Stable for the row's life. `File` has no id, and two files may share a name. */
  id: string;
  file: File;
  /** What this file will be filed under. Empty means "let the server decide". */
  labelIds: string[];
  /**
   * How the suggestion for this row ended, once one has been asked for. Undefined means
   * nobody has asked yet, which is what drives the button's progress count.
   *
   * A `boolean` until it caused a bug: it recorded only that the loop had been past the row,
   * and the row then said `no match — server will file it` for all three of the endings that
   * come back with no ids. Under `failed` that is the opposite of what happens to the
   * document.
   */
  suggestion?: StagedOutcome;
  /**
   * Under `failed`, what the provider said about why. Undefined for every other ending.
   *
   * Beside `suggestion` rather than folded into it, because they answer different questions
   * and only one of them is a fixed set of values. The outcome says what happens to the
   * document — that is the part the row must not lie about. This says what a person can do
   * about it, and it is a sentence only the provider can write: `the language model returned
   * 429` and `your prepayment credits are depleted` are the same outcome and two different
   * afternoons.
   */
  reason?: string;
  /**
   * Label ids the model proposed and **nobody has accepted yet**. Deliberately not
   * `labelIds`.
   *
   * In this product a label is a permission — the policy is
   * `label_ids && zenith_current_labels()` — so accepting one of these decides who can read
   * the document. Writing the model's answer straight into `labelIds`, which is what used to
   * happen, made a machine's guess indistinguishable from a person's decision the moment it
   * landed: same array, same chips, no way to tell them apart on a screen of a hundred rows
   * an hour later.
   *
   * Two arrays, so the difference survives being looked at cold. Nothing here is filed until
   * `acceptProposed` moves it across.
   */
  proposed?: string[];
}

export function stage(files: File[], existing: StagedFile[] = []): StagedFile[] {
  const seen = new Set(existing.map((row) => row.id));
  const added: StagedFile[] = [];
  for (const [index, file] of files.entries()) {
    // Size and name together, plus a counter for the genuine duplicate: dropping the same
    // folder twice is ordinary, and silently collapsing the second drop into the first
    // would look like files went missing.
    let id = `${file.name}:${file.size}`;
    let attempt = index;
    while (seen.has(id)) id = `${file.name}:${file.size}:${attempt++}`;
    seen.add(id);
    added.push({ id, file, labelIds: [] });
  }
  return [...existing, ...added];
}

/** Apply labels to a subset, replacing whatever those rows carried. */
export function tagSelected(
  rows: StagedFile[],
  selected: Set<string>,
  labelIds: string[],
): StagedFile[] {
  return rows.map((row) => (selected.has(row.id) ? { ...row, labelIds: [...labelIds] } : row));
}

/** Add labels to a subset without discarding what they already had. */
export function addToSelected(
  rows: StagedFile[],
  selected: Set<string>,
  labelIds: string[],
): StagedFile[] {
  return rows.map((row) =>
    selected.has(row.id) ? { ...row, labelIds: [...new Set([...row.labelIds, ...labelIds])] } : row,
  );
}

export function removeSelected(rows: StagedFile[], selected: Set<string>): StagedFile[] {
  return rows.filter((row) => !selected.has(row.id));
}

/**
 * The rows a shift-click covers.
 *
 * Anchored on the last row clicked without shift, which is what every file manager does:
 * click 3, shift-click 9, and 3 through 9 are selected whichever end you started from.
 * Without an anchor a range selection has to be built one click at a time, which for the
 * forty invoices this screen exists to handle is forty clicks.
 */
export function range(rows: StagedFile[], anchorId: string, targetId: string): string[] {
  const from = rows.findIndex((row) => row.id === anchorId);
  const to = rows.findIndex((row) => row.id === targetId);
  if (from === -1 || to === -1) return [targetId];
  const [start, end] = from <= to ? [from, to] : [to, from];
  return rows.slice(start, end + 1).map((row) => row.id);
}

export interface StagingSummary {
  total: number;
  tagged: number;
  untagged: number;
  bytes: number;
}

export function summarise(rows: StagedFile[]): StagingSummary {
  const tagged = rows.filter((row) => row.labelIds.length > 0).length;
  return {
    total: rows.length,
    tagged,
    untagged: rows.length - tagged,
    bytes: rows.reduce((sum, row) => sum + row.file.size, 0),
  };
}

/**
 * A person agreeing with the model: what they had chosen, plus what was proposed.
 *
 * **Union rather than replacement.** Somebody may have ticked a label by hand before the
 * classifier ran, and the suggestion is an addition to their judgement rather than a
 * correction of it. Silently dropping what they chose would be the interface overruling them
 * on the one screen where it must not — and since a label is a permission here, the label it
 * dropped is a permission it revoked without being asked.
 *
 * One function rather than one per screen. The staging table accepts per row and the
 * single-file review panel accepts into the label picker's own selection; they hold their
 * decisions in different shapes, but "added to, never in place of" is the same rule and a
 * second copy of it is how the two come to disagree.
 */
export function accepted(chosen: Iterable<string>, proposed: Iterable<string>): string[] {
  return [...new Set([...chosen, ...proposed])];
}

/** A person agreeing with the model. The only path from `proposed` to `labelIds`. */
export function acceptProposed(rows: StagedFile[], ids?: Set<string>): StagedFile[] {
  return rows.map((row) => {
    if (ids && !ids.has(row.id)) return row;
    if (!row.proposed?.length) return row;
    return { ...row, labelIds: accepted(row.labelIds, row.proposed), proposed: [] };
  });
}

/** A person disagreeing. The outcome stays, so the row still says what the model answered. */
export function dismissProposed(rows: StagedFile[], ids?: Set<string>): StagedFile[] {
  return rows.map((row) => {
    if (ids && !ids.has(row.id)) return row;
    if (!row.proposed?.length) return row;
    return { ...row, proposed: [] };
  });
}

/** How many rows are holding a proposal nobody has answered yet. */
export function awaitingReview(rows: StagedFile[]): number {
  return rows.filter((row) => row.proposed?.length).length;
}
