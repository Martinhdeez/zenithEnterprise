/**
 * The one number that changes while somebody is looking at another screen.
 *
 * A migration is confirmed on the upload screen and then takes minutes to hours. The queue
 * there stops answering the moment you navigate away, which is how the same folder gets
 * uploaded twice or a tab gets closed on work that had not finished.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Ingesting, inFlight, ready } from "./Ingesting";
import type { TenantStatus } from "@/shared/api/tenant";

const status = (documents: Record<string, number>): TenantStatus =>
  ({
    documents,
    chunks: 0,
    hardware: "low-spec",
    searchable: true,
    components: { embeddings: true, reranker: false, generation: true },
  }) as TenantStatus;

describe("counting what is in flight", () => {
  it("sums the statuses the schema actually defines", () => {
    expect(inFlight(status({ pending: 2, parsing: 1, embedding: 3, ready: 90 }))).toBe(6);
  });

  it("ignores an invented status rather than counting it", () => {
    // The mistake `StatusBadge` made once: a `processing` key that never exists, summed
    // forever to zero, so the indicator never appeared and nothing said why.
    expect(inFlight(status({ processing: 5, ready: 1 }))).toBe(0);
  });

  it("is zero before the first status has arrived", () => {
    expect(inFlight(null)).toBe(0);
    expect(ready(null)).toBe(0);
  });
});

describe("when something is being ingested", () => {
  it("says how many, against the corpus rather than a batch", () => {
    // The browser does not know how many documents the *server* has queued — another tab,
    // another person, a requeue from the CLI — so a bar measured against one batch would
    // sit still while six hundred other documents went through it.
    render(<Ingesting status={status({ embedding: 4, ready: 96 })} collapsed={false} />);

    expect(screen.getByText("Ingesting 4 of 100")).toBeTruthy();
  });

  it("warns that search is slower, where somebody will see it", () => {
    render(<Ingesting status={status({ parsing: 1 })} collapsed={false} />);

    expect(screen.getByText(/Searches run more slowly/)).toBeTruthy();
  });
});

describe("when nothing is being ingested", () => {
  it("says so instead of disappearing", () => {
    // An indicator that vanishes when idle cannot be told apart from one that is broken,
    // and "did my upload finish, or did the widget die" is the question it exists for.
    render(<Ingesting status={status({ ready: 12 })} collapsed={false} />);

    expect(screen.getByText("Nothing being ingested")).toBeTruthy();
  });
});

describe("in a collapsed sidebar", () => {
  it("still shows the count, because that is the only question it answers", () => {
    render(<Ingesting status={status({ embedding: 7 })} collapsed />);

    expect(screen.getByText("7")).toBeTruthy();
  });

  it("drops the prose rather than the indicator", () => {
    // 64 pixels has no honest rendering of a sentence, but it has room for a dot and a
    // number — and this is the one thing worth seeing from a narrow sidebar.
    render(<Ingesting status={status({ embedding: 7 })} collapsed />);

    expect(screen.queryByText(/Searches run more slowly/)).toBeNull();
    expect(screen.getByTitle("Ingesting 7 documents")).toBeTruthy();
  });
});
