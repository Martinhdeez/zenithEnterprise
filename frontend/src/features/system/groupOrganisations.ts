/**
 * How the operator console groups organisations.
 *
 * The API returns every tenant in creation order, including tombstones. That is the
 * complete picture, and it is the wrong default for a screen: an empty measurement run
 * and a destroyed trial then sit as peers of the live customer. Grouping by lifecycle —
 * what you operate, what you paused, what is gone — is the same distinction the status
 * column already makes; the list was just not using it.
 *
 * Inside a group, the organisation with the most behind it comes first. A buyer opening
 * this panel should land on the customer, not on whichever eval run happened to be
 * created first.
 */

import type { Organisation } from "./api";

export interface GroupedOrganisations {
  active: Organisation[];
  suspended: Organisation[];
  destroyed: Organisation[];
}

export function groupOrganisations(items: Organisation[]): GroupedOrganisations {
  return {
    active: byWeight(items.filter((item) => item.status === "active" || item.status === "purging")),
    suspended: byWeight(items.filter((item) => item.status === "suspended")),
    destroyed: items.filter((item) => item.status === "purged"),
  };
}

function byWeight(items: Organisation[]): Organisation[] {
  return [...items].sort(
    (left, right) =>
      right.documents - left.documents ||
      right.users - left.users ||
      left.name.localeCompare(right.name),
  );
}
