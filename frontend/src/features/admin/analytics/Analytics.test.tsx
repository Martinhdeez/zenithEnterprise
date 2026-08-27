/**
 * The audit log's paging, and the two ways it would quietly mislead.
 *
 * Fifty rows pushed every other admin panel below the fold, so reaching the access matrix
 * meant scrolling past a wall of questions nobody had asked to read. Ten rows and a cursor
 * fixes that — provided pages accumulate rather than replace, and provided the button
 * disappears when there is nothing left.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Analytics } from "./Analytics";

const TOTALS = {
  queries: 24,
  users: 2,
  prompt_tokens: 2610,
  completion_tokens: 109,
  queries_without_usage: 3,
  average_retrieval_ms: 300,
  average_generation_ms: 6200,
  abstentions: 12,
};

const entry = (id: string, question: string) => ({
  query_id: id,
  asked_at: "2026-08-08T10:00:00Z",
  email: "ana@example.com",
  question,
  abstained: false,
  model: "gemini-3.6-flash",
  latency_ms: 6500,
  documents: ["gdpr.pdf"],
});

const analytics = vi.fn();
const auditLog = vi.fn();

vi.mock("../api", () => ({
  analytics: (...a: unknown[]) => analytics(...a),
  auditLog: (...a: unknown[]) => auditLog(...a),
}));

beforeEach(() => {
  vi.clearAllMocks();
  analytics.mockResolvedValue({
    window_days: 30,
    totals: TOTALS,
    most_active: [{ user_id: "u1", email: "ana@example.com", queries: 24 }],
    top_cited: [{ document_id: "d1", filename: "gdpr.pdf", answers: 9 }],
  });
  auditLog.mockResolvedValue({
    entries: [entry("q1", "first question")],
    next_cursor: null,
  });
});

describe("the aggregates", () => {
  it("are fetched once, separately from the log", async () => {
    // A page turn cannot change a single number up there, so re-running four aggregates to
    // fetch ten log rows is work nobody asked for.
    render(<Analytics token="t" />);

    await screen.findByText("first question");

    expect(analytics).toHaveBeenCalledTimes(1);
  });

  it("show abstentions as a share, not just a count", async () => {
    render(<Analytics token="t" />);

    expect(await screen.findByText("50% of questions")).toBeTruthy();
  });

  it("keep unreported cost out of the total and say so", async () => {
    // "Nothing was spent" and "nobody reported what was spent" are different answers.
    render(<Analytics token="t" />);

    expect(await screen.findByText(/3 questions reported no usage/)).toBeTruthy();
  });
});

describe("the audit log", () => {
  it("asks for the first page with no cursor", async () => {
    render(<Analytics token="t" />);

    await waitFor(() => expect(auditLog).toHaveBeenCalledWith("t", null));
  });

  it("names the documents each answer read", async () => {
    // The column an auditor reads. It is not derivable from the answer text, which is why
    // `query_citations` exists at all.
    render(<Analytics token="t" />);

    expect(await screen.findByText("gdpr.pdf")).toBeTruthy();
  });

  it("offers no button when the first page is the last", async () => {
    render(<Analytics token="t" />);
    await screen.findByText("first question");

    expect(screen.queryByRole("button", { name: /Cargar más/ })).toBeNull();
  });
});

describe("loading more", () => {
  beforeEach(() => {
    auditLog.mockImplementation((_token: string, cursor: string | null) =>
      cursor === null
        ? Promise.resolve({ entries: [entry("q1", "first question")], next_cursor: "CURSOR" })
        : Promise.resolve({ entries: [entry("q2", "second question")], next_cursor: null }),
    );
  });

  it("passes the cursor back rather than an offset", async () => {
    render(<Analytics token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: /Cargar más/ }));

    await waitFor(() => expect(auditLog).toHaveBeenCalledWith("t", "CURSOR"));
  });

  it("keeps the rows already read", async () => {
    // Replacing them would discard pages somebody has already scrolled through — and a
    // keyset cursor names a position forward only, so there is no going back for them.
    render(<Analytics token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: /Cargar más/ }));

    expect(await screen.findByText("second question")).toBeTruthy();
    expect(screen.getByText("first question")).toBeTruthy();
  });

  it("stops offering the button at the end", async () => {
    render(<Analytics token="t" />);
    fireEvent.click(await screen.findByRole("button", { name: /Cargar más/ }));
    await screen.findByText("second question");

    expect(screen.queryByRole("button", { name: /Cargar más/ })).toBeNull();
  });
});

describe("when the log fails", () => {
  it("says so without taking the aggregates down with it", async () => {
    auditLog.mockRejectedValue(new Error("this cursor is not one we issued"));
    render(<Analytics token="t" />);

    expect(await screen.findByText("this cursor is not one we issued")).toBeTruthy();
    // The KPIs came from a different request and are still good.
    expect(screen.getByText("50% of questions")).toBeTruthy();
  });
});
