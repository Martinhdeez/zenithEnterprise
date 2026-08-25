/**
 * Who changed who could see what.
 *
 * The panel above this one — "Audit Log" — lists questions people asked. That is a useful
 * record and it is not an audit trail, and for a product sold on access control the
 * difference is the whole argument: *"show me who gave this person access to Legal, and
 * when"* was, until now, a question the system could not answer.
 *
 * **The sentences are written here, not by the server.** The API sends a stable
 * `subject.verb` token and a `details` object; prose in the log would be a log nothing can
 * filter, count or alert on, and it would arrive already translated into one language.
 */

import { useCallback, useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";

import { auditEvents, type AuditEvent } from "../api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";

/** The verbs, in the order somebody scanning the list would want to recognise them. */
const SENTENCES: Record<string, string> = {
  "role.created": "created the role",
  "role.deleted": "deleted the role",
  "role.permissions_set": "changed the permissions of",
  "role.clearance_set": "set the clearance of",
  "role.labels_set": "changed which labels are reachable by",
  "user.roles_assigned": "changed the roles of a user",
  "user.groups_set": "changed the groups of a user",
  "group.created": "created the group",
  "group.renamed": "renamed the group",
  "group.deleted": "deleted the group",
  "group.members_set": "changed who is in",
  "group.labels_set": "changed what is readable by",
  "label.created": "created the label",
  "label.renamed": "renamed the label",
  "label.deleted": "deleted a label",
  "label.default_set": "made this the default label",
  "label.clearance_set": "classified the label",
  "label.merged": "merged labels into",
  "llm_config.set": "changed the answer model",
  "llm_config.cleared": "removed the answer model",
  "document.labels_set": "changed the labels on a document",
  "document.classified": "filed a document automatically",
  "tenant.provisioned": "created the organisation",
  "tenant.suspended": "suspended the organisation",
  "tenant.activated": "reactivated the organisation",
  "tenant.purged": "destroyed the organisation",
};

/** Unknown actions still read as something. A token is better than a blank row. */
function describe(action: string): string {
  return SENTENCES[action] ?? action.replace(/[._]/g, " ");
}

/**
 * The address `record_automatic` writes when no person acted.
 *
 * `actor_email` is `NOT NULL`, so an automatic event has to put *something* there — and
 * rendering it beside every human address makes an audit trail where a model looks like a
 * colleague nobody remembers hiring. Named here and shown as what it is.
 */
const AUTOMATIC = "classifier@zenith";

function actor(email: string): string {
  return email === AUTOMATIC ? "Automatic classification" : email;
}

/** The actions that take access away or destroy things, which is what people scan for. */
const GRAVE = new Set([
  "tenant.purged",
  "tenant.suspended",
  "role.deleted",
  "group.deleted",
  "label.deleted",
]);

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * A permission change carries both sides, so the difference is readable without going and
 * finding the previous row. That is the one shape worth rendering specially — everything
 * else is short enough to read raw.
 */
function Detail({ event }: { event: AuditEvent }) {
  const { before, after } = event.details as { before?: string[]; after?: string[] };
  if (Array.isArray(before) && Array.isArray(after)) {
    const gained = after.filter((item) => !before.includes(item));
    const lost = before.filter((item) => !after.includes(item));
    if (gained.length === 0 && lost.length === 0) return null;
    return (
      <p className="mt-1 flex flex-wrap gap-x-3 text-xs">
        {gained.length > 0 && <span className="text-zenith-cyan">+ {gained.join(", ")}</span>}
        {lost.length > 0 && <span className="text-destructive">− {lost.join(", ")}</span>}
      </p>
    );
  }
  const rest = Object.entries(event.details).filter(([, value]) => value !== null);
  if (rest.length === 0) return null;
  return (
    <p className="mt-1 truncate font-mono text-xs text-muted-foreground/80">
      {rest.map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join("  ")}
    </p>
  );
}

export function AuditTrail({ token }: { token: string }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(
    async (from: string | null) => {
      setBusy(true);
      try {
        const page = await auditEvents(token, from);
        // Appended, never replaced: "load more" that discards what you were reading is a
        // control nobody presses twice.
        setEvents((current) => (from ? [...current, ...page.events] : page.events));
        setCursor(page.next_cursor);
        setMessage(null);
      } catch (error) {
        setMessage(
          error instanceof ApiError && error.status === 403
            ? "You do not have permission to read the access record."
            : "The record could not be loaded.",
        );
      } finally {
        setBusy(false);
      }
    },
    [token],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  if (message) {
    return <p className="text-sm text-muted-foreground">{message}</p>;
  }

  if (events.length === 0 && !busy) {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing has changed access yet. Grants, group edits and clearance changes appear here.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <ul className="divide-y-2 divide-border/70 overflow-hidden rounded-md border border-input">
        {events.map((event) => (
          <li key={event.id} className="flex gap-3 px-4 py-3">
            <ShieldCheck
              className={`mt-0.5 size-4 shrink-0 ${
                GRAVE.has(event.action) ? "text-destructive" : "text-muted-foreground"
              }`}
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm text-foreground">
                <span className="font-medium">{actor(event.actor_email)}</span>{" "}
                <span className="text-muted-foreground">{describe(event.action)}</span>
                {event.target_name && <span className="font-medium"> {event.target_name}</span>}
              </p>
              <Detail event={event} />
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">{when(event.created_at)}</span>
          </li>
        ))}
      </ul>

      {cursor && (
        <Button
          type="button"
          variant="outline"
          onClick={() => void load(cursor)}
          disabled={busy}
          className="rounded-md"
        >
          {busy ? "Loading…" : "Load more"}
        </Button>
      )}
    </div>
  );
}
