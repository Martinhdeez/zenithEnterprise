/**
 * What a document says about itself when it finished ingesting and something still went wrong.
 *
 * `status_detail` was rendered only for `failed`. Everything the pipeline wrote about a
 * *successful* ingestion went to the database and was shown to nobody — "3 of 40 page(s)
 * extracted with warnings" since it was written, and since 25 August "2 of 4 page(s) are not
 * searchable" on any document with a scanned page in it.
 *
 * The reader who needs that sentence is looking at this panel wondering why an answer did not
 * come from this document.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/features/labels", () => ({ TagChips: () => null, labels: vi.fn(async () => []) }));

// The panel fetches passage and citation counts on mount; neither is what these test.
vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return { ...actual, documentInsights: vi.fn(async () => ({ chunks: 0, citations: 0 })) };
});

const { DocumentDetail } = await import("./DocumentDetail");

const document_ = (overrides: Record<string, unknown> = {}) => ({
  id: "d1",
  filename: "contract.pdf",
  description: null,
  sha256: "abc",
  status: "ready",
  status_detail: null,
  page_count: 4,
  size_bytes: 1024,
  uploaded_by: null,
  created_at: "2026-08-25T00:00:00Z",
  label_ids: [],
  ...overrides,
});

const show = (overrides: Record<string, unknown> = {}) =>
  render(
    <DocumentDetail token="t" document={document_(overrides) as never} labelNames={[]} />,
  );

describe("a document that finished with something to report", () => {
  it("shows what the pipeline said about it", async () => {
    show({ status_detail: "2 of 4 page(s) are not searchable (page 2, 4): no OCR" });

    expect(
      await screen.findByText(/2 of 4 page\(s\) are not searchable/),
    ).toBeTruthy();
  });

  it("does not present it as a failure", async () => {
    // The document *is* searchable and most of it is in the index. Colouring this as a
    // failure would send somebody re-uploading work that is already done.
    show({ status_detail: "2 of 4 page(s) are not searchable (page 2, 4): no OCR" });

    expect(screen.queryByText(/Ingestion failed/)).toBeNull();
    expect(screen.queryByText(/not searchable\. *$/)).toBeNull();
  });

  it("says nothing when there is nothing to say", async () => {
    show({ status_detail: null });

    expect(screen.queryByText(/page\(s\)/)).toBeNull();
  });
});

describe("a document that failed", () => {
  it("says so, and says why", async () => {
    show({ status: "failed", status_detail: "no extractable text layer" });

    expect(await screen.findByText(/Ingestion failed: no extractable text layer/)).toBeTruthy();
  });

  it("does not also show the caution, which would say it twice", async () => {
    show({ status: "failed", status_detail: "no extractable text layer" });

    expect(screen.getAllByText(/no extractable text layer/)).toHaveLength(1);
  });
});
