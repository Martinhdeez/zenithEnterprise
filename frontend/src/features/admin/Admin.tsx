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

import { TagManager, labels as fetchLabels, type Label as LabelType } from "@/features/labels";
import { AccessMatrix, GroupManager } from "./access/AccessMatrix";
import { Analytics } from "./analytics/Analytics";
import { AuditTrail } from "./audit/AuditTrail";
import { UserGroups } from "./groups/UserGroups";
import { InvitePanel } from "./invite/InvitePanel";
import { LlmPanel } from "./model/LlmPanel";
import { RolePanel } from "./roles/RolePanel";
import { useT } from "@/shared/i18n/useT";

/** One administration panel: a title, a hairline rule beneath it, generous padding around
    the content. Every panel on this screen shares this frame so the eye reads them as one
    settings surface rather than three differently-built widgets stacked on a page.

    `bg-secondary`, not `bg-card`, and that is the whole reason this screen had no depth:
    the page's own `<main>` is already `bg-card`, so a panel painted `bg-card` is the same
    navy as the page behind it and only its hairline border says it exists. The palette has
    three steps of blue — `#070a12` behind the app, `#10172a` for the page, `#182238` for a
    thing *on* the page — and a panel is the third of those. The header keeps `bg-card` so
    it recedes below its own body, the way a title bar sits below a window. */
function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-secondary shadow-sm">
      <h2 className="rounded-t-lg border-b border-border bg-card px-5 py-3.5 text-sm font-semibold text-foreground">
        {title}
      </h2>
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Admin({ token }: { token: string }) {
  const t = useT();
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
      {/* First, because it is the only panel that answers a question rather than setting
          something — an administrator opening this screen usually wants to know what is
          happening before changing anything. */}
      <Panel title={t("Analytics")}>
        <Analytics token={token} />
      </Panel>
      {/* Directly under the analytics it is easily confused with, and named for what it
          actually holds. The panel above records questions asked; this one records changes
          to who may ask them of what. */}
      <Panel title={t("Access record")}>
        <AuditTrail token={token} />
      </Panel>
      <Panel title={t("Invite a colleague")}>
        <InvitePanel token={token} />
      </Panel>
      <Panel title={t("Groups")}>
        <GroupManager token={token} onChanged={reloadLabels} />
      </Panel>
      <Panel title={t("People and groups")}>
        <UserGroups token={token} />
      </Panel>
      <Panel title={t("Access matrix")}>
        <AccessMatrix token={token} labels={labels} onLabelsChanged={reloadLabels} />
      </Panel>
      <Panel title={t("Roles")}>
        <RolePanel token={token} />
      </Panel>
      <Panel title={t("Labels")}>
        <TagManager token={token} />
      </Panel>
      <Panel title={t("Answer model")}>
        <LlmPanel token={token} />
      </Panel>
    </div>
  );
}
