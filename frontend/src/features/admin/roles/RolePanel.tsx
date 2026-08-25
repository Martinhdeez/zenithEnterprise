/**
 * What each role may do.
 *
 * The API refuses to edit a system role whatever this screen sends — `admin` and `member` are
 * `is_system` — so the control is disabled here as well. Not belt and braces: a screen that
 * offers an action the server will reject teaches an administrator that the product is
 * unreliable, and the refusal is meant to be the safety net rather than the interface.
 */

import { useCallback, useEffect, useState } from "react";

import { roles as fetchRoles, setRolePermissions, type Role } from "../api";
import { ApiError } from "@/shared/api/http";

export function RolePanel({ token }: { token: string }) {
  const [roles, setRoles] = useState<Role[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

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

  const known = [...new Set(roles.flatMap((role) => role.permissions))].sort();

  return (
    <div className="space-y-4">
      {error && (
        <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </p>
      )}

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
              <span className="text-xs text-muted-foreground">
                {role.users} user{role.users === 1 ? "" : "s"}
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
