/**
 * Sign in.
 *
 * The API refuses to distinguish "no such user" from "wrong password", because telling
 * them apart turns a login form into an account-enumeration tool. This form must not undo
 * that by being helpful — whatever the API says is what the user sees, and nothing is
 * inferred from the status code.
 */

import { useState } from "react";

import { ApiError, login } from "../api/client";

export function Login({ onAuthenticated }: { onAuthenticated: (token: string) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex h-screen items-center justify-center bg-slate-50">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          try {
            const { access_token } = await login(email, password);
            onAuthenticated(access_token);
          } catch (caught) {
            setError(caught instanceof ApiError ? caught.message : "Sign in failed.");
          } finally {
            setBusy(false);
          }
        }}
        className="w-80 space-y-3 rounded-md border border-slate-200 bg-white p-6"
      >
        <h1 className="text-lg font-semibold">Zenith</h1>

        <label className="block text-sm">
          Email
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
          />
        </label>

        <label className="block text-sm">
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1"
          />
        </label>

        {error && (
          <p role="alert" className="text-sm text-red-800">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-slate-900 py-2 text-white disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
