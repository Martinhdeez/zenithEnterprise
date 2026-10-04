/** Repeated successful uploads describe one stored document rather than two new ones. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", () => ({
  uploadDocument: vi.fn(),
  getDocument: vi.fn(),
  suggestLabels: vi.fn(),
  suggestionAvailability: vi.fn(async () => ({ reason: "unavailable" })),
}));

vi.mock("@/features/labels", () => ({
  labels: vi.fn(async () => []),
  LabelPicker: () => null,
  TagChips: () => null,
}));

vi.mock("./uploadWatch", async (original) => ({
  ...await original<typeof import("./uploadWatch")>(),
  untilSettled: vi.fn(),
}));

const { Upload } = await import("./Upload");
const { uploadDocument } = await import("../api");
const { untilSettled } = await import("./uploadWatch");

const existing = {
  id: "existing-document",
  filename: "Stored report.pdf",
  description: null,
  sha256: "public-synthetic-digest",
  media_type: "application/pdf",
  status: "ready",
  status_detail: null,
  page_count: 1,
  size_bytes: 8,
  uploaded_by: null,
  created_at: "2026-10-03T00:00:00Z",
  label_ids: [],
};

beforeEach(() => {
  vi.mocked(uploadDocument).mockReset();
  vi.mocked(untilSettled).mockReset();
  vi.mocked(untilSettled).mockResolvedValue({ outcome: "settled", document: existing });
});

async function send(name: string) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Upload documents"), {
      target: { files: [new File(["%PDF-1.4"], name, { type: "application/pdf" })] },
    });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  });
}

describe("a server-deduplicated upload", () => {
  it("offers the document formats the server can ingest, including text suffix aliases", () => {
    render(<Upload token="public-test-token" onUploaded={() => {}} />);

    const offered = screen.getByLabelText("Upload documents").getAttribute("accept")?.split(",");
    expect(offered).toEqual([
      "application/pdf", "text/plain", "text/markdown", ".pdf", ".txt", ".text", ".md", ".markdown",
    ]);
    expect(screen.getByText("Choose PDFs, TXT or Markdown", { exact: true })).toBeTruthy();
  });

  it("reports already present and keeps one recent row for the stored document", async () => {
    vi.mocked(uploadDocument)
      .mockResolvedValueOnce({ document: existing, labels: [], deduplicated: false })
      .mockResolvedValueOnce({ document: existing, labels: [], deduplicated: true });
    render(<Upload token="public-test-token" onUploaded={() => {}} />);

    await send("first.pdf");
    await waitFor(() => expect(screen.getByText("Done", { exact: true })).toBeTruthy());
    await send("same-bytes.pdf");

    await waitFor(() => expect(screen.getByText("Already present", { exact: true })).toBeTruthy());
    expect(screen.getAllByText("Stored report.pdf", { exact: true })).toHaveLength(1);
    expect(vi.mocked(uploadDocument)).toHaveBeenCalledTimes(2);
  });

  it("keeps an existing failed document a failure rather than declaring it complete", async () => {
    const failed = { ...existing, status: "failed", status_detail: "The document could not be read" };
    vi.mocked(uploadDocument).mockResolvedValue({ document: failed, labels: [], deduplicated: true });
    vi.mocked(untilSettled).mockResolvedValue({ outcome: "settled", document: failed });
    render(<Upload token="public-test-token" onUploaded={() => {}} />);

    await send("failed-existing.pdf");

    await waitFor(() => expect(screen.getByText("The document could not be read", { exact: true })).toBeTruthy());
    expect(screen.queryByText("Already present", { exact: true })).toBeNull();
  });
});

describe("the browser's live selected files", () => {
  it("stages a bulk selection even when clearing the input empties its FileList", async () => {
    render(<Upload token="public-test-token" onUploaded={() => {}} />);
    const input = screen.getByLabelText("Upload documents") as HTMLInputElement;
    const first = new File(["%PDF-1.4"], "first.pdf", { type: "application/pdf" });
    const second = new File(["%PDF-1.4"], "second.pdf", { type: "application/pdf" });
    let held = [first, second];
    // A native FileList follows the input's current selection; a plain array in a test
    // does not. Model the lifetime that the real chooser resets after its change handler.
    const live = {
      get length() { return held.length; },
      *[Symbol.iterator]() { yield* held; },
    };
    let currentFiles = [
      new File(["%PDF-1.4"], "earlier-one.pdf", { type: "application/pdf" }),
      new File(["%PDF-1.4"], "earlier-two.pdf", { type: "application/pdf" }),
    ] as File[] | typeof live;
    Object.defineProperty(input, "files", { configurable: true, get: () => currentFiles });
    Object.defineProperty(input, "value", {
      configurable: true,
      get: () => "",
      set: (value: string) => { if (value === "") held = []; },
    });

    await act(async () => {
      // A queued change makes the bulk updater run after the input-reset handler, as it
      // may in the real browser. This is the lifetime that a static File[] would hide.
      input.dispatchEvent(new Event("change", { bubbles: true }));
      held = [first, second]; // A new chooser selection repopulates the same live list.
      currentFiles = live;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(held).toHaveLength(0);
    expect(screen.getByText("4 files staged", { exact: false })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Select first.pdf" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Select second.pdf" })).toBeTruthy();
  });
});
