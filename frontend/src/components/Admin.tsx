/**
 * The M3 administration screens: roles and the generation connector.
 *
 * Labels are absent deliberately — they have had their own endpoints since F3 and belong
 * with the screens that browse them, not with a settings page. One resource administered
 * from two places is how two screens start disagreeing about what a label is.
 *
 * Both panels here render what the server refuses as plainly as what it accepts. The
 * backend rejects an edit that would leave a tenant with nobody able to administer it, and
 * a screen that hid that refusal behind a generic failure would turn a careful safeguard
 * into a mystery.
 */

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  llmConfig,
  roles as fetchRoles,
  saveLlmConfig,
  setRolePermissions,
  type LlmConfig,
  type Role,
} from "../api/client";

export function Admin({ token }: { token: string }) {
  return (
    <div className="space-y-8">
      <RolePanel token={token} />
      <LlmPanel token={token} />
    </div>
  );
}

function RolePanel({ token }: { token: string }) {
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
    <section className="space-y-3">
      <h2 className="font-semibold">Roles</h2>
      {error && (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
          {error}
        </p>
      )}

      <ul className="space-y-3">
        {roles.map((role) => (
          <li key={role.id} className="rounded-md border border-slate-200 p-3">
            <div className="flex items-baseline justify-between">
              <p className="font-medium">
                {role.name}
                {role.is_system && (
                  // Stated rather than silently disabled: a screen that offers to edit a
                  // system role is offering a 409.
                  <span className="ml-2 text-xs font-normal text-slate-500">
                    built in — not editable
                  </span>
                )}
              </p>
              <span className="text-xs text-slate-500">
                {role.users} user{role.users === 1 ? "" : "s"}
              </span>
            </div>

            <div className="mt-2 flex flex-wrap gap-1">
              {known.map((permission) => (
                <button
                  key={permission}
                  type="button"
                  disabled={role.is_system || busy === role.id}
                  onClick={() => void toggle(role, permission)}
                  className={`rounded px-2 py-0.5 text-xs disabled:opacity-50 ${
                    role.permissions.includes(permission)
                      ? "bg-slate-900 text-white"
                      : "border border-slate-300"
                  }`}
                >
                  {permission}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function LlmPanel({ token }: { token: string }) {
  const [config, setConfig] = useState<LlmConfig | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void llmConfig(token).then((loaded) => {
      setConfig(loaded);
      setEndpoint(loaded.endpoint_url);
      setModel(loaded.model_name);
    });
  }, [token]);

  if (!config) return null;

  return (
    <section className="space-y-3">
      <h2 className="font-semibold">Answer model</h2>
      {!config.configured && (
        <p className="text-sm text-slate-500">
          Using this installation&rsquo;s default. Saving here overrides it for your organisation.
        </p>
      )}

      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setMessage(null);
          try {
            // `api_key` is omitted when the field was left blank, never sent as "". The
            // server reads omission as "keep the stored key" and "" as "clear it", and an
            // administrator editing a model name cannot re-enter a key they cannot read.
            const saved = await saveLlmConfig(token, {
              endpoint_url: endpoint,
              model_name: model,
              ...(key ? { api_key: key } : {}),
            });
            setConfig(saved);
            setKey("");
            setMessage("Saved.");
          } catch (caught) {
            setMessage(caught instanceof ApiError ? caught.message : "That could not be saved.");
          }
        }}
        className="max-w-md space-y-2 text-sm"
      >
        <label className="block">
          Endpoint
          <input
            value={endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
            required
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
          />
        </label>
        <label className="block">
          Model
          <input
            value={model}
            onChange={(event) => setModel(event.target.value)}
            required
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
          />
        </label>
        <label className="block">
          API key
          <input
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            placeholder={config.has_api_key ? "a key is stored — leave blank to keep it" : "none"}
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
          />
        </label>

        <button type="submit" className="rounded bg-slate-900 px-3 py-1 text-white">
          Save
        </button>
        {message && <p className="text-slate-600">{message}</p>}
      </form>
    </section>
  );
}
