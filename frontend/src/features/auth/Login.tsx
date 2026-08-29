/**
 * Sign in.
 *
 * The API refuses to distinguish "no such user" from "wrong password", because telling
 * them apart turns a login form into an account-enumeration tool. This form must not undo
 * that by being helpful — whatever the API says is what the user sees, and nothing is
 * inferred from the status code.
 *
 * The visual design comes from the "Obsidian Trust" prototype. Two things in it were
 * changed rather than copied, because a login screen that promises something the product
 * does not do is worse than a plain one:
 *
 *   - The prototype's "forgot your password?" link is gone. There is no reset flow: users
 *     are created by an administrator through `POST /users/invite`, which returns a
 *     password once. A link to nowhere is a support ticket.
 *   - The footer is text, not a link, for the same reason — contacting an administrator is
 *     genuinely the recovery path, and there is no page to send anyone to.
 *
 * The screen is dark while the rest of the application is light, so it carries `dark` on
 * its own root instead of the document. The redesign moves the rest across; until then the
 * two must not fight over `<html>`.
 */

import { useState } from "react";
import { ArrowRight, Eye, EyeOff, Lock, Mail, ShieldCheck } from "lucide-react";

import { login, type TokenPair } from "./api";
import { ApiError } from "@/shared/api/http";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/shared/i18n/useT";

// Focus ring matches the Sign In button's own colour (--primary, "Indigo Electric") rather
// than the cyan used for status/security indicators elsewhere on this screen — cyan means
// "the system is healthy" here, and reusing it for focus would blur that meaning.
// Tokens, not literals. This screen carries `dark` on its root, so `--input`,
// `--secondary`, `--foreground` and `--muted-foreground` already resolve to the dark
// palette here — the hex values these replaced were frozen copies of exactly those four,
// and a copy is only ever right until the palette moves. It moved once already, on 26
// August, and this screen kept the old field colours while every screen behind it changed.
const FIELD =
  "h-11 border-input bg-secondary text-foreground placeholder:text-muted-foreground " +
  "focus-visible:border-primary focus-visible:ring-primary/40";

export function Login({ onAuthenticated }: { onAuthenticated: (tokens: TokenPair) => void }) {
  const t = useT();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="dark relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4 pt-6 pb-40">
      {/* Ambient indigo/blue glow */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 45% at 50% 0%, rgba(99,102,241,0.22), transparent 70%), radial-gradient(45% 40% at 85% 90%, rgba(59,130,246,0.14), transparent 70%)",
        }}
      />
      {/* Grid texture, masked so it fades out before the edges */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-[0.15]"
        style={{
          backgroundImage:
            "linear-gradient(to right, rgba(148,163,184,0.08) 1px, transparent 1px), linear-gradient(to bottom, rgba(148,163,184,0.08) 1px, transparent 1px)",
          backgroundSize: "44px 44px",
          maskImage: "radial-gradient(70% 60% at 50% 30%, black, transparent 90%)",
        }}
      />

      <div className="relative z-10 flex w-full flex-col items-center">
        <header className="mb-8 flex flex-col items-center text-center">
          {/* No wordmark: the mark carries the brand alone, and the name reappears once,
              small, as the lead word of the caption below rather than twice on the page. */}
          <div className="relative flex size-20 items-center justify-center">
            {/* A soft glow behind the mark, and nothing more.

                A liquid-goo treatment lived here briefly: three blobs drifting through an
                SVG filter so they merged and pinched apart. It was doing what it was asked
                to do and it was wrong for the surround — at this size the shape reads as a
                stain on the logo rather than as atmosphere behind it, because the mark is
                small enough that the blob competes with it instead of sitting under it.
                An effect that has to be squinted past is decoration that stopped serving
                anything.

                The two brand colours, the same ones the goo used: the primary, and the
                cyan that means "healthy" everywhere else in the product. */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-0 blur-xl"
              style={{
                background:
                  "radial-gradient(55% 55% at 50% 45%, color-mix(in oklab, var(--primary) 45%, transparent), transparent 70%), radial-gradient(45% 45% at 55% 70%, color-mix(in oklab, var(--zenith-cyan) 35%, transparent), transparent 70%)",
              }}
            />
            <img
              src="/zenith-mark.png"
              alt="Zenith"
              width={80}
              height={80}
              className="relative size-20 object-contain drop-shadow-[0_0_12px_rgba(0,229,229,0.35)]"
            />
          </div>
          {/* A different typeface from the rest of the interface on purpose — the same
              treatment as the "RLS Security Enforced" badge below, so the caption reads as
              a designed detail rather than a plain paragraph of the UI's body font. */}
          <p className="mt-5 font-mono text-xs tracking-wide text-balance text-muted-foreground">{t("Zenith Enterprise Document Intelligence Platform")}</p>
        </header>

        <div className="w-full max-w-md">
          <div
            className="relative overflow-hidden rounded-2xl border border-slate-800/80 bg-card p-8 shadow-2xl shadow-black/40"
            style={{
              backgroundImage:
                "radial-gradient(120% 100% at 50% -10%, rgba(99,102,241,0.10), transparent 60%)",
            }}
          >
            {/* Hairline glow along the top edge of the card */}
            <div className="pointer-events-none absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-primary/60 to-transparent" />

            <form
              onSubmit={async (event) => {
                event.preventDefault();
                setBusy(true);
                setError(null);
                try {
                  onAuthenticated(await login(email, password));
                } catch (caught) {
                  setError(caught instanceof ApiError ? caught.message : t("Sign in failed."));
                } finally {
                  setBusy(false);
                }
              }}
              className="flex flex-col gap-5"
            >
              <div className="flex flex-col gap-2">
                <Label htmlFor="email" className="text-sm text-foreground/80">{t("Work email")}</Label>
                <div className="relative">
                  <Mail
                    aria-hidden="true"
                    className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                  />
                  <Input
                    id="email"
                    name="email"
                    type="email"
                    autoComplete="email"
                    required
                    placeholder="you@company.com"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    className={`${FIELD} pl-10`}
                  />
                </div>
              </div>

              <div className="flex flex-col gap-2">
                <Label htmlFor="password" className="text-sm text-foreground/80">{t("Password")}</Label>
                <div className="relative">
                  <Lock
                    aria-hidden="true"
                    className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                  />
                  <Input
                    id="password"
                    name="password"
                    type={visible ? "text" : "password"}
                    autoComplete="current-password"
                    required
                    placeholder="••••••••••••"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    className={`${FIELD} pr-11 pl-10`}
                  />
                  <button
                    type="button"
                    onClick={() => setVisible((shown) => !shown)}
                    aria-label={visible ? t("Hide password") : t("Show password")}
                    aria-pressed={visible}
                    className="absolute top-1/2 right-2 -translate-y-1/2 rounded-md p-1.5 text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:outline-none"
                  >
                    {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>

              {error && (
                <p
                  role="alert"
                  className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                >
                  {error}
                </p>
              )}

              <Button
                type="submit"
                disabled={busy}
                // The gradient was two hardcoded indigos from before the palette moved, so
                // this button kept the old accent after everything around it changed. Built
                // from `--primary` now, which is the point of having the token.
                style={{
                  background:
                    "linear-gradient(to right, var(--primary), color-mix(in oklab, var(--primary) 80%, black))",
                }}
                className="group mt-1 h-12 w-full rounded-xl text-sm font-semibold tracking-wide text-white shadow-[0_8px_24px_-6px_color-mix(in_oklab,var(--primary)_70%,transparent)] ring-1 ring-white/15 ring-inset transition-all hover:-translate-y-0.5 hover:brightness-110 active:translate-y-0 disabled:translate-y-0 disabled:opacity-60"
              >
                {busy ? t("Signing in…") : t("Sign In to Workspace")}
                <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" />
              </Button>
            </form>

            {/* Both claims are true of this build: migration 0001 puts tenant isolation in
                Postgres policies rather than in application code. */}
            <div className="mt-7 flex justify-center border-t border-white/5 pt-6">
              <div className="inline-flex items-center gap-2 rounded-full border border-input bg-secondary/50 px-3 py-1.5 font-mono text-[11px] tracking-tight text-muted-foreground">
                <ShieldCheck className="size-3.5 text-zenith-cyan" />
                <span className="flex items-center gap-1.5">
                  <span className="size-1.5 rounded-full bg-zenith-cyan shadow-[0_0_6px_rgba(0,229,229,0.9)]" />{t("RLS Security Enforced")}</span>
                <span className="text-white/15">|</span>
                <span>{t("Multi-Tenant Isolated")}</span>
              </div>
            </div>
          </div>

          <p className="mt-6 text-center text-xs text-muted-foreground">{t("No account? Your workspace administrator creates one for you.")}</p>
        </div>
      </div>
    </div>
  );
}
