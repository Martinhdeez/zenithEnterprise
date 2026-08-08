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

export interface StagedFile {
  /** Stable for the row's life. `File` has no id, and two files may share a name. */
  id: string;
  file: File;
  /** What this file will be filed under. Empty means "let the server decide". */
  labelIds: string[];
  /** True once a suggestion has been asked for, so the button can report progress. */
  suggested?: boolean;
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
