/**
 * Choosing your own password, from a link.
 *
 * The only screen in the product reachable without signing in, which is the whole point:
 * the people who need it either have no account yet or cannot get into the one they have.
 *
 * It is deliberately not a route in the app shell. The shell renders the sidebar, fetches
 * a profile and expects a token; none of that exists here, and pretending otherwise means
 * an unauthenticated request failing on a screen whose entire job is to work before you
 * are authenticated. `App.tsx` checks the path before it decides anything else.
 */

import { useCallback, useEffect, useState } from "react";
import { KeyRound } from "lucide-react";

import { describeCredential, redeemCredential } from "./api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** Matches the server's minimum. Checked here so the answer is instant, and there too. */
const MINIMUM = 12;

type State =
  | { phase: "checking" }
  | { phase: "ready"; email: string; purpose: string }
  | { phase: "done" }
  | { phase: "dead" };

export function SetPassword({ token }: { token: string }) {
  const [state, setState] = useState<State>({ phase: "checking" });
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void describeCredential(token)
      .then((subject) => setState({ phase: "ready", ...subject }))
      // Expired, already used and never issued arrive as one answer, on purpose: telling
      // them apart would confirm to somebody guessing that a string had once been real.
      .catch(() => setState({ phase: "dead" }));
  }, [token]);

  const submit = useCallback(async () => {
    if (password.length < MINIMUM) {
      setMessage(`Use at least ${MINIMUM} characters.`);
      return;
    }
    if (password !== again) {
      // Checked before sending, because the link is spent on the server's first success and
      // a typo that burned it would leave somebody locked out holding a dead link.
      setMessage("The two passwords are not the same.");
      return;
    }
    setBusy(true);
    try {
      await redeemCredential(token, password);
      setState({ phase: "done" });
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.status === 404
          ? "This link is no longer valid. Ask your administrator for a new one."
          : "The password could not be set.",
      );
    } finally {
      setBusy(false);
    }
  }, [token, password, again]);

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-6 rounded-xl border border-border bg-card p-8">
        <div className="flex size-11 items-center justify-center rounded-full border border-input bg-secondary">
          <KeyRound className="size-5 text-primary" />
        </div>

        {state.phase === "checking" && (
          <p className="text-sm text-muted-foreground">Checking the link…</p>
        )}

        {state.phase === "dead" && (
          <div className="space-y-2">
            <h1 className="text-lg font-medium text-foreground">This link no longer works</h1>
            <p className="text-sm text-muted-foreground">
              Links can be used once and expire on their own. Ask your administrator to send
              a new one.
            </p>
          </div>
        )}

        {state.phase === "done" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <h1 className="text-lg font-medium text-foreground">Your password is set</h1>
              <p className="text-sm text-muted-foreground">You can sign in now.</p>
            </div>
            <Button type="button" onClick={() => (window.location.href = "/")} className="rounded-md">
              Go to sign in
            </Button>
          </div>
        )}

        {state.phase === "ready" && (
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <div className="space-y-2">
              <h1 className="text-lg font-medium text-foreground">
                {state.purpose === "reset" ? "Choose a new password" : "Welcome to Zenith"}
              </h1>
              {/* Naming the account matters most on a reset: somebody with two addresses
                  needs to know which one this link is about before they commit a password
                  to it. */}
              <p className="text-sm text-muted-foreground">
                Setting the password for <span className="text-foreground">{state.email}</span>.
              </p>
            </div>

            <Input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="New password"
              aria-label="New password"
              autoFocus
              className="rounded-md"
            />
            <Input
              type="password"
              value={again}
              onChange={(event) => setAgain(event.target.value)}
              placeholder="Repeat it"
              aria-label="Repeat the password"
              className="rounded-md"
            />

            {message && <p className="text-sm text-destructive">{message}</p>}

            <Button type="submit" disabled={busy} className="w-full rounded-md">
              {busy ? "Setting…" : "Set password"}
            </Button>
            <p className="text-xs text-muted-foreground">
              At least {MINIMUM} characters. Nobody else ever sees it — not even the
              administrator who invited you.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
