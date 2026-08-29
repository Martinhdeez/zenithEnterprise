/**
 * The naming convention, and the line it must not cross.
 *
 * `parent/child` is a display convention over a flat table — there is no parent column and
 * the RLS policy is a single `&&` against `documents.label_ids`. These tests hold the
 * parsing, and the last of them holds the part that is not cosmetic: a shared prefix is
 * not a grant, and a namespace filter can never resolve to a label the caller does not
 * already reach.
 */

import { describe, expect, it } from "vitest";

import { descendants, leaf, parentPath, segments, tree, within } from "./namespace";

const named = (name: string) => ({ id: name, name });
const nameOf = (label: { name: string }) => label.name;

describe("reading a path", () => {
  it("splits on the separator", () => {
    expect(segments("finance/2026/invoices")).toEqual(["finance", "2026", "invoices"]);
  });

  it("survives stray and trailing separators", () => {
    // Somebody will type this. A nameless level would render as an empty row in the tree.
    expect(segments("legal//contracts/")).toEqual(["legal", "contracts"]);
  });

  it("treats a name without a separator as a single level", () => {
    expect(segments("Finance")).toEqual(["Finance"]);
    expect(parentPath("Finance")).toBe("");
    expect(leaf("Finance")).toBe("Finance");
  });

  it("names the namespace and the leaf", () => {
    expect(parentPath("finance/2026/invoices")).toBe("finance/2026");
    expect(leaf("finance/2026/invoices")).toBe("invoices");
  });
});

describe("building the tree", () => {
  it("nests three levels under one root", () => {
    const [root] = tree([named("a/b/c")], nameOf);

    expect(root?.name).toBe("a");
    expect(root?.children[0]?.name).toBe("b");
    expect(root?.children[0]?.children[0]?.path).toBe("a/b/c");
  });

  it("marks implied levels as having no label of their own", () => {
    // `a` and `a/b` are not rows anybody can file a document under, and the tree must not
    // offer them as if they were.
    const [root] = tree([named("a/b/c")], nameOf);

    expect(root?.value).toBeNull();
    expect(root?.children[0]?.value).toBeNull();
    expect(root?.children[0]?.children[0]?.value).toEqual(named("a/b/c"));
  });

  it("keeps a real label at an intermediate level", () => {
    const [root] = tree([named("legal"), named("legal/contracts")], nameOf);

    expect(root?.value).toEqual(named("legal"));
    expect(root?.children).toHaveLength(1);
  });

  it("groups siblings under one parent rather than repeating it", () => {
    const roots = tree([named("legal/contracts"), named("legal/policies")], nameOf);

    expect(roots).toHaveLength(1);
    expect(roots[0]?.children.map((child) => child.name)).toEqual(["contracts", "policies"]);
  });

  it("orders every level, so the same labels always render the same way", () => {
    const roots = tree([named("b/z"), named("a"), named("b/a")], nameOf);

    expect(roots.map((node) => node.name)).toEqual(["a", "b"]);
    expect(roots[1]?.children.map((node) => node.name)).toEqual(["a", "z"]);
  });
});

describe("what belongs to a namespace", () => {
  it("includes the namespace itself and everything under it", () => {
    expect(within("legal", "legal")).toBe(true);
    expect(within("legal/contracts", "legal")).toBe(true);
    expect(within("legal/contracts/2026", "legal")).toBe(true);
  });

  it("matches whole segments, not characters", () => {
    // The assertion that stops `legal/*` sweeping up an unrelated label. Filtering on it
    // would show documents the user did not ask for, under a heading claiming otherwise.
    expect(within("legal-hold", "legal")).toBe(false);
    expect(within("legalese", "legal")).toBe(false);
  });

  it("does not treat a child namespace as its parent", () => {
    expect(within("legal", "legal/contracts")).toBe(false);
  });

  it("collects the labels a namespace filter resolves to", () => {
    const labels = [named("legal"), named("legal/contracts"), named("legal-hold"), named("tax")];

    expect(descendants(labels, "legal", nameOf).map(nameOf)).toEqual(["legal", "legal/contracts"]);
  });

  it("resolves only to labels that were passed in", () => {
    // The line this convention must not cross. A `legal/secret` held by another role is
    // not something this can know or include: the caller passes the labels it reaches, so
    // a namespace filter narrows that set and can never widen it.
    const reachable = [named("legal/contracts")];

    expect(descendants(reachable, "legal", nameOf).map(nameOf)).toEqual(["legal/contracts"]);
  });
});
