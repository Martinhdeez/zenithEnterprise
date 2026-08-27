/**
 * The corpus itself, browsable — what clicking a folder in the sidebar was missing.
 *
 * `Folders` only ever narrowed *Chat* and *Search*; there was no screen that actually
 * listed a document you could look at or remove. `GET /documents` has existed since F4, and
 * nothing in the client called it until this component did.
 */

import { useCallback, useEffect, useState } from "react";
import { FileText, ShieldCheck, Trash2 } from "lucide-react";

import { deleteDocument, listDocuments, type DocumentSummary } from "../api";
import { TagChips, labels as fetchLabels, type Label as LabelType } from "@/features/labels";
import { AccessInspector } from "./AccessInspector";
import { DocumentDetail } from "./DocumentDetail";
import { ApiError } from "@/shared/api/http";
import type { Citation } from "@/features/chat";
import { Button } from "@/components/ui/button";
import { useT } from "@/shared/i18n/useT";
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
  /** Clicking a chip narrows the view to that label. Omitted where nothing can filter. */
  onSelectTag?: (name: string) => void;
  /**
   * The caller's permission codes. The access inspector is offered only to somebody who
   * holds `roles.manage`, because it names groups and roles — and a screen that lists the
   * groups a viewer is not in tells them those groups exist, which is the inference
   * mvp.md 3.1 forbids. The API refuses them anyway; this stops the affordance appearing.
   */
  permissions?: string[];
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

export function Documents({
  token,
  onCitation,
  onSelectTag,
  filter,
  refreshKey = 0,
  permissions = [],
}: Props) {
  const t = useT();
  const mayInspect = permissions.includes("roles.manage");
  const [inspecting, setInspecting] = useState<string | null>(null);
  // Label ids resolve to names only for the labels this caller reaches — `GET /labels`
  // already applies that rule. A document may carry an id absent from this map, which
  // means RLS admitted the document through some *other* label; that name is not theirs
  // to learn, so it is dropped rather than shown as a uuid.
  const [known, setKnown] = useState<Map<string, string>>(new Map());
  // The same labels, unflattened, for the access inspector — it needs each one's clearance
  // as well as its name, and deriving a level from a name is not a thing that can be done.
  const [reachable, setReachable] = useState<LabelType[]>([]);

  useEffect(() => {
    let cancelled = false;
    void fetchLabels(token)
      .then((all) => {
        if (cancelled) return;
        setKnown(new Map(all.map((label) => [label.id, label.name])));
        setReachable(all);
      })
      .catch(() => !cancelled && setKnown(new Map()));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const namesFor = useCallback(
    (document_: DocumentSummary): string[] =>
      document_.label_ids
        .map((id) => known.get(id))
        .filter((name): name is string => name !== undefined)
        .sort(),
    [known],
  );
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
        setError(fetchError instanceof ApiError ? fetchError.message : t("Couldn't load documents."));
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
        setError(deleteError instanceof ApiError ? deleteError.message : t("Couldn't delete that document."));
      } finally {
        setDeleting(null);
      }
    },
    [token],
  );

  if (loading && items.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("Loading documents…")}</p>;
  }

  if (!loading && items.length === 0) {
    return (
      <p className="rounded-xl border border-border bg-card p-4 text-sm text-muted-foreground">
        {filter ? t("No documents in this folder.") : t("No documents yet — Upload is in the nav to get started.")}
      </p>
    );
  }

  return (
    <section className="space-y-3">
      {error && (
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </p>
      )}

      <ul className="space-y-2">
        {items.map((document_) => (
          <li
            key={document_.id}
            className="overflow-hidden rounded-lg border border-border bg-secondary shadow-sm transition-colors hover:bg-secondary/70"
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
                    media_type: document_.media_type,
                    // From the library rather than from an answer: no cited passage, so
                    // the first page and no highlight. Inventing one would point the
                    // reader at something the corpus never said.
                    page_num: document_.media_type.startsWith("text/") ? null : 1,
                    char_start: 0,
                    char_end: 0,
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

              {/* Names, resolved from the labels this caller already holds. A document can
                  carry an id the caller does not reach — RLS let them see the document
                  through a *different* label — and that name is not theirs to learn, so it
                  is silently absent rather than rendered as an id. */}
              <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
                <TagChips names={namesFor(document_)} onSelect={onSelectTag} short />
                {mayInspect && (
                  <button
                    type="button"
                    aria-label={`Who can open ${document_.filename}`}
                    title={t("Who can open this")}
                    onClick={() =>
                      setInspecting((current) =>
                        current === document_.id ? null : document_.id,
                      )
                    }
                    className="rounded p-1 text-muted-foreground transition-colors hover:text-foreground"
                  >
                    <ShieldCheck className="size-3.5" />
                  </button>
                )}
              </div>
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

            {inspecting === document_.id && (
              <div className="space-y-3 border-t border-border p-3">
                {/* What the document is, above who can read it. Somebody who opened this
                    row wants to know whether the thing is any good before they ask which
                    compartments it sits in — and if ingestion failed, that answers both
                    questions at once. */}
                <DocumentDetail
                  token={token}
                  document={document_}
                  labelNames={namesFor(document_)}
                  onSelectTag={onSelectTag}
                />
                <AccessInspector
                  token={token}
                  filename={document_.filename}
                  labelIds={document_.label_ids}
                  labels={reachable}
                />
              </div>
            )}
          </li>
        ))}
      </ul>

      <Dialog open={confirming !== null} onOpenChange={(open) => !open && setConfirming(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("Delete this document?")}</DialogTitle>
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
            <Button type="button" variant="outline" onClick={() => setConfirming(null)}>{t("Cancel")}</Button>
            <Button
              type="button"
              onClick={() => confirming && void remove(confirming.id)}
              className="bg-destructive text-white hover:bg-destructive/90"
            >{t("Delete document")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {cursor && (
        <Button type="button" variant="outline" disabled={loading} onClick={() => void load(cursor, false)}>
          {loading ? t("Loading…") : t("Show more")}
        </Button>
      )}
    </section>
  );
}
