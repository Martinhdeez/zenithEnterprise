/**
 * What the upload pool is allowed to wait for.
 *
 * `CONCURRENCY` bounds the number of files whose *bytes* are in flight, because that is what
 * competes for the API's connection pool. It was accidentally bounding something else as
 * well: the worker awaited the ingestion watch too, so a slot stayed occupied for as long as
 * the server took to parse, chunk and embed the document — up to the watch's two-minute
 * limit. A twenty-file batch therefore advanced three files at a time and looked frozen
 * between them, which is the opposite of what bounded concurrency was added for.
 *
 * The watch is mocked here to a promise that never resolves. That is the worst case the old
 * code had to survive and did not: with it, the batch must still upload every file.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", () => ({
  uploadDocument: vi.fn(),
  getDocument: vi.fn(),
}));

vi.mock("@/features/labels", () => ({
  labels: vi.fn(async () => []),
  LabelPicker: () => null,
}));

vi.mock("./uploadWatch", () => ({
  untilSettled: vi.fn(),
  phaseFor: vi.fn(() => ({ phase: "done" as const })),
}));

const { Upload } = await import("./Upload");
const { uploadDocument } = await import("../api");
const { untilSettled } = await import("./uploadWatch");

const uploads = vi.mocked(uploadDocument);
const watches = vi.mocked(untilSettled);

const pdf = (name: string) => new File(["%PDF-1.4"], name, { type: "application/pdf" });

const document_ = (id: string) => ({
  id,
  filename: `${id}.pdf`,
  description: null,
  sha256: id,
  status: "pending",
  status_detail: null,
  page_count: null,
  size_bytes: 8,
  uploaded_by: null,
  created_at: "2026-08-25T00:00:00Z",
  label_ids: [],
});

beforeEach(() => {
  uploads.mockReset();
  watches.mockReset();
  uploads.mockImplementation(async (file) => ({
    document: document_(file.name),
    labels: [],
    deduplicated: false,
  }));
});

/** Drop `count` files on the input and confirm the staging table. */
async function drop(count: number) {
  const files = Array.from({ length: count }, (_, index) => pdf(`f${index}.pdf`));
  const input = screen.getByLabelText("Upload PDFs");
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  const confirm = await screen.findByRole("button", { name: /Confirm & process/ });
  await act(async () => {
    fireEvent.click(confirm);
  });
}

async function sendBatch(count: number) {
  render(<Upload token="t" onUploaded={() => {}} />);
  await drop(count);
}

describe("a batch larger than the concurrency limit", () => {
  it("uploads every file even while no ingestion ever finishes", async () => {
    // Never resolves: every document stays mid-ingestion forever.
    watches.mockImplementation(() => new Promise(() => {}));

    await sendBatch(8);

    // The assertion that fails on the old code. Awaiting the watch inside the pool capped
    // this at CONCURRENCY (3) until the two-minute limit expired on each one.
    await waitFor(() => expect(uploads).toHaveBeenCalledTimes(8), { timeout: 3000 });
  });

  it("starts a watch for each file it uploaded", async () => {
    watches.mockImplementation(() => new Promise(() => {}));

    await sendBatch(8);

    await waitFor(() => expect(watches).toHaveBeenCalledTimes(8), { timeout: 3000 });
  });

  it("refreshes the library once the bytes are in, without waiting for ingestion", async () => {
    // Folder counts and the document list should be right within seconds of the transfer.
    // Tying them to the watches meant they trailed ingestion by up to two minutes per file.
    watches.mockImplementation(() => new Promise(() => {}));
    const onUploaded = vi.fn();
    render(<Upload token="t" onUploaded={onUploaded} />);
    await drop(4);

    await waitFor(() => expect(onUploaded).toHaveBeenCalled(), { timeout: 3000 });
  });
});

describe("a file the server rejects", () => {
  it("does not stop the rest of the batch", async () => {
    // One duplicate in two hundred must not abandon the other hundred and ninety-nine.
    watches.mockImplementation(() => new Promise(() => {}));
    uploads.mockImplementationOnce(async () => {
      throw new Error("already exists");
    });

    await sendBatch(5);

    await waitFor(() => expect(uploads).toHaveBeenCalledTimes(5), { timeout: 3000 });
  });
});
