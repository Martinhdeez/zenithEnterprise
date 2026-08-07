/**
 * The tenant's state, in the sidebar, from one call.
 *
 * Every number here is **label-scoped** by the API — a user sees the size of the corpus
 * they can reach, not the tenant's. That is a security property (mvp.md 3.1: a user who
 * cannot reach a document must not be able to deduce it exists), and the client's only job
 * is not to undermine it by displaying something it fetched from somewhere else.
 */

import { IN_FLIGHT, type TenantStatus } from "@/shared/api/tenant";

export function StatusBadge({ status }: { status: TenantStatus | null }) {
  if (!status) return <p className="text-sm text-muted-foreground">Loading…</p>;

  const ready = status.documents.ready ?? 0;
  const failed = status.documents.failed ?? 0;
  // Summed from the statuses the schema actually defines. The first version of this
  // component added `processing`, which has never been one of them — it was always zero,
  // silently, and the ingestion indicator never appeared. F16 found the same invented
  // status in a backend folder count on the same day.
  const processing = IN_FLIGHT.reduce((total, state) => total + (status.documents[state] ?? 0), 0);

  return (
    <div
      className="space-y-2.5 rounded-lg border border-border p-3 text-sm"
      style={{
        // A flat `bg-secondary` read as just another box in a sidebar full of them — this
        // is the one panel that says "the product is alive right now", so it gets the one
        // bit of color in the nav. Cyan because that's already `--zenith-cyan`'s job on the
        // ready-dot above; the gradient just lets it bleed into its own container instead
        // of stopping at a single pixel.
        backgroundImage:
          "linear-gradient(135deg, color-mix(in oklab, var(--zenith-cyan) 12%, transparent), color-mix(in oklab, var(--zenith-cyan) 2%, transparent) 60%)",
      }}
    >
      <p className="flex items-center gap-1.5">
        <span className="size-1.5 shrink-0 rounded-full bg-zenith-cyan shadow-[0_0_6px_rgba(0,229,229,0.9)]" />
        <span className="font-medium text-foreground">{ready}</span>
        <span className="text-muted-foreground">document{ready === 1 ? "" : "s"} ready</span>
      </p>

      {processing > 0 && (
        // A pill rather than a plain line: while this is non-zero, every search on the box
        // is slower, and it earns a bit more visual weight than the passive metrics below —
        // without being alarming, which is what the amber text alone read as.
        <p
          className="inline-flex items-center gap-1.5 rounded-full bg-zenith-amber/10 px-2 py-0.5 text-xs font-medium text-zenith-amber"
          title="Searches run more slowly while a document is being processed."
        >
          <span className="size-1.5 shrink-0 animate-pulse rounded-full bg-zenith-amber" />
          {processing} processing
        </p>
      )}

      {failed > 0 && (
        // The most useful thing on this component. A failed document is invisible in
        // search — that is what failing means — so if the status does not surface it, the
        // only symptom is an answer that should have existed and did not.
        <p className="inline-flex items-center gap-1.5 rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
          {failed} failed to process
        </p>
      )}

      {!status.searchable && (
        <p className="text-muted-foreground">Nothing to search yet.</p>
      )}

      <dl className="border-t border-border pt-2 text-xs text-muted-foreground">
        <div className="flex justify-between">
          <dt>Passages</dt>
          <dd>{status.chunks.toLocaleString()}</dd>
        </div>
        <div className="flex justify-between">
          <dt>Hardware</dt>
          <dd>{status.hardware}</dd>
        </div>
        {!status.components.reranker && (
          // Stated plainly rather than hidden. On `low-spec` this is the configured
          // product, and F11 proved by measurement that it is the correct configuration
          // for that hardware — the cross-encoder cannot rerank ten passages inside the
          // interactive timeout on four cores.
          <div className="flex justify-between">
            <dt>Reranking</dt>
            <dd>off for this hardware</dd>
          </div>
        )}
        {!status.components.generation && (
          <div className="flex justify-between">
            <dt>Answers</dt>
            <dd>no model configured</dd>
          </div>
        )}
      </dl>
    </div>
  );
}
