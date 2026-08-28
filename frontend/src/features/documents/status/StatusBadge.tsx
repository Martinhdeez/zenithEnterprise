/**
 * The tenant's state, in the sidebar, from one call.
 *
 * Every number here is **label-scoped** by the API — a user sees the size of the corpus
 * they can reach, not the tenant's. That is a security property (mvp.md 3.1: a user who
 * cannot reach a document must not be able to deduce it exists), and the client's only job
 * is not to undermine it by displaying something it fetched from somewhere else.
 */

import type { ReactNode } from "react";

import { IN_FLIGHT, type TenantStatus } from "@/shared/api/tenant";
import { useFormat, useT } from "@/shared/i18n/useT";

/**
 * `children` is the ingestion indicator, drawn inside this box rather than beside it.
 *
 * They were two sidebar sections, each with its own uppercase label and its own rule — two
 * headings and two dividers to introduce one card and one line of text. They answer the
 * same question ("what is behind this product right now"), so they are one panel now and
 * the labels are gone: a card whose two largest numbers are captioned "documentos listos"
 * and "pasajes" does not need a heading above it saying "Estado".
 */
export function StatusBadge({
  status,
  children,
}: {
  status: TenantStatus | null;
  children?: ReactNode;
}) {
  const t = useT();
  const format = useFormat();
  if (!status) return <p className="text-sm text-muted-foreground">{t("Loading…")}</p>;

  const ready = status.documents.ready ?? 0;
  const failed = status.documents.failed ?? 0;
  const processing = IN_FLIGHT.reduce((total, state) => total + (status.documents[state] ?? 0), 0);
  const total = ready + processing + failed;
  const share = (part: number) => (total === 0 ? 0 : (part / total) * 100);

  return (
    // The corpus as a bar, rather than a box with numbers printed in it.
    //
    // The four facts here were four different kinds of thing set as one list: a quantity
    // that matters, an internal unit nobody outside this product thinks in, a deployment
    // detail that never changes, and a live state. Printed as four rows they read as equal,
    // and the panel needed three rules to keep them apart.
    //
    // The bar carries the three that are one fact — how much of this corpus is searchable
    // right now — as proportions instead of prose. Ready, processing and failed are
    // segments of the same length, so ingestion and failure are visible *in* the object
    // rather than announced beside it, and a full cyan bar is a state anyone can read
    // without counting: everything in here can be found.
    //
    // It is drawn at every count, including zero-processing, which is the same reason the
    // ingestion line used to state "nothing being ingested" rather than disappear: an
    // indicator you cannot see is one you cannot tell apart from an indicator that broke.
    //
    // No frame and no tint. This sits inside a bordered sidebar above a filled footer; a
    // third boundary around it was a box drawn inside a box.
    <div className="flex flex-col gap-2.5">
      <p className="flex items-baseline gap-2">
        <span className="text-[30px] leading-none font-semibold tracking-[-0.02em] tabular-nums text-foreground">
          {format.number(ready)}
        </span>
        <span className="min-w-0 text-xs leading-tight text-muted-foreground">
          {t(ready === 1 ? "document ready" : "documents ready")}
        </span>
      </p>

      {/* `aria-hidden` on purpose: every quantity this encodes is already written in the
          text above and below it, and a screen reader announcing the same three numbers a
          second time as an image would be noise, not access. */}
      <div aria-hidden className="flex h-1.5 gap-px overflow-hidden rounded-full bg-secondary">
        <div className="bg-zenith-cyan transition-[width] duration-500" style={{ width: `${share(ready)}%` }} />
        {processing > 0 && (
          <div
            className="animate-pulse bg-zenith-amber transition-[width] duration-500"
            style={{ width: `${share(processing)}%` }}
          />
        )}
        {failed > 0 && (
          <div className="bg-destructive transition-[width] duration-500" style={{ width: `${share(failed)}%` }} />
        )}
      </div>

      {/* One line for everything that is a condition rather than a quantity. The labels
          survive as tooltips: "cpu" alone is cryptic, and "Hardware: cpu" spelled out was
          a whole row for a value that changes once, at install. */}
      <p className="flex flex-wrap items-baseline gap-x-1.5 text-[11px] text-muted-foreground/80">
        <span className="tabular-nums">{format.number(status.chunks)}</span>
        <span>{t("passages")}</span>
        <span aria-hidden className="text-muted-foreground/40">&middot;</span>
        <span className="font-mono" title={t("Hardware")}>
          {status.hardware}
        </span>
        {!status.components.reranker && (
          <>
            <span aria-hidden className="text-muted-foreground/40">&middot;</span>
            <span title={t("Reranking")}>{t("off for this hardware")}</span>
          </>
        )}
        {!status.components.generation && (
          <>
            <span aria-hidden className="text-muted-foreground/40">&middot;</span>
            <span title={t("Answers")}>{t("no model configured")}</span>
          </>
        )}
      </p>

      {failed > 0 && (
        // Kept as its own line rather than left to the red segment. A failed document is
        // invisible in search — that is what failing means — and a three-pixel stripe is
        // not a thing anybody is owed as the only notice of it.
        <p className="inline-flex items-center gap-1.5 self-start rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
          {t("{count} failed to process", { count: failed })}
        </p>
      )}

      {!status.searchable && <p className="text-sm text-muted-foreground">{t("Nothing to search yet.")}</p>}

      {children}
    </div>
  );
}
