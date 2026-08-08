/**
 * Administering the label set itself: find one, see what carries it, fold duplicates
 * together, delete what nothing needs.
 *
 * This is the screen `Admin.tsx` used to say did not belong. The objection it recorded was
 * real and is worth keeping in view: labels are already created and deleted from the
 * upload picker, and one resource administered from two places is how two screens start
 * disagreeing about what a label is. What settles it is that these are different jobs.
 * `LabelPicker` files *this document* and creates a label in passing; nothing there can
 * answer "we have four spellings of Finance and one of them is on six hundred documents",
 * which is the question a tenant has after a year of uploads. Both screens read the same
 * endpoints, so neither holds an opinion the other can contradict.
 *
 * The part that is not a CRUD table is the merge. Labels are the access-control primitive
 * — `role_labels` decides who reaches what, `documents.label_ids` is what RLS reads — so
 * folding two together moves documents between roles, in both directions at once. The
 * server computes that as `visibility_widening` and refuses a merge that would widen
 * anything unless it is acknowledged; this screen's job is to make sure the number is
 * *read* before it is acknowledged, which is why the confirmation is two steps and the
 * second one is not reachable without the first.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Check, Loader2, Merge, Trash2 } from "lucide-react";

import { deleteLabel, mergeLabels, searchLabels, type LabelMergeResult, type LabelSearchItem, type LabelSort } from "./api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SearchField } from "@/shared/ui/SearchField";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const PAGE = 20;

const SORTS: { value: LabelSort; label: string }[] = [
  { value: "name", label: "Name" },
  { value: "usage_count", label: "Most used" },
  { value: "created_at", label: "Newest" },
];

export function TagManager({ token }: { token: string }) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<LabelSort>("name");
  const [items, setItems] = useState<LabelSearchItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [merging, setMerging] = useState(false);

  // Every fetch carries a ticket, and a stale one is dropped rather than rendered. Without
  // it a slow request for "fin" lands after a fast one for "finance" and the list shows
  // results for a query the box no longer contains.
  const latest = useRef(0);

  const load = useCallback(
    async (options: { append?: string } = {}) => {
      const ticket = ++latest.current;
      setLoading(true);
      setError(null);
      try {
        const page = await searchLabels(token, {
          q: query || undefined,
          sort,
          cursor: options.append,
          limit: PAGE,
        });
        if (ticket !== latest.current) return;
        setItems((current) => (options.append ? [...current, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } catch (failure) {
        if (ticket !== latest.current) return;
        setError(message(failure, "Those labels couldn't be loaded."));
      } finally {
        if (ticket === latest.current) setLoading(false);
      }
    },
    [token, query, sort],
  );

  // Debounced on the query, immediate on the sort — one is typed a character at a time and
  // the other is a single deliberate click. The cursor is deliberately *not* a dependency:
  // it belongs to the ordering that issued it, and the server rejects one carried across a
  // sort change rather than silently returning a page from the wrong position.
  useEffect(() => {
    const timer = setTimeout(() => void load(), query ? 200 : 0);
    return () => clearTimeout(timer);
  }, [load, query]);

  const selected = useMemo(
    () => items.filter((item) => chosen.has(item.id)),
    [items, chosen],
  );

  const toggle = useCallback((id: string) => {
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const remove = useCallback(
    async (item: LabelSearchItem) => {
      setError(null);
      try {
        await deleteLabel(token, item.id);
        setItems((current) => current.filter((label) => label.id !== item.id));
      } catch (failure) {
        // The server refuses while documents still carry it, and says how many. That
        // refusal is the guard against a delete that would leave those documents
        // unlabelled — which means visible to the whole tenant — so its wording is the
        // only useful thing to show here.
        setError(message(failure, "That label couldn't be deleted."));
      }
    },
    [token],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex min-w-56 flex-1">
          <SearchField
            value={query}
            onChange={setQuery}
            label="Search labels"
            placeholder="Search labels"
          />
        </div>
        <Select value={sort} onValueChange={(value) => setSort(value as LabelSort)}>
          <SelectTrigger className="h-9 w-36 rounded-md border-input bg-card" aria-label="Sort by">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SORTS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="outline"
          disabled={selected.length < 2}
          onClick={() => setMerging(true)}
          className="h-9 gap-1.5 rounded-md"
        >
          <Merge className="size-4" />
          Merge {selected.length > 1 && `(${selected.length})`}
        </Button>
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <ul className="space-y-1.5">
        {items.map((item) => (
          <li
            key={item.id}
            className={`flex items-center gap-3 rounded-md border px-3 py-2 transition-colors ${
              chosen.has(item.id)
                ? "border-primary/40 bg-primary/10"
                : "border-input bg-card"
            }`}
          >
            {/* The same disc as the access list and the staging table. A native checkbox is
                drawn by the operating system, so it is the one control on the page that
                cannot be made to look like the rest of it. */}
            <button
              type="button"
              role="checkbox"
              aria-checked={chosen.has(item.id)}
              onClick={() => toggle(item.id)}
              aria-label={`Select ${item.name}`}
              className={`flex size-4 shrink-0 items-center justify-center rounded-full border transition-colors ${
                chosen.has(item.id)
                  ? "border-primary bg-primary text-white"
                  : "border-muted-foreground/40 hover:border-primary/60"
              }`}
            >
              {chosen.has(item.id) && <Check className="size-2.5" strokeWidth={3.5} />}
            </button>
            <span className="min-w-0 flex-1 truncate text-sm text-foreground">
              {item.name}
              {item.is_default && (
                <span className="ml-2 text-xs text-muted-foreground">default</span>
              )}
            </span>
            {/* "documents you can see", not a tenant total — the count is RLS-scoped.
                Stated plainly rather than dressed up as a statistic. */}
            <span className="shrink-0 text-xs text-muted-foreground">
              {item.documents} {item.documents === 1 ? "document" : "documents"}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Delete ${item.name}`}
              onClick={() => void remove(item)}
              className="shrink-0 text-muted-foreground hover:text-destructive"
            >
              <Trash2 className="size-4" />
            </Button>
          </li>
        ))}
      </ul>

      {items.length === 0 && !loading && (
        <p className="py-6 text-center text-sm text-muted-foreground">
          {query ? "No labels match." : "This tenant has no labels yet."}
        </p>
      )}

      {loading && (
        <p className="flex items-center justify-center gap-2 py-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading…
        </p>
      )}

      {cursor && !loading && (
        <Button
          type="button"
          variant="outline"
          onClick={() => void load({ append: cursor })}
          className="w-full rounded-md"
        >
          Load more
        </Button>
      )}

      {merging && (
        <MergeDialog
          token={token}
          selected={selected}
          onClose={() => setMerging(false)}
          onMerged={(gone) => {
            setItems((current) => current.filter((item) => !gone.includes(item.id)));
            setChosen(new Set());
            setMerging(false);
            void load();
          }}
        />
      )}
    </div>
  );
}

/**
 * The two-step confirmation, and the reason it is two steps.
 *
 * Step one is a `dry_run`: the server computes what the merge would move and how much of
 * it becomes visible to somebody new, and changes nothing. Step two sends the same merge
 * with `acknowledge_widening`. The confirm button does not exist until the preview has
 * come back, so there is no path through this dialog that commits a merge whose
 * consequences were never on screen.
 *
 * That is deliberately more friction than a "are you sure?" — the mistake this prevents is
 * not clicking the wrong button, it is understanding the operation as tidying up a tag
 * when it is really a change to who can read what.
 */
function MergeDialog({
  token,
  selected,
  onClose,
  onMerged,
}: {
  token: string;
  selected: LabelSearchItem[];
  onClose: () => void;
  onMerged: (merged: string[]) => void;
}) {
  const [target, setTarget] = useState(selected[0]?.id ?? "");
  const [preview, setPreview] = useState<LabelMergeResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sources = selected.filter((item) => item.id !== target).map((item) => item.id);

  // A different target is a different merge — different documents move, and a different
  // set of roles gains them. Dropping the stale preview is what stops the confirm button
  // from committing one merge while the screen describes another.
  useEffect(() => setPreview(null), [target]);

  const run = useCallback(
    async (acknowledge: boolean) => {
      setBusy(true);
      setError(null);
      try {
        const result = await mergeLabels(token, {
          sources,
          target,
          dry_run: !acknowledge,
          acknowledge_widening: acknowledge,
        });
        if (acknowledge) onMerged(result.merged);
        else setPreview(result);
      } catch (failure) {
        setError(message(failure, "That merge couldn't be completed."));
      } finally {
        setBusy(false);
      }
    },
    [token, sources, target, onMerged],
  );

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Merge labels</DialogTitle>
          <DialogDescription>
            Every document and role carrying the other labels will carry the one you keep
            instead. The others are deleted.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1.5">
            <span className="text-xs font-medium text-muted-foreground">Keep</span>
            <Select value={target} onValueChange={setTarget}>
              <SelectTrigger className="h-9 rounded-md border-input bg-card" aria-label="Keep">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {selected.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-sm text-foreground">
            {selected
              .filter((item) => item.id !== target)
              .map((item) => (
                <span
                  key={item.id}
                  className="rounded-full border border-input bg-card px-2.5 py-1 text-xs text-muted-foreground line-through"
                >
                  {item.name}
                </span>
              ))}
            <ArrowRight className="size-4 text-muted-foreground" />
            <span className="rounded-full border border-primary bg-primary/15 px-2.5 py-1 text-xs font-medium text-primary">
              {selected.find((item) => item.id === target)?.name}
            </span>
          </div>

          {preview && (
            <div className="space-y-1.5 rounded-md border border-input bg-card p-3 text-sm">
              <p className="text-foreground">
                {preview.documents_relabelled}{" "}
                {preview.documents_relabelled === 1 ? "document" : "documents"} will be
                relabelled.
              </p>
              {preview.visibility_widening > 0 ? (
                // The warning this whole dialog exists for. Not a generic "this cannot be
                // undone" — a specific statement of who gains access to how much.
                <p className="font-medium text-zenith-amber">
                  {preview.visibility_widening}{" "}
                  {preview.visibility_widening === 1 ? "document becomes" : "documents become"}{" "}
                  visible to roles that cannot see {preview.visibility_widening === 1 ? "it" : "them"}{" "}
                  today.
                </p>
              ) : (
                <p className="text-muted-foreground">
                  No document changes hands: every role that can see these already reaches
                  the label you are keeping.
                </p>
              )}
            </div>
          )}

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} className="rounded-md">
            Cancel
          </Button>
          {preview ? (
            <Button
              type="button"
              disabled={busy}
              onClick={() => void run(true)}
              className={
                preview.visibility_widening > 0
                  ? "rounded-md bg-destructive text-white hover:bg-destructive/90"
                  : "rounded-md"
              }
            >
              {busy
                ? "Merging…"
                : preview.visibility_widening > 0
                  ? "Merge and widen access"
                  : "Merge"}
            </Button>
          ) : (
            <Button
              type="button"
              disabled={busy || sources.length === 0}
              onClick={() => void run(false)}
              className="rounded-md"
            >
              {busy ? "Checking…" : "Preview changes"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function message(failure: unknown, fallback: string): string {
  return failure instanceof ApiError ? failure.message : fallback;
}
