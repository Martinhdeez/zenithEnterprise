/**
 * Who reads what, as a grid.
 *
 * Groups down the side, labels across the top, a checkbox where they meet. The grid is the
 * point: access spread across a role screen, a label screen and a membership screen is
 * access nobody can review, and the question an administrator actually has — "who can see
 * the payroll folder?" — is a column, not a page.
 *
 * **A tick is necessary, not sufficient.** Ticking a cell maps a label to a group; a member
 * still reads nothing there unless they also carry the clearance the label demands. That is
 * the whole design — horizontal and vertical are independent — so the clearance is shown in
 * the column header rather than left for someone to discover from a support ticket.
 */

import { useEffect, useState } from "react";
import { Loader2, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { Label } from "@/features/labels";
import { type Group, groups as fetchGroups, setGroupLabels, setLabelClearance } from "./api";

interface Props {
  token: string;
  labels: Label[];
  /** So the parent can refetch labels after a clearance changes under it. */
  onLabelsChanged?: () => void;
}

export function AccessMatrix({ token, labels, onLabelsChanged }: Props) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchGroups(token)
      .then((result) => !cancelled && setGroups(result))
      .catch((problem: Error) => !cancelled && setError(problem.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  async function toggle(group: Group, labelId: string) {
    // Computed from what is on screen rather than from a local optimistic copy: the server
    // replaces the whole set, so sending a set derived from stale state would silently
    // revoke a mapping somebody else just made.
    const next = group.label_ids.includes(labelId)
      ? group.label_ids.filter((id) => id !== labelId)
      : [...group.label_ids, labelId];

    setSaving(`${group.id}:${labelId}`);
    setError(null);
    try {
      const updated = await setGroupLabels(token, group.id, next);
      setGroups((current) => current.map((one) => (one.id === group.id ? updated : one)));
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "That change was not saved.");
    } finally {
      setSaving(null);
    }
  }

  async function classify(labelId: string, level: number) {
    setError(null);
    try {
      await setLabelClearance(token, labelId, level);
      onLabelsChanged?.();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "That change was not saved.");
    }
  }

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> Loading the access matrix…
      </p>
    );
  }

  if (groups.length === 0) {
    return (
      <p className="rounded-md border border-input bg-card p-4 text-sm text-muted-foreground">
        No groups yet. A group is a part of the business — Finance, Engineering,
        Project-Alpha — and mapping labels to one is how its members reach documents.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <p className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          <ShieldAlert className="mt-0.5 size-4 shrink-0" />
          {error}
        </p>
      )}

      {/* The table scrolls inside its own box: a tenant with thirty labels must not make
          the whole page scroll sideways. */}
      <div className="overflow-x-auto rounded-md border border-input">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-input bg-secondary/50">
              <th className="sticky left-0 z-10 bg-secondary/50 p-3 text-left font-medium">
                Group
              </th>
              {labels.map((label) => (
                <th key={label.id} className="min-w-28 p-3 text-center font-medium">
                  <span className="block truncate" title={label.name}>
                    {label.name}
                  </span>
                  {/* The clearance the label demands, editable in place. Shown here because
                      a tick below means nothing without it. */}
                  <select
                    value={label.priority_level ?? 0}
                    onChange={(event) => void classify(label.id, Number(event.target.value))}
                    aria-label={`Clearance required by ${label.name}`}
                    className="mt-1 rounded border border-input bg-card px-1 py-0.5 text-xs font-normal text-muted-foreground"
                  >
                    <option value={0}>no clearance</option>
                    {Array.from({ length: 10 }, (_, index) => index + 1).map((level) => (
                      <option key={level} value={level}>
                        level {level}
                      </option>
                    ))}
                  </select>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.id} className="border-b border-input last:border-0">
                <th
                  scope="row"
                  className="sticky left-0 z-10 bg-card p-3 text-left font-medium whitespace-nowrap"
                >
                  {group.name}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {group.members} member{group.members === 1 ? "" : "s"}
                  </span>
                </th>
                {labels.map((label) => {
                  const busy = saving === `${group.id}:${label.id}`;
                  return (
                    <td key={label.id} className="p-3 text-center">
                      <input
                        type="checkbox"
                        checked={group.label_ids.includes(label.id)}
                        disabled={busy}
                        onChange={() => void toggle(group, label.id)}
                        aria-label={`${group.name} may reach ${label.name}`}
                        className="size-4 accent-primary"
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-muted-foreground">
        A tick maps a label to a group. Members still read nothing there unless their role's
        clearance is at or above the level in the column header — the two are independent,
        and neither opens anything on its own.
      </p>
    </div>
  );
}

/** Creating and removing groups, beside the matrix that maps them. */
export function GroupManager({ token, onChanged }: { token: string; onChanged?: () => void }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reload = () => void fetchGroups(token).then(setGroups).catch(() => setGroups([]));
  useEffect(reload, [token]);

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Finance, Engineering, Project-Alpha…"
          aria-label="New group name"
          className="flex-1 rounded-md border border-input bg-card px-3 py-2 text-sm"
        />
        <Button
          disabled={!name.trim()}
          onClick={async () => {
            setError(null);
            try {
              const { createGroup } = await import("./api");
              await createGroup(token, { name: name.trim() });
              setName("");
              reload();
              onChanged?.();
            } catch (problem) {
              setError(problem instanceof Error ? problem.message : "That group was not created.");
            }
          }}
        >
          Add group
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <ul className="divide-y divide-border rounded-md border border-input">
        {groups.map((group) => (
          <li key={group.id} className="flex items-center justify-between p-3 text-sm">
            <span>
              {group.name}
              <span className="ml-2 text-xs text-muted-foreground">
                {group.members} member{group.members === 1 ? "" : "s"} ·{" "}
                {group.label_ids.length} label{group.label_ids.length === 1 ? "" : "s"}
              </span>
            </span>
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive"
              onClick={async () => {
                const { deleteGroup } = await import("./api");
                // Deleting removes the access rather than transferring it, so the count of
                // people it affects goes in the confirmation.
                if (
                  !confirm(
                    `Delete ${group.name}? Its ${group.members} member(s) lose whatever it opened.`,
                  )
                )
                  return;
                await deleteGroup(token, group.id);
                reload();
                onChanged?.();
              }}
            >
              Delete
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
