/**
 * The corpus itself, browsable — what clicking a folder in the sidebar was missing.
 *
 * `Folders` only ever narrowed *Chat* and *Search*; there was no screen that actually
 * listed a document you could look at or remove. `GET /documents` has existed since F4, and
 * nothing in the client called it until this component did.
 */

import { useCallback, useEffect, useState } from "react";
import { FileText, Trash2 } from "lucide-react";

import { deleteDocument, listDocuments, type DocumentSummary } from "./api";
import { ApiError } from "@/shared/api/http";
import type { Citation } from "@/features/chat";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface Props {
  token: string;
  onCitation: (citation: Citation) => void;
  /** From `Folders`. `null` (no object at all) means every reachable document; `{ labelId:
      null }` means specifically the ones carrying no label. */
  filter?: { labelId: string | null } | null;
  /** Bumped after an upload so a freshly-added document appears without a manual reload. */
  refreshKey?: number;
}

const STATUS_STYLE: Record<string, string> = {
  ready: "bg-zenith-cyan/10 text-zenith-cyan",
  failed: "bg-destructive/10 text-destructive",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function Documents({ token, onCitation, filter, refreshKey = 0 }: Props) {
  const [items, setItems] = useState<DocumentSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  // The document a delete click is waiting to be confirmed for — deletion here is
  // physical and cascading (RF-03: the file, its chunks, its citations, all gone), and a
  // trash icon a few pixels from the row it sits in is exactly the kind of control an
  // unwanted click hits by mistake. Set on click, cleared on either button in the dialog.
  const [confirming, setConfirming] = useState<DocumentSummary | null>(null);

  const load = useCallback(
    async (after: string | null, replace: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const page = await listDocuments(token, after, filter);
        setItems((current) => (replace ? page.items : [...current, ...page.items]));
        setCursor(page.next_cursor);
      } catch (fetchError) {
        setError(fetchError instanceof ApiError ? fetchError.message : "Couldn't load documents.");
      } finally {
        setLoading(false);
      }
    },
    [token, filter],
  );

  useEffect(() => {
    void load(null, true);
    // `refreshKey` deliberately not `load` in the dep list — a page already fetched
    // shouldn't refetch itself just because the callback identity changed from an
    // unrelated render; only a real upload or a changed `filter` should reload it, and
    // `load` already captures the current `filter` by closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, filter?.labelId, refreshKey]);

  const remove = useCallback(
    async (id: string) => {
      setConfirming(null);
      setDeleting(id);
      try {
        await deleteDocument(token, id);
        setItems((current) => current.filter((item) => item.id !== id));
      } catch (deleteError) {
        setError(deleteError instanceof ApiError ? deleteError.message : "Couldn't delete that document.");
      } finally {
        setDeleting(null);
      }
    },
    [token],
  );

  if (loading && items.length === 0) {
    return <p className="text-sm text-muted-foreground">Loading documents…</p>;
  }

  if (!loading && items.length === 0) {
    return (
      <p className="rounded-xl border border-border bg-card p-4 text-sm text-muted-foreground">
        {filter ? "No documents in this folder." : "No documents yet — Upload is in the nav to get started."}
      </p>
    );
  }

  return (
    <section className="max-w-3xl space-y-3">
      {error && (
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </p>
      )}

      <ul className="space-y-2">
        {items.map((document_) => (
          <li
            key={document_.id}
            className="overflow-hidden rounded-lg border border-border bg-secondary transition-colors hover:bg-secondary/70"
          >
            <div className="flex items-center gap-3 px-4 py-3">
              <button
                type="button"
                // No specific page or chunk to point at — a row here is the whole document,
                // not a passage a search ranked — so this opens at page 1 with nothing
                // highlighted, same as any citation the viewer can't find bounding boxes
                // for. Still strictly more useful than a document you can see but not open.
                onClick={() =>
                  onCitation({
                    marker: 0,
                    chunk_id: "",
                    document_id: document_.id,
                    filename: document_.filename,
                    page_num: 1,
                    text: "",
                    bboxes: [],
                  })
                }
                className="flex min-w-0 flex-1 items-center gap-3 text-left"
              >
                <FileText className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-foreground">{document_.filename}</p>
                  <p className="text-xs text-muted-foreground">
                    {formatSize(document_.size_bytes)}
                    {document_.page_count !== null && ` · ${document_.page_count} pages`}
                    {" · "}
                    {new Date(document_.created_at).toLocaleDateString()}
                  </p>
                </div>
              </button>
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium capitalize ${
                  STATUS_STYLE[document_.status] ?? "bg-secondary text-muted-foreground"
                }`}
              >
                {document_.status}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Delete ${document_.filename}`}
                disabled={deleting === document_.id}
                onClick={() => setConfirming(document_)}
                className="shrink-0 text-muted-foreground hover:text-destructive"
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          </li>
        ))}
      </ul>

      <Dialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this document?</DialogTitle>
            <DialogDescription>
              {/* RF-03: physical, cascading deletion — not a soft delete, not reversible
                  from here. Naming the filename rather than saying "this document" is the
                  difference between confirming the row you meant and confirming a
                  position in a list that just reloaded. */}
              <strong className="text-foreground">{confirming?.filename}</strong> and everything
              derived from it — its chunks, its citations — will be permanently removed. This
              cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => confirming && void remove(confirming.id)}
              className="bg-destructive text-white hover:bg-destructive/90"
            >
              Delete document
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {cursor && (
        <Button type="button" variant="outline" disabled={loading} onClick={() => void load(cursor, false)}>
          {loading ? "Loading…" : "Show more"}
        </Button>
      )}
    </section>
  );
}
