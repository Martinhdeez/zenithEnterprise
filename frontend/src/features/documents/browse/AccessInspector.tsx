/**
 * Who can open this file, and why — beside the file itself.
 *
 * The access matrix in Admin answers "who can see payroll?". This answers the question an
 * administrator has while looking at one document: *this* document, which labels does it
 * carry, what does each one demand, and who does that add up to.
 *
 * It is the derived answer that makes it worth a panel rather than a list of chips. A label
 * on its own says nothing about who reads it: reachable through a group only if the reader
 * is in that group *and* cleared for the level, or through an outright grant to a role. All
 * three facts live on different screens, and joining them by hand is how an administrator
 * ends up believing a document is restricted when it is not.
 *
 * Every number here is still RLS-scoped. A group or role the caller cannot see is absent
 * rather than counted, so this describes the access *this administrator can account for* —
 * which is stated on the panel rather than left to be assumed.
 */

import { useEffect, useState } from "react";
import { Loader2, ShieldCheck } from "lucide-react";

import { type Group, type Role, groups as fetchGroups, roles as fetchRoles } from "@/features/admin";
import type { Label } from "@/features/labels";

interface Props {
  token: string;
  filename: string;
  /** The labels this document carries, as ids. */
  labelIds: string[];
  /** Every label the caller reaches, for resolving ids to names and levels. */
  labels: Label[];
}

export function AccessInspector({ token, filename, labelIds, labels }: Props) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([fetchGroups(token), fetchRoles(token)])
      .then(([g, r]) => {
        if (cancelled) return;
        setGroups(g);
        setRoles(r);
      })
      .catch(() => {
        if (cancelled) return;
        setGroups([]);
        setRoles([]);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (loading) {
    return (
      <p className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Working out who can open this…
      </p>
    );
  }

  const carried = labels.filter((label) => labelIds.includes(label.id));

  // An unlabelled document is visible tenant-wide — that is what the empty array means in
  // the schema, and it is the one case where naming no label is the least restrictive
  // outcome rather than the most.
  if (labelIds.length === 0) {
    return (
      <div className="rounded-md border border-input bg-input/60 p-4 text-sm">
        <p className="font-medium text-foreground">{filename}</p>
        <p className="mt-1 text-muted-foreground">
          Carries no label, which makes it visible to everyone in this organisation. Add a
          label to restrict it.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3 rounded-md border border-input bg-input/60 p-4 text-sm">
      <p className="flex items-center gap-2 font-medium text-foreground">
        <ShieldCheck className="size-4 text-primary" />
        Who can open {filename}
      </p>

      <ul className="space-y-3">
        {carried.map((label) => {
          // Necessary and sufficient are different: a group opens the label only to members
          // whose clearance also reaches it, so both halves are named per row.
          const throughGroups = groups.filter((group) => group.label_ids.includes(label.id));
          const throughGrant = roles.filter((role) => role.label_ids.includes(label.id));

          return (
            <li key={label.id} className="border-l-2 border-border pl-3">
              <p className="font-medium text-foreground">
                {label.name}
                <span className="ml-2 text-xs font-normal text-muted-foreground">
                  {label.priority_level > 0
                    ? `needs clearance ${label.priority_level}`
                    : "needs no clearance"}
                </span>
              </p>

              {throughGroups.length > 0 ? (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Groups: {throughGroups.map((group) => group.name).join(", ")}
                  {label.priority_level > 0 &&
                    " — members also need the clearance above"}
                </p>
              ) : (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  No group opens this label.
                </p>
              )}

              {throughGrant.length > 0 && (
                <p className="mt-0.5 text-xs text-muted-foreground">
                  Granted outright to: {throughGrant.map((role) => role.name).join(", ")} —
                  ignoring group and clearance.
                </p>
              )}

              {throughGroups.length === 0 && throughGrant.length === 0 && (
                <p className="mt-0.5 text-xs text-zenith-amber">
                  Nobody reaches this label. A document carrying only labels like this one is
                  readable by no one.
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {carried.length < labelIds.length && (
        // The honest caveat. Ids the caller does not reach are not resolved to names —
        // that is the leak the listing is careful about — so this panel cannot claim to be
        // the whole picture.
        <p className="text-xs text-muted-foreground">
          {labelIds.length - carried.length} further label(s) on this document are ones you
          do not hold, and are not shown.
        </p>
      )}
    </div>
  );
}
