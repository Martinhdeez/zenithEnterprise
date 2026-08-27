/**
 * What each role may do.
 *
 * The API refuses to edit a system role whatever this screen sends — `admin` and `member` are
 * `is_system` — so the control is disabled here as well. Not belt and braces: a screen that
 * offers an action the server will reject teaches an administrator that the product is
 * unreliable, and the refusal is meant to be the safety net rather than the interface.
 */

import { useCallback, useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import {
  createRole,
  deleteRole,
  roles as fetchRoles,
  setRoleClearance,
  setRolePermissions,
  type Role,
} from "../api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function RolePanel({ token }: { token: string }) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // Creating and deleting roles, and setting a clearance, have been in the API and in
  // `api.ts` since they were written, and no screen called any of them. "A custom role is
  // configuration rather than a release" is a sentence this product sells on, and it was only
  // true over HTTP.
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      setRoles(await fetchRoles(token));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Roles could not be loaded.");
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (role: Role, permission: string) => {
    setBusy(role.id);
    setError(null);
    const next = role.permissions.includes(permission)
      ? role.permissions.filter((held) => held !== permission)
      : [...role.permissions, permission];
    try {
      await setRolePermissions(token, role.id, next);
      await load();
    } catch (caught) {
      // Shown verbatim. The server's refusals here are specific and actionable — "this
      // would leave nobody in the tenant holding: roles.manage" tells an administrator
      // exactly what to do first, and a generic message would not.
      setError(caught instanceof ApiError ? caught.message : "That change was refused.");
    } finally {
      setBusy(null);
    }
  };

  /** One `try` for every call that changes a role, because they all fail the same way.

      The server's refusals here are specific and actionable — "this would leave nobody in the
      tenant holding: roles.manage" tells an administrator exactly what to do first — so they
      are shown verbatim and never replaced with a sentence of our own. */
  const attempt = async (id: string, work: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await work();
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That change was refused.");
    } finally {
      setBusy(null);
    }
  };

  const known = [...new Set(roles.flatMap((role) => role.permissions))].sort();

  return (
    <div className="space-y-4">
      {error && (
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </p>
      )}

      {/* A role starts with no permissions and no clearance, which is the safe end to start
          from: the panel below is where it is given anything, one deliberate click at a time.
          A form that offered permissions at creation would be a role granted in a single
          gesture nobody reviews. */}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setCreating(true);
          void attempt("new", async () => {
            await createRole(token, { name: name.trim(), permissions: [], priority_level: 0 });
            setName("");
          }).finally(() => setCreating(false));
        }}
      >
        <Input
          aria-label="New role name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="editor"
          className="h-9 w-48 border-border bg-card text-foreground"
        />
        <Button type="submit" size="sm" disabled={!name.trim() || creating} className="h-9">
          <Plus className="mr-1.5 size-3.5" />
          {creating ? "Creating…" : "New role"}
        </Button>
      </form>

      <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
        {roles.map((role) => (
          <li key={role.id} className="p-4">
            <div className="flex items-baseline justify-between">
              <p className="font-medium text-foreground">
                {role.name}
                {role.is_system && (
                  // Stated rather than silently disabled: a screen that offers to edit a
                  // system role is offering a 409.
                  <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs font-normal text-muted-foreground">
                    built in — not editable
                  </span>
                )}
              </p>
              <span className="flex items-center gap-3 text-xs text-muted-foreground">
                {/* Clearance is the vertical half of the access model and had no control at
                    all. 0 is a compartment — no clearance reaches anything through a group,
                    only an outright grant does — which is the default and the safe one. */}
                <label className="flex items-center gap-1.5">
                  clearance
                  <select
                    aria-label={`Clearance of ${role.name}`}
                    disabled={role.is_system || busy === role.id}
                    value={role.priority_level}
                    onChange={(event) =>
                      void attempt(role.id, () =>
                        setRoleClearance(token, role.id, Number(event.target.value)),
                      )
                    }
                    className="rounded border border-border bg-card px-1.5 py-0.5 text-foreground disabled:opacity-50"
                  >
                    {Array.from({ length: 11 }, (_, level) => (
                      <option key={level} value={level}>
                        {level}
                      </option>
                    ))}
                  </select>
                </label>
                <span>
                  {role.users} user{role.users === 1 ? "" : "s"}
                </span>
                {!role.is_system && (
                  <button
                    type="button"
                    aria-label={`Delete ${role.name}`}
                    disabled={busy === role.id}
                    onClick={() => void attempt(role.id, () => deleteRole(token, role.id))}
                    className="rounded p-1 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                )}
              </span>
            </div>

            <div className="mt-3 flex flex-wrap gap-1.5">
              {known.map((permission) => (
                <button
                  key={permission}
                  type="button"
                  disabled={role.is_system || busy === role.id}
                  onClick={() => void toggle(role, permission)}
                  className={`rounded-full px-2.5 py-1 font-mono text-xs transition-colors disabled:opacity-50 ${
                    role.permissions.includes(permission)
                      ? "bg-primary text-white"
                      : "border border-border text-muted-foreground hover:bg-secondary/50"
                  }`}
                >
                  {permission}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
