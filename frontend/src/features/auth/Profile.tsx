/**
 * Who you are signed in as, and the one credential you can change yourself.
 *
 * The screen exists for a reason narrower than "every product has a profile page". This
 * product hides documents by label, and the question it prompts more than any other is
 * "why can I not see the file my colleague is talking about". The answer is always the
 * same — the label on it is not one your roles reach — and until now there was nowhere to
 * read that. `Access` is the part of this screen that earns its place.
 *
 * Changing your own password is the other half, and it was simply missing: one was
 * generated when you were invited, read out once, and could only ever be replaced by an
 * administrator with shell access running `reset-password`.
 */

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Loader2, LogOut, ShieldCheck } from "lucide-react";

import { changePassword, profile as fetchProfile, signOutEverywhere, type UserProfile } from "./api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Props {
  token: string;
  /** Called once every session is invalidated, so the shell can drop its tokens. */
  onSignedOut: () => void;
}

export function Profile({ token, onSignedOut }: Props) {
  const [me, setMe] = useState<UserProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchProfile(token)
      .then((result) => !cancelled && setMe(result))
      .catch((failure) => !cancelled && setError(message(failure, "Your profile couldn't be loaded.")));
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (error && !me) {
    return (
      <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
        {error}
      </p>
    );
  }

  if (!me) {
    return (
      <p className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        Loading…
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <Panel title="Account">
        <dl className="space-y-3 text-sm">
          <Row label="Name">{me.name ?? <Absent>Not set</Absent>}</Row>
          <Row label="Email">{me.email}</Row>
          <Row label="Organisation">{me.tenant_name ?? <Absent>Unnamed</Absent>}</Row>
          <Row label="Member since">{new Date(me.created_at).toLocaleDateString()}</Row>
          <Row label="Documents uploaded">{me.documents_uploaded}</Row>
        </dl>
      </Panel>

      <Panel title="Access">
        <div className="space-y-4 text-sm">
          <Field label="Roles">
            <Chips values={me.roles} empty="No roles assigned" />
          </Field>
          <Field
            label="Labels you reach"
            hint="A document is visible to you only if it carries one of these — or none at all."
          >
            <Chips values={me.labels} empty="None — you see only unlabelled documents" />
          </Field>
          <Field label="Permissions">
            <div className="flex flex-wrap gap-1.5">
              {me.permissions.map((permission) => (
                <code
                  key={permission}
                  className="rounded border border-input bg-card px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
                >
                  {permission}
                </code>
              ))}
            </div>
          </Field>
        </div>
      </Panel>

      <Panel title="Password">
        <PasswordForm token={token} onChanged={onSignedOut} />
      </Panel>

      <Panel title="Sessions">
        <SignOutEverywhere token={token} onSignedOut={onSignedOut} />
      </Panel>
    </div>
  );
}

function PasswordForm({ token, onChanged }: { token: string; onChanged: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBusy(true);
      setError(null);
      try {
        await changePassword(token, current, next);
        // Changing it ends every session, this one included, so there is nothing to stay
        // on this screen for — the shell drops the tokens and shows the login form.
        onChanged();
      } catch (failure) {
        setError(message(failure, "That password couldn't be changed."));
      } finally {
        setBusy(false);
      }
    },
    [token, current, next, onChanged],
  );

  return (
    <form onSubmit={(event) => void submit(event)} className="max-w-sm space-y-3">
      <div className="space-y-1.5">
        <label htmlFor="current-password" className="text-xs font-medium text-muted-foreground">
          Current password
        </label>
        <Input
          id="current-password"
          type="password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          autoComplete="current-password"
          className="rounded-md border-input bg-background text-foreground focus-visible:border-primary focus-visible:ring-primary/40"
        />
      </div>
      <div className="space-y-1.5">
        <label htmlFor="new-password" className="text-xs font-medium text-muted-foreground">
          New password <span className="font-normal">(at least 8 characters)</span>
        </label>
        <Input
          id="new-password"
          type="password"
          value={next}
          onChange={(event) => setNext(event.target.value)}
          autoComplete="new-password"
          className="rounded-md border-input bg-background text-foreground focus-visible:border-primary focus-visible:ring-primary/40"
        />
      </div>
      {/* Said before the button is pressed, not after: it changes what the action means. */}
      <p className="text-xs text-muted-foreground">
        Your other sessions stop being able to renew themselves. One that is already open
        keeps working until its access token expires.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button
        type="submit"
        disabled={busy || current.length === 0 || next.length < 8}
        className="rounded-md"
      >
        {busy ? "Changing…" : "Change password"}
      </Button>
    </form>
  );
}

function SignOutEverywhere({ token, onSignedOut }: { token: string; onSignedOut: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="space-y-3">
      {/* The honest version of what this does. Access tokens are stateless by design
          (mvp.md 2.4) — nothing reads the database to check one — so this ends the ability
          to *renew* a session rather than killing it mid-flight. Promising more than that
          on a security control would be the worst place to overstate. */}
      <p className="max-w-prose text-sm text-muted-foreground">
        Ends every session, including this one. Sessions already open stop working once
        their access token expires; none of them can renew itself after this.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setError(null);
          void signOutEverywhere(token)
            .then(onSignedOut)
            .catch((failure) => {
              setError(message(failure, "Those sessions couldn't be ended."));
              setBusy(false);
            });
        }}
        className="gap-1.5 rounded-md"
      >
        <LogOut className="size-4" />
        {busy ? "Ending…" : "Sign out everywhere"}
      </Button>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <h2 className="flex items-center gap-2 border-b border-border px-5 py-3.5 text-sm font-semibold text-foreground">
        {title === "Access" && <ShieldCheck className="size-4 text-primary" />}
        {title}
      </h2>
      <div className="p-5">{children}</div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right text-foreground">{children}</dd>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      {hint && <p className="text-xs text-muted-foreground/70">{hint}</p>}
      {children}
    </div>
  );
}

function Chips({ values, empty }: { values: string[]; empty: string }) {
  if (values.length === 0) return <Absent>{empty}</Absent>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {values.map((value) => (
        <span
          key={value}
          className="rounded-full border border-input bg-secondary px-3 py-1 text-xs text-foreground"
        >
          {value}
        </span>
      ))}
    </div>
  );
}

function Absent({ children }: { children: React.ReactNode }) {
  return <span className="text-sm text-muted-foreground/60 italic">{children}</span>;
}

function message(failure: unknown, fallback: string): string {
  return failure instanceof ApiError ? failure.message : fallback;
}
