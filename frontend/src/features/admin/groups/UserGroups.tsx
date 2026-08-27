/**
 * The staff list, and which groups each person is in.
 *
 * The access matrix says which labels a group opens. This says who is in the group — the
 * other half of the same sentence, and the half that was only reachable through the API
 * until now.
 *
 * **Saved per person, not per checkbox.** Group membership is access, and access edited a
 * tick at a time produces a run of intermediate states that are each briefly true: remove
 * Finance, save, add Legal, save, and for a moment somebody was in neither. Ticking is
 * local and Save sends the set, so the change an administrator meant is the change that
 * happens.
 */

import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2, Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  type Group,
  type Invitation,
  type Member,
  groups as fetchGroups,
  issueResetLink,
  users as fetchUsers,
  setUserGroups,
} from "../api";
import { ApiError } from "@/shared/api/http";
import { SingleUseLink } from "../invite/SingleUseLink";
import { useT } from "@/shared/i18n/useT";

export function UserGroups({ token }: { token: string }) {
  const t = useT();
  const [members, setMembers] = useState<Member[]>([]);
  // The endpoint has existed and been tested since the credential links shipped, and no
  // screen called it. There is no outbound mail on an on-premise install, so the alternative
  // for somebody locked out of their account was an operator with shell access running
  // `zenith reset-password` — which is a support ticket for a thing an administrator is
  // entitled to do.
  const [resetting, setResetting] = useState<string | null>(null);
  const [link, setLink] = useState<Invitation | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** Unsaved ticks, keyed by user. Absent means "no pending edit for this person". */
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    void Promise.all([fetchUsers(token), fetchGroups(token)])
      .then(([people, allGroups]) => {
        setMembers(people);
        setGroups(allGroups);
        // Dropped on reload: what came back from the server is now the truth, and keeping
        // a draft over it would show an edit that has already been applied as pending.
        setDraft({});
      })
      .catch((problem: Error) => setError(problem.message))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(load, [load]);

  const held = (member: Member) => draft[member.id] ?? member.group_ids;
  const edited = (member: Member) => draft[member.id] !== undefined;

  function toggle(member: Member, groupId: string) {
    const current = held(member);
    const next = current.includes(groupId)
      ? current.filter((id) => id !== groupId)
      : [...current, groupId];
    setDraft((all) => ({ ...all, [member.id]: next }));
  }

  async function save(member: Member) {
    setSaving(member.id);
    setError(null);
    try {
      await setUserGroups(token, member.id, held(member));
      // Re-read rather than patching local state: the server is what decides membership,
      // and a screen that congratulates itself without asking is a screen that can be
      // wrong about who has access.
      load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "That change was not saved.");
    } finally {
      setSaving(null);
    }
  }

  async function reset(member: Member) {
    setResetting(member.id);
    setError(null);
    try {
      setLink(await issueResetLink(token, member.id));
    } catch (failure) {
      setError(
        failure instanceof ApiError ? failure.message : "That reset link could not be issued.",
      );
    } finally {
      setResetting(null);
    }
  }

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />{t("Loading people…")}</p>
    );
  }

  if (groups.length === 0) {
    return (
      <p className="rounded-md border border-input bg-input/60 p-4 text-sm text-muted-foreground">{t("No groups yet. Create one above, then come back to put people in it.")}</p>
    );
  }

  if (link) {
    // Replaces the list for the same reason the invitation replaces its form: the link is
    // returned once, and a panel competing with forty rows of checkboxes is a link somebody
    // closes without copying.
    return (
      <SingleUseLink
        issued={link}
        headline={`${link.email} can set a new password with this link.`}
        onDone={() => setLink(null)}
      />
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </p>
      )}

      <ul className="divide-y divide-border rounded-md border border-input">
        {members.map((member) => (
          <li key={member.id} className="space-y-2.5 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">
                  {member.name ?? member.email}
                </p>
                {member.name && (
                  <p className="truncate text-xs text-muted-foreground">{member.email}</p>
                )}
              </div>
              {!edited(member) && (
                // Only while the row is settled. Beside an unsaved group change it would
                // compete with Save, and issuing a link is not what somebody halfway through
                // editing memberships meant to click.
                <Button
                  variant="ghost"
                  size="sm"
                  className="shrink-0 text-muted-foreground hover:text-foreground"
                  disabled={resetting === member.id}
                  onClick={() => void reset(member)}
                >
                  <KeyRound className="mr-1.5 size-3.5" />
                  {resetting === member.id ? "Issuing…" : "Reset link"}
                </Button>
              )}
              {edited(member) && (
                <div className="flex shrink-0 gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setDraft((all) => {
                        const { [member.id]: _discarded, ...rest } = all;
                        return rest;
                      })
                    }
                  >{t("Cancel")}</Button>
                  <Button size="sm" disabled={saving === member.id} onClick={() => void save(member)}>
                    {saving === member.id ? "Saving…" : "Save"}
                  </Button>
                </div>
              )}
            </div>

            <div className="flex flex-wrap gap-1.5">
              {groups.map((group) => {
                const inGroup = held(member).includes(group.id);
                return (
                  <button
                    key={group.id}
                    type="button"
                    role="checkbox"
                    aria-checked={inGroup}
                    aria-label={`${member.name ?? member.email} in ${group.name}`}
                    onClick={() => toggle(member, group.id)}
                    className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                      inGroup
                        ? "border-primary/50 bg-primary/10 text-foreground"
                        : "border-input bg-input/60 text-muted-foreground hover:border-primary/40 hover:text-foreground"
                    }`}
                  >
                    {group.name}
                  </button>
                );
              })}
            </div>

            {held(member).length === 0 && (
              // Said rather than left as an empty row. Somebody in no group reaches nothing
              // through groups at all, which is easy to mistake for "not configured yet".
              <p className="text-xs text-muted-foreground">{t("In no group — reaches documents only through labels granted to their roles.")}</p>
            )}
          </li>
        ))}
      </ul>

      <p className="flex items-start gap-2 text-xs text-muted-foreground">
        <Users className="mt-0.5 size-3.5 shrink-0" />
        Being in a group is half of what opens a document. Members also need their role's
        clearance to reach the level each label demands.
      </p>
    </div>
  );
}
