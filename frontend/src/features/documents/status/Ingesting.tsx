/**
 * What the server is still chewing on, visible from every screen.
 *
 * A migration is confirmed on the upload screen and then takes minutes to hours. The queue
 * on that screen answers "how is this batch going" and answers it only while you stand
 * there; navigate to Search and the work becomes invisible, which is how somebody uploads
 * the same folder twice or closes the tab believing it finished.
 *
 * This is the whole-installation view instead, and it needs no new plumbing: `GET
 * /tenant/status` already reports documents by status, already runs on a timer in `App`,
 * and is already label-scoped — so the count is what *this* user's corpus is doing, not the
 * tenant's, which is the same property every other number in the sidebar has.
 *
 * It renders in the collapsed sidebar too. A progress indicator that disappears when the
 * sidebar narrows is one nobody can rely on, and a dot with a count fits in 64 pixels where
 * the full status panel honestly does not.
 */

import { Check, Loader2 } from "lucide-react";

import { IN_FLIGHT, type TenantStatus } from "@/shared/api/tenant";
import { useT } from "@/shared/i18n/useT";

export function inFlight(status: TenantStatus | null): number {
  if (!status) return 0;
  return IN_FLIGHT.reduce((total, state) => total + (status.documents[state] ?? 0), 0);
}

export function ready(status: TenantStatus | null): number {
  return status?.documents.ready ?? 0;
}

export function Ingesting({
  status,
  collapsed,
}: {
  status: TenantStatus | null;
  collapsed: boolean;
}) {
  const t = useT();
  const processing = inFlight(status);
  const done = ready(status);

  if (collapsed) {
    // A dot and a number. Anything with words in it is unreadable at this width, and an
    // icon alone cannot say *how many*, which is the only question this answers.
    return (
      <div
        className="flex flex-col items-center gap-1 py-2"
        title={processing > 0 ? `Ingesting ${processing} document(s)` : "Nothing being ingested"}
      >
        {processing > 0 ? (
          <>
            <Loader2 className="size-4 animate-spin text-zenith-amber" />
            <span className="text-[10px] font-medium text-zenith-amber">{processing}</span>
          </>
        ) : (
          <Check className="size-4 text-muted-foreground/50" />
        )}
      </div>
    );
  }

  if (processing === 0) {
    // Stated rather than hidden. An indicator that vanishes when idle is one you cannot
    // tell apart from an indicator that is broken, and "did my upload finish or did the
    // widget die" is exactly the question this exists to answer.
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Check className="size-3.5 shrink-0" />{t("Nothing being ingested")}</p>
    );
  }

  const total = processing + done;

  return (
    <div className="space-y-1.5">
      <p className="flex items-center gap-1.5 text-xs font-medium text-zenith-amber">
        <Loader2 className="size-3.5 shrink-0 animate-spin" />
        Ingesting {processing} of {total}
      </p>
      {/* Against the searchable corpus rather than against the batch: the browser does not
          know how many documents the *server* has queued — another tab, another person, a
          requeue from the CLI — and a bar measured against one batch would sit at 40% while
          six hundred other documents went through it. */}
      <div className="h-1 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full rounded-full bg-zenith-amber transition-[width] duration-500"
          style={{ width: `${total === 0 ? 0 : Math.round((done / total) * 100)}%` }}
        />
      </div>
      <p className="text-[11px] text-muted-foreground">{t("Searches run more slowly while this is happening.")}</p>
    </div>
  );
}
