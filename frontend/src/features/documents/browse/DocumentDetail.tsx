/**
 * What a document *is*, as opposed to what it says.
 *
 * Opening a PDF showed the PDF. Everything else about it — how big, how many passages it
 * became, which compartments it sits in, who put it there, whether ingestion actually
 * finished, whether any answer has ever used it — was either on another screen or nowhere.
 *
 * The number worth the extra request is **how many answers have cited it**. A corpus where
 * you can see which documents are load-bearing and which have never been reached by a
 * single question is a corpus somebody can curate; without it, every upload looks equally
 * useful for ever.
 */

import { useEffect, useState } from "react";
import { FileText, Hash, Layers, Quote, User } from "lucide-react";

import { documentInsights, type DocumentInsights, type DocumentSummary } from "../api";
import { TagChips } from "@/features/labels";

function bytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} kB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** One label-and-value row. The icon is what makes six of these scannable. */
function Fact({
  icon: Icon,
  label,
  children,
}: {
  icon: typeof FileText;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2.5">
      <Icon className="size-3.5 shrink-0 translate-y-0.5 text-muted-foreground" />
      <span className="w-28 shrink-0 text-xs text-muted-foreground">{label}</span>
      <span className="min-w-0 flex-1 text-sm text-foreground">{children}</span>
    </div>
  );
}

export function DocumentDetail({
  token,
  document,
  labelNames,
  onSelectTag,
}: {
  token: string;
  document: DocumentSummary;
  /** Resolved by the caller against `GET /labels` — only the ones this caller reaches. */
  labelNames: string[];
  onSelectTag?: (name: string) => void;
}) {
  const [insights, setInsights] = useState<DocumentInsights | null>(null);

  useEffect(() => {
    // Per document, not per list row: both counts are aggregates over other tables, and
    // paying for them on every row of a page that shows neither is work nobody asked for.
    setInsights(null);
    void documentInsights(token, document.id)
      .then(setInsights)
      .catch(() => setInsights(null));
  }, [token, document.id]);

  const failed = document.status === "failed";
  // A document can finish ingesting and still have something to say — pages that produced no
  // text, an automatic filing that could not run. `status_detail` carried those long before
  // anything displayed them: it was rendered only for `failed`, so "2 of 4 page(s) are not
  // searchable" was written to the database on every mixed-content upload and shown to
  // nobody. The one reader who needs it is looking at this panel wondering why an answer did
  // not come from this document.
  const caution = !failed && document.status === "ready" ? document.status_detail : null;

  return (
    <div className="space-y-4 rounded-lg border border-input bg-secondary p-4">
      <div className="space-y-1">
        <p className="truncate text-sm font-medium text-foreground">{document.filename}</p>
        {document.description && (
          <p className="text-sm text-muted-foreground">{document.description}</p>
        )}
      </div>

      {/* Failure first and in its own colour. A document that never finished ingesting is
          not searchable, and every other number on this panel is beside the point until
          somebody knows that. */}
      {failed && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-sm text-destructive">
          Ingestion failed{document.status_detail ? `: ${document.status_detail}` : "."} This
          document is not searchable.
        </p>
      )}

      {/* Amber, not red, and deliberately so: the document *is* searchable and most of it is
          in the index. Colouring it as a failure would send somebody re-uploading work that
          is already done. */}
      {caution && (
        <p className="rounded-md border border-zenith-amber/30 bg-zenith-amber/10 p-2.5 text-sm text-zenith-amber">
          {caution}
        </p>
      )}

      <div className="space-y-2">
        <Fact icon={FileText} label="Pages">
          {document.page_count ?? "—"}
        </Fact>
        <Fact icon={Layers} label="Passages">
          {/* The unit retrieval actually searches, which says more about whether this is
              findable than a page count does. */}
          {insights ? insights.chunks : "…"}
        </Fact>
        <Fact icon={Quote} label="Cited by">
          {insights === null
            ? "…"
            : insights.answers === 0
              ? "No answers yet"
              : `${insights.answers} answer${insights.answers === 1 ? "" : "s"}`}
        </Fact>
        <Fact icon={User} label="Uploaded">
          {/* From `insights`, not from the document: the list payload carries the uploader
              as an id, and "uploaded by 7b1c5fd3-a760…" answers nothing a person asked. */}
          {insights === null
            ? "…"
            : (insights.uploaded_by ?? "someone since removed")}{" "}
          · {when(document.created_at)}
        </Fact>
        <Fact icon={Hash} label="Size">
          {bytes(document.size_bytes)}
        </Fact>
      </div>

      <div className="space-y-1.5">
        <p className="text-xs text-muted-foreground">Readable through</p>
        {labelNames.length > 0 ? (
          <TagChips names={labelNames} onSelect={onSelectTag} short />
        ) : (
          // Not the same as "no labels": the caller may reach this document through a
          // compartment they are not entitled to see the name of, and inventing "None"
          // would state something the API deliberately does not.
          <p className="text-sm text-muted-foreground">
            No labels you can see. Ask an administrator if this looks wrong.
          </p>
        )}
      </div>
    </div>
  );
}
