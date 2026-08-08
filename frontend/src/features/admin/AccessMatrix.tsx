/**
 * Which labels each group opens — one group at a time, not a grid.
 *
 * It was a grid: groups down the side, labels across the top, a checkbox where they met.
 * That shape is wrong for what it holds. A matrix grows in *two* directions, and only one of
 * these axes is bounded — a tenant has a handful of departments and, in real use, thousands
 * of labels — so the table got wider than the screen and stayed that way. Horizontal
 * scrolling is not a thing to make more comfortable; it is a symptom of the wrong shape.
 *
 * So: pick a group, then work down a searchable list of labels. One dimension at a time, the
 * unbounded one running vertically where a list is allowed to be long, and a search box
 * because a list of thousands is not something anybody reads.
 *
 * **Ticking no longer saves.** It used to write immediately, which reads as nothing having
 * happened — there was no moment where the screen said "this is now true". Ticks are local
 * and Save sends the set, which is also how `UserGroups` works, and for the same reason:
 * membership and mapping are access, and access edited one checkbox at a time produces a run
 * of intermediate states that are each briefly real.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, Search, ShieldAlert } from "lucide-react";

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
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Set<string> | null>(null);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    void fetchGroups(token)
      .then((result) => {
        setGroups(result);
        setSelected((current) => current ?? result[0]?.id ?? null);
      })
      .catch((problem: Error) => setError(problem.message))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(load, [load]);

  const group = groups.find((one) => one.id === selected) ?? null;
  // The draft exists only while there are unsaved ticks. Absent means "showing what the
  // server said", which is what makes the Save button's presence meaningful.
  const held = draft ?? new Set(group?.label_ids ?? []);
  const dirty = draft !== null;

  const shown = useMemo(() => {
    const term = search.trim().toLowerCase();
    const matching = term ? labels.filter((l) => l.name.toLowerCase().includes(term)) : labels;
    // Mapped labels first. The question this screen answers most often is "what does this
    // group open", and that answer should not be somewhere down an alphabetical list of
    // everything else.
    return [...matching].sort((a, b) => {
      const byHeld = Number(held.has(b.id)) - Number(held.has(a.id));
      return byHeld !== 0 ? byHeld : a.name.localeCompare(b.name);
    });
  }, [labels, search, held]);

  function toggle(labelId: string) {
    const next = new Set(held);
    if (next.has(labelId)) next.delete(labelId);
    else next.add(labelId);
    setDraft(next);
  }

  async function save() {
    if (!group || !draft) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await setGroupLabels(token, group.id, [...draft]);
      setGroups((current) => current.map((one) => (one.id === group.id ? updated : one)));
      setDraft(null);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "That change was not saved.");
    } finally {
      setSaving(false);
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
        <Loader2 className="size-4 animate-spin" /> Loading groups…
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
        <p role="alert" className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          <ShieldAlert className="mt-0.5 size-4 shrink-0" />
          {error}
        </p>
      )}

      <div className="grid gap-3 md:grid-cols-[minmax(0,14rem)_1fr]">
        {/* Groups: the bounded axis, so it can be a plain list that fits. */}
        <ul className="divide-y divide-border overflow-hidden rounded-md border border-input bg-card">
          {groups.map((one) => (
            <li key={one.id}>
              <button
                type="button"
                aria-pressed={one.id === selected}
                onClick={() => {
                  // Unsaved ticks belong to the group they were made on; carrying them to
                  // another group would apply somebody's intent to the wrong one.
                  setDraft(null);
                  setSelected(one.id);
                }}
                className={`w-full px-3 py-2.5 text-left text-sm transition-colors ${
                  one.id === selected
                    ? "bg-primary/10 text-foreground"
                    : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                }`}
              >
                <span className="block truncate font-medium">{one.name}</span>
                <span className="block text-xs text-muted-foreground">
                  {one.label_ids.length} label{one.label_ids.length === 1 ? "" : "s"} ·{" "}
                  {one.members} member{one.members === 1 ? "" : "s"}
                </span>
              </button>
            </li>
          ))}
        </ul>

        {/* Labels: the unbounded axis, running vertically where length is allowed. */}
        <div className="space-y-2 rounded-md border border-input bg-card p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search labels"
                aria-label="Search labels to map"
                className="w-full rounded-full border border-input bg-input/30 py-1.5 pr-3 pl-8 text-sm text-foreground placeholder:text-muted-foreground focus-visible:border-primary/40 focus-visible:outline-none"
              />
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">
              {held.size} of {labels.length} mapped
            </span>
          </div>

          <ul className="max-h-96 divide-y divide-border overflow-y-auto rounded-md border border-input">
            {shown.map((label) => (
              <li key={label.id} className="flex items-center gap-3 px-3 py-2">
                <input
                  type="checkbox"
                  checked={held.has(label.id)}
                  onChange={() => toggle(label.id)}
                  aria-label={`${group?.name ?? ""} may reach ${label.name}`}
                  className="size-4 shrink-0 accent-primary"
                />
                <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                  {label.name}
                </span>
                {/* The clearance the label demands, beside it rather than in a column
                    header. A tick alone opens nothing without it, and that is the one thing
                    this screen must not let anybody misread. */}
                <select
                  value={label.priority_level ?? 0}
                  onChange={(event) => void classify(label.id, Number(event.target.value))}
                  aria-label={`Clearance required by ${label.name}`}
                  className="shrink-0 rounded border border-input bg-card px-1.5 py-0.5 text-xs text-muted-foreground"
                >
                  <option value={0}>no clearance</option>
                  {Array.from({ length: 10 }, (_, index) => index + 1).map((level) => (
                    <option key={level} value={level}>
                      level {level}
                    </option>
                  ))}
                </select>
              </li>
            ))}
          </ul>

          {shown.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No label matches that.
            </p>
          )}

          <div className="flex items-center justify-between gap-3 pt-1">
            <p className="text-xs text-muted-foreground">
              Members still need their role&apos;s clearance to reach the level each label
              demands — the two are independent.
            </p>
            {dirty && (
              <div className="flex shrink-0 gap-2">
                <Button variant="ghost" size="sm" onClick={() => setDraft(null)}>
                  Cancel
                </Button>
                <Button size="sm" disabled={saving} onClick={() => void save()}>
                  {saving ? "Saving…" : "Save"}
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>
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
