/**
 * The M3 administration screens: roles, labels and the generation connector.
 *
 * Labels used to be absent here on the argument that they belong with the screens that
 * browse them, and that one resource administered from two places is how two screens start
 * disagreeing about what a label is. F21 added `TagManager` anyway, for a reason the
 * original note did not cover: filing a document under a label and administering the label
 * *set* — finding four spellings of Finance, folding them together, seeing what carries
 * what — are different jobs, and only the first one belongs on an upload form. The risk the
 * note identified is answered by both screens reading the same endpoints rather than by
 * keeping one of them out.
 *
 * Every panel here renders what the server refuses as plainly as what it accepts. The
 * backend rejects an edit that would leave a tenant with nobody able to administer it,
 * refuses to delete a label documents still carry, and refuses a merge that would widen
 * visibility unacknowledged. A screen that hid any of those behind a generic failure would
 * turn a careful safeguard into a mystery.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import { inviteUser, llmConfig, roles as fetchRoles, saveLlmConfig, setRolePermissions, type Invitation, type LlmConfig, type Role } from "./api";
import { ApiError } from "@/shared/api/http";
import { TagManager, labels as fetchLabels, type Label as LabelType } from "@/features/labels";
import { AccessMatrix, GroupManager } from "./AccessMatrix";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const FIELD =
  "border-border bg-card text-foreground placeholder:text-muted-foreground " +
  "focus-visible:border-primary focus-visible:ring-primary/40";

/** One administration panel: a title, a hairline rule beneath it, generous padding around
    the content. Every panel on this screen shares this frame so the eye reads them as one
    settings surface rather than three differently-built widgets stacked on a page. */
function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <h2 className="border-b border-border px-5 py-3.5 text-sm font-semibold text-foreground">
        {title}
      </h2>
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Admin({ token }: { token: string }) {
  const [labels, setLabels] = useState<LabelType[]>([]);
  // Reloaded rather than mutated in place: a label's clearance is edited from inside the
  // matrix, and the matrix is drawn from this list, so the change has to come back through
  // the same fetch everything else reads.
  const reloadLabels = useCallback(() => {
    void fetchLabels(token)
      .then(setLabels)
      .catch(() => setLabels([]));
  }, [token]);
  useEffect(reloadLabels, [reloadLabels]);

  return (
    <div className="space-y-6">
      <Panel title="Invite a colleague">
        <InvitePanel token={token} />
      </Panel>
      <Panel title="Groups">
        <GroupManager token={token} onChanged={reloadLabels} />
      </Panel>
      <Panel title="Access matrix">
        <AccessMatrix token={token} labels={labels} onLabelsChanged={reloadLabels} />
      </Panel>
      <Panel title="Roles">
        <RolePanel token={token} />
      </Panel>
      <Panel title="Labels">
        <TagManager token={token} />
      </Panel>
      <Panel title="Answer model">
        <LlmPanel token={token} />
      </Panel>
    </div>
  );
}

function InvitePanel({ token }: { token: string }) {
  const [email, setEmail] = useState("");
  // "none" rather than "" — Radix `Select.Item` refuses an empty-string value, since that's
  // reserved to mean "cleared" internally.
  const [roleId, setRoleId] = useState("none");
  const [available, setAvailable] = useState<Role[]>([]);
  const [issued, setIssued] = useState<Invitation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetchRoles(token)
      .then(setAvailable)
      .catch(() => setAvailable([]));
  }, [token]);

  if (issued) {
    // The password is returned once and is not recoverable, so this replaces the form
    // rather than sitting beside it — an administrator who closes it without copying has
    // to invite again, and that is only cheap if there is nothing else competing for their
    // attention on the screen.
    return (
      <div className="space-y-3 rounded-lg border border-zenith-amber/30 bg-zenith-amber/10 p-4 text-sm">
        <p className="font-medium text-foreground">{issued.email} can now sign in.</p>
        <p className="text-foreground">
          Password:{" "}
          <code className="rounded bg-background px-1.5 py-0.5 font-mono text-foreground">
            {issued.password}
          </code>
        </p>
        <p className="text-muted-foreground">
          This is shown once and cannot be retrieved. Copy it now and pass it on the way you
          already share credentials.
        </p>
        <Button
          type="button"
          variant="outline"
          onClick={() => setIssued(null)}
          className="border-zenith-amber/40 text-foreground hover:bg-zenith-amber/10"
        >
          I have copied it
        </Button>
      </div>
    );
  }

  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setError(null);
        try {
          setIssued(await inviteUser(token, email, roleId === "none" ? [] : [roleId]));
          setEmail("");
          setRoleId("none");
        } catch (caught) {
          // Shown verbatim: "that address is already a user here" is actionable, and a
          // generic failure is not.
          setError(caught instanceof ApiError ? caught.message : "The invitation failed.");
        }
      }}
      className="flex flex-wrap items-end gap-3"
    >
      <div className="min-w-56 flex-1 space-y-1.5">
        <Label htmlFor="invite-email" className="text-sm text-foreground/80">
          Email
        </Label>
        <Input
          id="invite-email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
          className={`h-10 ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="invite-role" className="text-sm text-foreground/80">
          Role
        </Label>
        <Select value={roleId} onValueChange={setRoleId}>
          <SelectTrigger id="invite-role" className={`h-10 w-40 ${FIELD}`}>
            <SelectValue placeholder="none" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">none</SelectItem>
            {available.map((role) => (
              <SelectItem key={role.id} value={role.id}>
                {role.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Button type="submit" className="h-10 bg-primary text-white hover:bg-primary/90">
        Invite
      </Button>

      {error && (
        <p role="alert" className="w-full text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
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
      className="space-y-4"
    >
      {!config.configured && (
        <p className="text-sm text-muted-foreground">
          Using this installation&rsquo;s default. Saving here overrides it for your organisation.
        </p>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="llm-endpoint" className="text-sm text-foreground/80">
          Endpoint
        </Label>
        <Input
          id="llm-endpoint"
          value={endpoint}
          onChange={(event) => setEndpoint(event.target.value)}
          required
          className={`h-10 w-full ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="llm-model" className="text-sm text-foreground/80">
          Model
        </Label>
        <Input
          id="llm-model"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          required
          className={`h-10 w-full ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="llm-key" className="text-sm text-foreground/80">
          API key
        </Label>
        <Input
          id="llm-key"
          type="password"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder={config.has_api_key ? "a key is stored — leave blank to keep it" : "none"}
          className={`h-10 w-full ${FIELD}`}
        />
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" className="h-10 bg-primary text-white hover:bg-primary/90">
          Save
        </Button>
        {message && <p className="text-sm text-muted-foreground">{message}</p>}
      </div>
    </form>
  );
}
