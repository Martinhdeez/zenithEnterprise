/**
 * Every organisation in the installation, and the two things you can do to one.
 *
 * The screen is deliberately plain. It is the only place in the product where a single
 * click can end a customer, so the design job here is legibility rather than polish: what
 * state each organisation is in, how much is behind it, and what the action about to be
 * taken actually means.
 *
 * **Suspend and purge are drawn differently on purpose.** Suspend is reversible and reads
 * as an ordinary control. Purge is destructive and irreversible, so it is the only red
 * thing on the page, it is unavailable until the organisation is already suspended, and it
 * opens a dialogue that will not proceed until the name has been typed out. All three of
 * those are re-checked on the server — this file makes the intent obvious, it does not
 * enforce it.
 *
 * **Destroyed organisations are listed, and not as peers of the live ones.** A tombstone
 * is the record that a purge happened; hiding it would make the irreversible act look as
 * if it had never. Folding it away is what keeps that record from being the first thing
 * a buyer sees.
 */

import { useCallback, useEffect, useState } from "react";
import { Building2, Loader2, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  type Organisation,
  activateOrganisation,
  organisations as fetchOrganisations,
  provisionOrganisation,
  purgeOrganisation,
  suspendOrganisation,
} from "./api";
import { groupOrganisations } from "./groupOrganisations";
import { useFormat, useT, type T } from "@/shared/i18n/useT";

const STATUS_STYLE: Record<string, string> = {
  active: "bg-zenith-cyan/10 text-zenith-cyan",
  suspended: "bg-zenith-amber/10 text-zenith-amber",
  purging: "bg-destructive/10 text-destructive animate-pulse",
  purged: "bg-secondary text-muted-foreground",
};

function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${Math.round(bytes / 1_000)} kB`;
  return `${bytes} B`;
}

export function System({ token }: { token: string }) {
  const t = useT();
  const [items, setItems] = useState<Organisation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [purging, setPurging] = useState<Organisation | null>(null);

  const load = useCallback(() => {
    void fetchOrganisations(token)
      .then(setItems)
      .catch((problem: Error) => setError(problem.message))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(load, [load]);

  async function act(id: string, work: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await work();
      // Re-read rather than patching the row locally: the server owns the lifecycle, and
      // a screen that reports a state it decided for itself can be wrong about whether a
      // customer is switched off.
      load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : t("That change was not saved."));
    } finally {
      setBusy(null);
    }
  }

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> {t("Loading organisations…")}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <NewOrganisation token={token} onCreated={load} />

      {error && (
        <p role="alert" className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          <ShieldAlert className="mt-0.5 size-4 shrink-0" />
          {error}
        </p>
      )}

      <OrganisationList
        token={token}
        items={items}
        busy={busy}
        onAct={act}
        onPurge={setPurging}
      />

      {items.length === 0 && (
        <p className="rounded-md border border-input bg-input/60 p-4 text-sm text-muted-foreground">
          {t("No organisations yet.")}
        </p>
      )}

      {purging && (
        <PurgeDialog
          organisation={purging}
          onCancel={() => setPurging(null)}
          onConfirm={async (name) => {
            await act(purging.id, () => purgeOrganisation(token, purging.id, name));
            setPurging(null);
          }}
        />
      )}
    </div>
  );
}

function OrganisationList({
  token,
  items,
  busy,
  onAct,
  onPurge,
}: {
  token: string;
  items: Organisation[];
  busy: string | null;
  onAct: (id: string, work: () => Promise<unknown>) => Promise<void>;
  onPurge: (organisation: Organisation) => void;
}) {
  const t = useT();
  const { active, suspended, destroyed } = groupOrganisations(items);
  const grouped = suspended.length > 0 || destroyed.length > 0;

  return (
    <div className="space-y-6">
      {active.length > 0 && (
        <OrganisationGroup
          title={grouped ? t("Active") : undefined}
          organisations={active}
          token={token}
          busy={busy}
          onAct={onAct}
          onPurge={onPurge}
        />
      )}
      {suspended.length > 0 && (
        <OrganisationGroup
          title={t("Suspended")}
          organisations={suspended}
          token={token}
          busy={busy}
          onAct={onAct}
          onPurge={onPurge}
        />
      )}
      {destroyed.length > 0 && (
        <details className="space-y-2">
          <summary className="cursor-pointer text-sm font-medium text-muted-foreground">
            {t("Destroyed organisations ({count})", { count: destroyed.length })}
          </summary>
          <OrganisationGroup
            organisations={destroyed}
            token={token}
            busy={busy}
            onAct={onAct}
            onPurge={onPurge}
          />
        </details>
      )}
    </div>
  );
}

function OrganisationGroup({
  title,
  organisations,
  token,
  busy,
  onAct,
  onPurge,
}: {
  title?: string;
  organisations: Organisation[];
  token: string;
  busy: string | null;
  onAct: (id: string, work: () => Promise<unknown>) => Promise<void>;
  onPurge: (organisation: Organisation) => void;
}) {
  return (
    <section className="space-y-2">
      {title && <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>}
      {/* `divide-input`, not `divide-border`, and two pixels rather than one. `--border`
          (#1e293b) sits almost on top of the `--secondary` panel it divides (#182238), so a
          hairline in it was a line nobody could see; `--input` (#334155) is a real step away
          from both. Each row here is an organisation somebody may be about to suspend, and
          telling one row from the next is not a decorative concern. */}
      <ul className="divide-y-2 divide-input overflow-hidden rounded-lg border border-border bg-secondary shadow-sm">
        {organisations.map((organisation) => (
          <OrganisationRow
            key={organisation.id}
            organisation={organisation}
            token={token}
            busy={busy}
            onAct={onAct}
            onPurge={onPurge}
          />
        ))}
      </ul>
    </section>
  );
}

/**
 * A lifecycle status in the reader's language.
 *
 * A switch of literals rather than `t(organisation.status)`, for the same reason the upload
 * stages are: a key reached through a variable is invisible to the test that guarantees every
 * key has a Spanish sentence, so it would silently render English forever.
 */
function statusWord(status: string, t: T): string {
  switch (status) {
    case "active":
      return t("active");
    case "suspended":
      return t("suspended");
    case "purging":
      return t("purging");
    case "purged":
      return t("purged");
    default:
      return status;
  }
}

function OrganisationRow({
  organisation,
  token,
  busy,
  onAct,
  onPurge,
}: {
  organisation: Organisation;
  token: string;
  busy: string | null;
  onAct: (id: string, work: () => Promise<unknown>) => Promise<void>;
  onPurge: (organisation: Organisation) => void;
}) {
  const t = useT();
  const format = useFormat();
  return (
    <li className="flex flex-wrap items-center gap-3 p-4">
      <Building2 className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-foreground">{organisation.name}</p>
        <p className="text-xs text-muted-foreground">
          {format.date(organisation.created_at)} ·{" "}
          {organisation.users} user{organisation.users === 1 ? "" : "s"} ·{" "}
          {organisation.documents} document{organisation.documents === 1 ? "" : "s"} ·{" "}
          {formatBytes(organisation.storage_bytes)}
        </p>
      </div>

      <span
        className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium capitalize ${
          STATUS_STYLE[organisation.status] ?? "bg-secondary text-muted-foreground"
        }`}
      >
        {statusWord(organisation.status, t)}
      </span>

      {organisation.status === "active" && (
        <Button
          variant="outline"
          size="sm"
          disabled={busy === organisation.id}
          onClick={() => void onAct(organisation.id, () => suspendOrganisation(token, organisation.id))}
        >{t("Suspend")}</Button>
      )}

      {organisation.status === "suspended" && (
        <>
          <Button
            variant="outline"
            size="sm"
            disabled={busy === organisation.id}
            onClick={() => void onAct(organisation.id, () => activateOrganisation(token, organisation.id))}
          >{t("Activate")}</Button>
          {/* The only destructive control in the product, and it is reachable only
              from `suspended` — the reversible step has to have happened first. */}
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            onClick={() => onPurge(organisation)}
          >{t("Purge")}</Button>
        </>
      )}
    </li>
  );
}

function NewOrganisation({ token, onCreated }: { token: string; onCreated: () => void }) {
  const t = useT();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [issued, setIssued] = useState<{ email: string; password: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  return (
    // Raised off the page like every other panel in the product. It was transparent, which
    // on a `bg-card` page means it was the page.
    <section className="space-y-3 rounded-lg border border-border bg-secondary p-4 shadow-sm">
      <p className="text-sm font-medium text-foreground">{t("New organisation")}</p>
      <div className="flex flex-wrap gap-2">
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t("Organisation name")}
          aria-label={t("Organisation name")}
          className="min-w-48 flex-1 rounded-md border border-input bg-input/60 px-3 py-2 text-sm"
        />
        <input
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder={t("First administrator's email")}
          aria-label={t("First administrator's email")}
          className="min-w-56 flex-1 rounded-md border border-input bg-input/60 px-3 py-2 text-sm"
        />
        <Button
          disabled={!name.trim() || !email.trim()}
          onClick={async () => {
            setError(null);
            try {
              const created = await provisionOrganisation(token, {
                name: name.trim(),
                admin_email: email.trim(),
              });
              setIssued({ email: created.admin_email, password: created.password });
              setName("");
              setEmail("");
              onCreated();
            } catch (problem) {
              setError(problem instanceof Error ? problem.message : t("That was not created."));
            }
          }}
        >{t("Create")}</Button>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {issued && (
        // Shown once and never again — there is no outbound mail on an on-premise install,
        // so this is the only moment the credential exists anywhere readable.
        <div className="space-y-1 rounded-md border border-zenith-amber/40 bg-zenith-amber/10 p-3 text-sm">
          <p className="font-medium text-zenith-amber">
            Save this password now — it is not stored and cannot be shown again.
          </p>
          <p className="text-foreground">{issued.email}</p>
          <p className="font-mono font-medium text-foreground">{issued.password}</p>
        </div>
      )}
    </section>
  );
}

function PurgeDialog({
  organisation,
  onCancel,
  onConfirm,
}: {
  organisation: Organisation;
  onCancel: () => void;
  onConfirm: (name: string) => Promise<void>;
}) {
  const t = useT();
  const [typed, setTyped] = useState("");
  const matches = typed === organisation.name;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Purge ${organisation.name}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
    >
      <div className="w-full max-w-md space-y-4 rounded-lg border border-destructive/40 bg-card p-5">
        <p className="text-sm font-medium text-destructive">
          Permanently destroy {organisation.name}?
        </p>
        <p className="text-sm text-muted-foreground">
          {organisation.users} user{organisation.users === 1 ? "" : "s"},{" "}
          {organisation.documents} document{organisation.documents === 1 ? "" : "s"} and every
          passage, embedding and question belonging to them will be deleted, along with their
          files on disk. This cannot be undone.
        </p>
        <div className="space-y-1.5">
          <label htmlFor="confirm-name" className="text-xs text-muted-foreground">
            Type <span className="font-medium text-foreground">{organisation.name}</span> to
            confirm
          </label>
          <input
            id="confirm-name"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            className="w-full rounded-md border border-input bg-input/60 px-3 py-2 text-sm"
          />
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            // Disabled until the name is exact. The server checks it too — this is the
            // affordance, not the guarantee.
            disabled={!matches}
            className="bg-destructive text-white hover:bg-destructive/90"
            onClick={() => void onConfirm(typed)}
          >
            {t("Purge permanently")}
          </Button>
        </div>
      </div>
    </div>
  );
}
