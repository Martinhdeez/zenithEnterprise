/**
 * The staging area's arithmetic: what a drop adds, what a subset selection covers, and what
 * a bulk tag changes.
 *
 * The behaviours here are the ones that make a thousand-file migration possible at all —
 * uploading on drop means a thousand documents land under the default label before anybody
 * has said what any of them are, and unpicking that afterwards means editing a thousand
 * documents.
 */

import { describe, expect, it } from "vitest";

import {
  addToSelected,
  range,
  removeSelected,
  stage,
  summarise,
  tagSelected,
  type StagedFile,
} from "./stagingState";

const file = (name: string, size = 100) => new File(["x".repeat(size)], name);
const rows = (...names: string[]) => stage(names.map((name) => file(name)));
const ids = (staged: StagedFile[]) => staged.map((row) => row.id);

describe("staging a drop", () => {
  it("adds a row per file and files nothing", () => {
    const staged = rows("a.pdf", "b.pdf");

    expect(staged).toHaveLength(2);
    expect(staged.every((row) => row.labelIds.length === 0)).toBe(true);
  });

  it("appends to what is already staged", () => {
    // Dropping a second folder must not discard the first, and the tags already assigned
    // to it must survive.
    const staged = rows("a.pdf");
    const first = tagSelected(staged, new Set(ids(staged)), ["l1"]);
    const both = stage([file("b.pdf")], first);

    expect(both).toHaveLength(2);
    expect(both[0]?.labelIds).toEqual(["l1"]);
  });

  it("keeps a genuine duplicate as its own row", () => {
    // Dropping the same folder twice is ordinary. Collapsing the second into the first
    // would look like files went missing.
    const staged = stage([file("same.pdf")], rows("same.pdf"));

    expect(staged).toHaveLength(2);
    expect(staged[0]?.id).not.toBe(staged[1]?.id);
  });
});

describe("tagging a subset", () => {
  it("changes only the selected rows", () => {
    const staged = rows("a.pdf", "b.pdf", "c.pdf");
    const selected = new Set([staged[0]!.id, staged[2]!.id]);

    const tagged = tagSelected(staged, selected, ["invoices"]);

    expect(tagged[0]?.labelIds).toEqual(["invoices"]);
    expect(tagged[1]?.labelIds).toEqual([]);
    expect(tagged[2]?.labelIds).toEqual(["invoices"]);
  });

  it("replaces rather than accumulates, so a mistake can be corrected", () => {
    const staged = rows("a.pdf");
    const selected = new Set(ids(staged));

    const corrected = tagSelected(tagSelected(staged, selected, ["wrong"]), selected, ["right"]);

    expect(corrected[0]?.labelIds).toEqual(["right"]);
  });

  it("can add without discarding, for the second pass over a subset", () => {
    const initial = rows("a.pdf");
    const staged = tagSelected(initial, new Set(ids(initial)), ["finance"]);
    const selected = new Set(ids(staged));

    const both = addToSelected(staged, selected, ["2026"]);

    expect(both[0]?.labelIds).toEqual(["finance", "2026"]);
  });

  it("does not duplicate a label already on the row", () => {
    const staged = rows("a.pdf");
    const selected = new Set(ids(staged));

    const twice = addToSelected(addToSelected(staged, selected, ["x"]), selected, ["x"]);

    expect(twice[0]?.labelIds).toEqual(["x"]);
  });

  it("leaves the rows it was given alone", () => {
    const staged = rows("a.pdf");

    tagSelected(staged, new Set(ids(staged)), ["x"]);

    expect(staged[0]?.labelIds).toEqual([]);
  });
});

describe("selecting a range", () => {
  it("covers everything between the anchor and the target", () => {
    const staged = rows("1", "2", "3", "4", "5");

    const covered = range(staged, staged[1]!.id, staged[3]!.id);

    expect(covered).toEqual([staged[1]!.id, staged[2]!.id, staged[3]!.id]);
  });

  it("works upwards as well as downwards", () => {
    // Every file manager does this, and a range that only runs one way is forty clicks for
    // the forty invoices this screen exists to handle.
    const staged = rows("1", "2", "3", "4");

    expect(range(staged, staged[3]!.id, staged[1]!.id)).toEqual([
      staged[1]!.id,
      staged[2]!.id,
      staged[3]!.id,
    ]);
  });

  it("falls back to the clicked row when there is no anchor yet", () => {
    const staged = rows("1", "2");

    expect(range(staged, "not-a-row", staged[1]!.id)).toEqual([staged[1]!.id]);
  });
});

describe("removing", () => {
  it("drops the selected rows and keeps the rest", () => {
    const staged = rows("a.pdf", "b.pdf");

    expect(removeSelected(staged, new Set([staged[0]!.id]))).toHaveLength(1);
  });
});

describe("the summary", () => {
  it("counts everything as unclassified before anybody tags", () => {
    // The number that tells somebody whether they are done: untagged files are the ones
    // the server will file under the tenant's default label.
    expect(summarise(rows("a.pdf", "b.pdf", "c.pdf"))).toMatchObject({ total: 3, untagged: 3 });
  });

  it("counts tagged rows once they carry a label", () => {
    const staged = rows("a.pdf", "b.pdf");
    const tagged = tagSelected(staged, new Set([staged[0]!.id]), ["x"]);

    expect(summarise(tagged)).toMatchObject({ total: 2, tagged: 1, untagged: 1 });
  });

  it("adds up the bytes about to be sent", () => {
    expect(summarise(stage([file("a", 500), file("b", 300)])).bytes).toBe(800);
  });
});
