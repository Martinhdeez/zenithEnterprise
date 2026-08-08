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

import {
  changePassword,
  profile as fetchProfile,
  renameSelf,
  signOutEverywhere,
  type UserProfile,
} from "./api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Props {
  token: string;
  /** Called once every session is invalidated, so the shell can drop its tokens. */
  onSignedOut: () => void;
  /**
   * Reported upward on load and on every change, because the shell draws the avatar from
   * the same profile and would otherwise keep showing the initial of the email after
   * somebody had just set their name on this very screen.
   */
  onProfile: (profile: UserProfile) => void;
}

export function Profile({ token, onSignedOut, onProfile }: Props) {
  const [me, setMe] = useState<UserProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  const remember = useCallback(
    (result: UserProfile) => {
      setMe(result);
      onProfile(result);
    },
    [onProfile],
  );

  useEffect(() => {
    let cancelled = false;
    void fetchProfile(token)
      .then((result) => !cancelled && remember(result))
      .catch((failure) => !cancelled && setError(message(failure, "Your profile couldn't be loaded.")));
    return () => {
      cancelled = true;
    };
  }, [token, remember]);

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
          <Row label="Name">
            <EditableName token={token} value={me.name} onSaved={remember} />
          </Row>
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
        <Sessions token={token} onSignedOut={onSignedOut} />
      </Panel>
    </div>
  );
}

/**
 * The name, in place, because it is the one field on this screen its owner may change.
 *
 * Everything else here is either an identifier (email) or granted by somebody else (roles,
 * labels, permissions) — a profile that let you edit your own access would defeat the
 * point of having roles. So one field is editable and the rest reads as the record it is.
 */
function EditableName({
  token,
  value,
  onSaved,
}: {
  token: string;
  value: string | null;
  onSaved: (profile: UserProfile) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => {
          setDraft(value ?? "");
          setEditing(true);
        }}
        className="text-foreground underline decoration-muted-foreground/40 underline-offset-4 transition-colors hover:decoration-foreground"
      >
        {value ?? <Absent>Set a name</Absent>}
      </button>
    );
  }

  return (
    <form
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        setBusy(true);
        setError(null);
        void renameSelf(token, draft)
          .then((updated) => {
            onSaved(updated);
            setEditing(false);
          })
          .catch((failure) => setError(message(failure, "That name couldn't be saved.")))
          .finally(() => setBusy(false));
      }}
      className="flex flex-col items-end gap-1.5"
    >
      {/* Bordered box removed on purpose: this row is a definition list, and dropping a
          field-shaped control into it made one line look like a form and the rest like
          data. Underline only, right-aligned, same size and position as the text it
          replaces — so entering edit mode moves nothing on the page. */}
      <input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Your name"
        aria-label="Your name"
        maxLength={200}
        autoFocus
        // Escape leaves without saving, which is the shortcut anyone editing in place
        // reaches for before they look for a Cancel button.
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setEditing(false);
            setError(null);
          }
        }}
        className="w-48 border-b border-muted-foreground/40 bg-transparent pb-0.5 text-right text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground/50 focus:border-primary"
      />
      <div className="flex items-center gap-3 text-xs">
        {error && (
          <span role="alert" className="text-destructive">
            {error}
          </span>
        )}
        <button
          type="button"
          onClick={() => {
            setEditing(false);
            setError(null);
          }}
          className="text-muted-foreground transition-colors hover:text-foreground"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy}
          className="font-medium text-primary transition-colors hover:text-primary/80 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </form>
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

/**
 * Both ways of ending a session, read together.
 *
 * Plain sign-out used to live in the sidebar and this one lived here, which left the
 * difference between them to be inferred from two labels in two places. Side by side, one
 * says "this device" and the other says "all of them", and the second's real limitation —
 * it cannot kill a live access token, only stop it renewing — is stated where somebody
 * choosing between them will read it.
 */
function Sessions({ token, onSignedOut }: { token: string; onSignedOut: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="space-y-5">
      <div className="space-y-3">
        <p className="max-w-prose text-sm text-muted-foreground">
          Sign out on this device. Your other sessions are untouched.
        </p>
        <Button type="button" variant="outline" onClick={onSignedOut} className="gap-1.5 rounded-md">
          <LogOut className="size-4" />
          Sign out
        </Button>
      </div>

      <div className="space-y-3 border-t border-border pt-5">
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
    </div>
  );
}

/** `bg-secondary`, not `bg-card`: the page's own `<main>` is already `bg-card`, so a panel
    painted the same navy is invisible but for its hairline. The palette has three steps and
    a panel is the third — the same frame the administration screens use, so the two read as
    one product rather than two screens built by different hands. */
function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-secondary shadow-sm">
      <h2 className="flex items-center gap-2 rounded-t-lg border-b border-border bg-card px-5 py-3.5 text-sm font-semibold text-foreground">
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
