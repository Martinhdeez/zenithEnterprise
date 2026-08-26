/**
 * The operator console's grouping, without the screen.
 *
 * A destroyed trial and an empty eval run used to sit as peers of the live customer,
 * because the API returns creation order and the list rendered that. These tests pin
 * the two decisions that stop that: lifecycle groups, and weight inside a group.
 */

import { describe, expect, it } from "vitest";

import type { Organisation } from "./api";
import { groupOrganisations } from "./groupOrganisations";

function org(partial: Partial<Organisation> & Pick<Organisation, "id" | "name" | "status">): Organisation {
  return {
    created_at: "2026-01-01T00:00:00Z",
    status_changed_at: null,
    users: 0,
    documents: 0,
    storage_bytes: 0,
    ...partial,
  };
}

describe("groupOrganisations", () => {
  it("puts the organisation with the corpus first among the live ones", () => {
    const empty = org({ id: "a", name: "M0 baseline a", status: "active" });
    const real = org({
      id: "b",
      name: "M0 baseline b",
      status: "active",
      users: 4,
      documents: 26,
    });
    const alsoEmpty = org({ id: "c", name: "M0 baseline c", status: "active" });

    const grouped = groupOrganisations([empty, alsoEmpty, real]);

    expect(grouped.active.map((item) => item.id)).toEqual(["b", "a", "c"]);
  });

  it("keeps destroyed organisations out of the live list", () => {
    const live = org({ id: "live", name: "Acme SA", status: "active", users: 4, documents: 26 });
    const gone = org({ id: "gone", name: "Prueba Ciclo", status: "purged" });

    const grouped = groupOrganisations([gone, live]);

    expect(grouped.active).toEqual([live]);
    expect(grouped.destroyed).toEqual([gone]);
    expect(grouped.suspended).toEqual([]);
  });

  it("keeps a purging organisation with the live ones", () => {
    const purging = org({ id: "p", name: "Leaving", status: "purging", users: 2, documents: 10 });

    expect(groupOrganisations([purging]).active).toEqual([purging]);
  });
});
