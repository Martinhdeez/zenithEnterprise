/**
 * The tenant's state, in the sidebar, from one call.
 *
 * Every number here is **label-scoped** by the API — a user sees the size of the corpus
 * they can reach, not the tenant's. That is a security property (mvp.md 3.1: a user who
 * cannot reach a document must not be able to deduce it exists), and the client's only job
 * is not to undermine it by displaying something it fetched from somewhere else.
 */

import { IN_FLIGHT, type TenantStatus } from "../api/client";

export function StatusBadge({ status }: { status: TenantStatus | null }) {
  if (!status) return <p className="text-sm text-slate-500">Loading…</p>;

  const ready = status.documents.ready ?? 0;
  const failed = status.documents.failed ?? 0;
  // Summed from the statuses the schema actually defines. The first version of this
  // component added `processing`, which has never been one of them — it was always zero,
  // silently, and the ingestion indicator never appeared. F16 found the same invented
  // status in a backend folder count on the same day.
  const processing = IN_FLIGHT.reduce((total, state) => total + (status.documents[state] ?? 0), 0);

  return (
    <div className="space-y-2 text-sm">
      <p>
        <span className="font-medium">{ready}</span> document{ready === 1 ? "" : "s"} ready
      </p>

      {processing > 0 && (
        // Worth its own line: while this is non-zero, every search on the box is slower,
        // and a user who knows why is not a user reporting a fault.
        <p className="text-sky-700">{processing} processing — searches will be slower</p>
      )}

      {failed > 0 && (
        // The most useful thing on this component. A failed document is invisible in
        // search — that is what failing means — so if the status does not surface it, the
        // only symptom is an answer that should have existed and did not.
        <p className="font-medium text-red-700">
          {failed} failed to process
        </p>
      )}

      {!status.searchable && (
        <p className="text-slate-500">Nothing to search yet.</p>
      )}

      <dl className="border-t border-slate-200 pt-2 text-xs text-slate-500">
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
