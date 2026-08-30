/**
 * The table a migration is done from.
 *
 * Dropping a folder here files nothing. Rows sit in the browser while somebody selects the
 * invoices and tags them, selects the contracts and tags those, and only then confirms —
 * which is the difference between a corpus that arrives classified and a thousand documents
 * that all land under the default label and have to be edited one at a time afterwards.
 *
 * **Tags and nothing else.** There is deliberately no control here for users, roles or
 * groups. Who may read a document is decided by which labels it carries and by the group
 * and clearance matrix those labels sit in; letting an uploader name a *person* would be a
 * second access model beside the one RLS enforces, and two access models disagree.
 *
 * Rendering is capped rather than virtualised. A thousand `<li>`s of two spans is not what
 * makes a browser stutter, and a windowing library is a dependency bought against a cost
 * nobody has measured — but ten thousand rows is real, so beyond the cap the list says how
 * many more there are instead of drawing them. Selection and tagging still cover every row,
 * because they operate on the array and not on what is painted.
 */

import { useCallback, useMemo, useState } from "react";
import { Check, Loader2, Sparkles, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { LabelPicker, TagChips, type Label } from "@/features/labels";
import { excerpt } from "./excerpt";
import { applySuggestion, noteFor, suggestFor, type Suggested } from "./suggestion";
import { useT } from "@/shared/i18n/useT";
import {
  addToSelected,
  range,
  removeSelected,
  stage,
  summarise,
  type StagedFile,
} from "./stagingState";

/** Rows painted at once. Everything beyond is counted, not drawn. */
const RENDER_CAP = 300;

interface Props {
  token: string;
  rows: StagedFile[];
  known: Map<string, Label>;
  onChange: (rows: StagedFile[]) => void;
  onConfirm: () => void;
  busy: boolean;
}

export function Staging({ token, rows, known, onChange, onConfirm, busy }: Props) {
  const t = useT();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [picking, setPicking] = useState<Set<string>>(new Set());
  const [suggesting, setSuggesting] = useState<{ done: number; total: number } | null>(null);

  const summary = useMemo(() => summarise(rows), [rows]);
  const painted = rows.slice(0, RENDER_CAP);

  const click = useCallback(
    (id: string, shift: boolean) => {
      setSelected((current) => {
        const next = new Set(current);
        const covered = shift && anchor ? range(rows, anchor, id) : [id];
        // A shift-range asserts rather than toggles: dragging over a block that is half
        // selected should end with the whole block selected, not with it inverted.
        const turningOn = shift ? true : !next.has(id);
        for (const rowId of covered) {
          if (turningOn) next.add(rowId);
          else next.delete(rowId);
        }
        return next;
      });
      if (!shift) setAnchor(id);
    },
    [rows, anchor],
  );

  const allSelected = rows.length > 0 && selected.size === rows.length;

  /**
   * Ask the model where the selected files belong.
   *
   * Sequential on purpose. Each file is a `getDocument` in the browser and a model call on
   * the server; running a hundred at once would compete with itself for the worker and
   * take the API's connection pool from every other user of the installation. The counter
   * is what makes a slow, ordered pass tolerable to watch.
   *
   * **A file that fails does not stop the pass, and it does not pretend to have succeeded
   * either.** Both properties are needed at once, which is why `suggestFor` never rejects
   * and returns an ending instead — the old `.catch(() => [])` bought the first at the cost
   * of the second.
   */
  const autoTag = useCallback(async () => {
    const targets = rows.filter((row) => selected.has(row.id));
    if (!targets.length) return;

    setSuggesting({ done: 0, total: targets.length });
    let updated = rows;
    for (const [index, row] of targets.entries()) {
      const text = await excerpt(row.file);
      // No readable text is the server's `unavailable` reached one step earlier: nothing was
      // asked, so nothing broke, and the document is filed as any untagged one is.
      const suggested: Suggested = text
        ? await suggestFor(token, text)
        : { outcome: "unavailable", labelIds: [] };
      updated = applySuggestion(updated, row.id, suggested);
      onChange(updated);
      setSuggesting({ done: index + 1, total: targets.length });
    }
    setSuggesting(null);
  }, [rows, selected, token, onChange]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="text-muted-foreground">
          {summary.total} file{summary.total === 1 ? "" : "s"} staged ·{" "}
          <span className={summary.untagged ? "text-zenith-amber" : ""}>
            {summary.untagged} untagged
          </span>
        </span>
        <span className="text-xs text-muted-foreground">
          {(summary.bytes / 1_000_000).toFixed(1)} MB
        </span>
      </div>

      {/* The toolbar acts on the selection, so it says what the selection is. A bulk action
          whose scope is implicit is how somebody tags nine hundred files by accident. */}
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-input bg-secondary p-2">
        <button
          type="button"
          role="checkbox"
          aria-checked={allSelected}
          aria-label={t("Select all staged files")}
          onClick={() => setSelected(allSelected ? new Set() : new Set(rows.map((r) => r.id)))}
          className="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm text-foreground transition-colors hover:bg-card"
        >
          <span
            className={`flex size-4 items-center justify-center rounded-full border transition-colors ${
              allSelected ? "border-primary bg-primary text-white" : "border-muted-foreground/40"
            }`}
          >
            {allSelected && <Check className="size-2.5" strokeWidth={3.5} />}
          </span>
          {selected.size > 0 ? `${selected.size} selected` : t("Select all")}
        </button>

        <div className="flex-1" />

        <Button
          variant="outline"
          size="sm"
          disabled={!selected.size || !!suggesting}
          onClick={() => void autoTag()}
          title={t("Suggest tags from each file's opening pages, using the configured model")}
        >
          {suggesting ? (
            <>
              <Loader2 className="mr-1.5 size-3.5 animate-spin" />
              {suggesting.done}/{suggesting.total}
            </>
          ) : (
            <>
              <Sparkles className="mr-1.5 size-3.5" />{t("Auto-tag")}</>
          )}
        </Button>

        <Button
          variant="outline"
          size="sm"
          disabled={!selected.size}
          onClick={() => setPicking(new Set(selected))}
        >{t("Tag selected")}</Button>

        <Button
          variant="ghost"
          size="sm"
          disabled={!selected.size}
          className="text-destructive"
          onClick={() => {
            onChange(removeSelected(rows, selected));
            setSelected(new Set());
          }}
        >
          <Trash2 className="size-3.5" />
        </Button>
      </div>

      {picking.size > 0 && (
        <div className="space-y-3 rounded-md border border-primary/40 bg-card p-3">
          <p className="text-sm text-foreground">
            Tagging {picking.size} file{picking.size === 1 ? "" : "s"}
          </p>
          <LabelPicker
            token={token}
            selected={new Set<string>()}
            known={known}
            onToggle={(label) => {
              // Added rather than replacing: the toolbar is used in passes — finance, then
              // 2026 — and each pass should build on the last.
              onChange(addToSelected(rows, picking, [label.id]));
            }}
            onCreated={(label) => onChange(addToSelected(rows, picking, [label.id]))}
            onRemoved={() => {}}
          />
          <Button size="sm" variant="ghost" onClick={() => setPicking(new Set())}>{t("Done")}</Button>
        </div>
      )}

      <ul className="max-h-96 divide-y divide-border overflow-y-auto rounded-md border border-input">
        {painted.map((row) => (
          <li key={row.id} className="flex items-center gap-3 px-3 py-2 text-sm">
            {/* The same disc the access list uses, for the same reason: a native
                checkbox is drawn by the operating system and matches nothing else here.
                Kept as its own control rather than making the whole row a toggle — this
                list has shift-click ranges, and a row that toggles on click cannot also
                extend a selection without fighting the file name for the same gesture. */}
            <button
              type="button"
              role="checkbox"
              aria-checked={selected.has(row.id)}
              aria-label={`Select ${row.file.name}`}
              onClick={(event) => click(row.id, event.shiftKey)}
              className={`flex size-4 shrink-0 items-center justify-center rounded-full border transition-colors ${
                selected.has(row.id)
                  ? "border-primary bg-primary text-white"
                  : "border-muted-foreground/40 hover:border-primary/60"
              }`}
            >
              {selected.has(row.id) && <Check className="size-2.5" strokeWidth={3.5} />}
            </button>
            <span className="min-w-0 flex-1 truncate text-foreground">{row.file.name}</span>
            <span className="shrink-0">
              {row.labelIds.length > 0 ? (
                <TagChips
                  names={row.labelIds
                    .map((id) => known.get(id)?.name)
                    .filter((name): name is string => name !== undefined)}
                  short
                />
              ) : (
                /* One neutral treatment for all five endings: they have to be *told
                   apart*, and what they look like — colour, icon, weight — is not decided
                   here. */
                <span className="text-xs text-muted-foreground">
                  {noteFor(t, row.suggestion)}
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>

      {rows.length > RENDER_CAP && (
        <p className="text-xs text-muted-foreground">
          Showing the first {RENDER_CAP}. The other {rows.length - RENDER_CAP} are staged and
          are covered by Select all and by every bulk action.
        </p>
      )}

      <Button className="w-full" disabled={busy || !rows.length} onClick={onConfirm}>
        {busy ? t("Uploading…") : `Confirm & process ${rows.length} file${rows.length === 1 ? "" : "s"}`}
      </Button>

      {summary.untagged > 0 && (
        <p className="text-xs text-muted-foreground">
          {summary.untagged} untagged file{summary.untagged === 1 ? "" : "s"} will be filed by
          the server — it classifies anything nobody tagged, choosing only from labels you
          already hold.
        </p>
      )}
    </div>
  );
}

export { stage };
