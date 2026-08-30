/**
 * Automatic labelling on the path that had none: one file, in the review panel.
 *
 * Two or more files reached the staging table and its button; one file went to the
 * rename-and-describe form, which had no way of asking a model at all — so the feature was
 * unreachable in the most ordinary upload there is. It is reachable here without a button:
 * the panel asks as soon as the file is staged, because the label picker is already open
 * above it and a second control competing for the same field is two ways to answer one
 * question.
 *
 * What is tested is not that a request goes out. It is the safety model around it, which is
 * the same one the staging table has and must not diverge from:
 *
 * - a proposal is **not** an application, and survives an upload without being applied;
 * - accepting **unions** with what the person picked by hand, rather than replacing it;
 * - the three refusals settled before a model is spoken to ask nothing and say nothing.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { Label } from "@/features/labels";
import type { SuggestionRefusal } from "../api";

vi.mock("../api", () => ({
  uploadDocument: vi.fn(),
  getDocument: vi.fn(),
  suggestLabels: vi.fn(),
  suggestionAvailability: vi.fn(),
}));

// `excerpt` runs pdf.js over real bytes. What it reads is not this file's subject, and a
// `File` built from a string is not a PDF.
vi.mock("./excerpt", () => ({ excerpt: vi.fn(async () => "an invoice, dated March") }));

vi.mock("./uploadWatch", () => ({
  untilSettled: vi.fn(() => new Promise(() => {})),
  phaseFor: vi.fn(() => ({ phase: "done" as const })),
}));

const label = (id: string, name: string): Label =>
  ({ id, name, is_default: false }) as unknown as Label;

const FINANCE = label("l-finance", "finance");
const LEGAL = label("l-legal", "legal");

/**
 * The real picker searches server-side; this one is two buttons over the same contract —
 * `selected` in, a whole `Label` out — which is all this file needs in order to tick one by
 * hand before the model answers. `TagChips` is deliberately **not** replaced: the dashed,
 * unfilled chip is the thing that makes a proposal visibly a proposal, so the real one is
 * what renders.
 */
vi.mock("@/features/labels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/labels")>()),
  labels: vi.fn(async () => [FINANCE, LEGAL]),
  LabelPicker: ({
    selected,
    onToggle,
  }: {
    selected: Set<string>;
    onToggle: (label: Label) => void;
  }) => (
    <div>
      {[FINANCE, LEGAL].map((one) => (
        <button key={one.id} type="button" onClick={() => onToggle(one)}>
          {selected.has(one.id) ? `untick ${one.name}` : `tick ${one.name}`}
        </button>
      ))}
    </div>
  ),
}));

const { Upload } = await import("./Upload");
const { uploadDocument, suggestLabels, suggestionAvailability } = await import("../api");

const uploads = vi.mocked(uploadDocument);
const asked = vi.mocked(suggestLabels);
const offered = vi.mocked(suggestionAvailability);

const pdf = () => new File(["%PDF-1.4"], "invoice.pdf", { type: "application/pdf" });

beforeEach(() => {
  uploads.mockReset();
  asked.mockReset();
  offered.mockReset();
  offered.mockResolvedValue({ reason: null });
  uploads.mockImplementation(async (file) => ({
    document: {
      id: "d1",
      filename: file.name,
      description: null,
      sha256: "abc",
      media_type: "application/pdf",
      status: "pending",
      status_detail: null,
      page_count: null,
      size_bytes: 8,
      uploaded_by: null,
      created_at: "2026-08-25T00:00:00Z",
      label_ids: [],
    },
    labels: [],
    deduplicated: false,
  }));
});

/** Choose exactly one file, which is the path that goes to the review panel. */
async function chooseOne() {
  render(<Upload token="t" onUploaded={() => {}} />);
  // The picker's two buttons only exist once the label list has arrived, and the panel reads
  // the same map to name a proposed id.
  await screen.findByText("tick finance");
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Upload PDFs"), { target: { files: [pdf()] } });
  });
}

/** The ids the one upload was actually sent with. */
function sentLabels(): string[] {
  return (uploads.mock.calls[0]?.[2] ?? []) as string[];
}

async function upload() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  });
  await waitFor(() => expect(uploads).toHaveBeenCalledTimes(1));
}

describe("one file, staged for review", () => {
  it("asks where it belongs without anybody pressing anything", async () => {
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOne();

    // The excerpt, not the file: a document that is only being considered never leaves the
    // machine.
    await waitFor(() => expect(asked).toHaveBeenCalledWith("t", "an invoice, dated March"));
    // And only after the pre-flight, so a tenant that can never be offered this is never
    // asked about.
    expect(offered).toHaveBeenCalledWith("t");
  });

  it("shows the answer as a proposal, not as a chosen label", async () => {
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOne();

    // The dashed chip's own title. A filed chip says its name; this one says what it is.
    expect(await screen.findByTitle("Suggested — not yet applied")).toBeTruthy();
    // The picker is untouched: `legal` is still offered to be ticked, not shown as ticked.
    expect(screen.getByText("tick legal")).toBeTruthy();
  });

  it("does not apply the proposal until somebody accepts it", async () => {
    // The defect this whole shape exists to prevent. A label is a permission here, so a
    // suggestion that files itself is a machine deciding who may read the document.
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOne();
    await screen.findByTitle("Suggested — not yet applied");
    await upload();

    expect(sentLabels()).toEqual([]);
  });

  it("adds to what the person picked when accepted, rather than replacing it", async () => {
    // Somebody ticked a folder by hand while the model was still reading. The suggestion is
    // an addition to their judgement, and dropping `finance` here would be the interface
    // revoking a permission it was never asked to touch.
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOne();
    await act(async () => {
      fireEvent.click(screen.getByText("tick finance"));
    });
    await screen.findByTitle("Suggested — not yet applied");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply the suggestion/ }));
    });
    await upload();

    expect(sentLabels()).toEqual([FINANCE.id, LEGAL.id]);
  });

  it("keeps the hand-picked label and drops the proposal when dismissed", async () => {
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOne();
    await act(async () => {
      fireEvent.click(screen.getByText("tick finance"));
    });
    await screen.findByTitle("Suggested — not yet applied");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Dismiss the suggestion/ }));
    });
    expect(screen.queryByTitle("Suggested — not yet applied")).toBeNull();
    await upload();

    expect(sentLabels()).toEqual([FINANCE.id]);
  });
});

describe("the three refusals settled before a model is spoken to", () => {
  const remedies: SuggestionRefusal[] = ["unavailable", "no_folders", "too_many_folders"];

  for (const reason of remedies) {
    it(`${reason}: nothing is asked, and nothing is said about it`, async () => {
      // Four of the six tenants on this installation hold labels of which none can be
      // suggested, so this is the ordinary first experience rather than an edge. The panel's
      // job — name the file, describe it, send it — does not depend on a model, and a
      // sentence about a feature nobody asked for is noise on a form.
      offered.mockResolvedValue({ reason });

      await chooseOne();

      await waitFor(() => expect(offered).toHaveBeenCalled());
      expect(asked).not.toHaveBeenCalled();
      expect(screen.queryByText("Looking for a folder for this document…")).toBeNull();
      expect(screen.queryByTitle("Suggested — not yet applied")).toBeNull();
      // Not even the endings the staging rows write. Nothing was asked, so there is no
      // ending to report.
      expect(screen.queryByText(/not offered/)).toBeNull();
      expect(screen.queryByText(/untagged/)).toBeNull();
    });
  }
});

describe("the endings that only a call can reach", () => {
  it("says the model broke, and does not promise the document has been filed", async () => {
    // `failed` is the one ending that leaves the document waiting for an administrator, so
    // the panel must not fall back to the comfortable sentence. The tenant's key on this
    // installation has no credits, which is exactly this.
    asked.mockResolvedValue({
      outcome: "failed",
      labelIds: [],
      detail: "your prepayment credits are depleted",
    });

    await chooseOne();

    const note = await screen.findByText(/suggestion failed/);
    expect(note.textContent).toMatch(/prepayment credits are depleted/);
    expect(note.textContent).not.toMatch(/server will file it/);
    expect(note.className).toMatch(/zenith-amber/);
  });

  it("says the model read it and found nothing, when that is what happened", async () => {
    asked.mockResolvedValue({ outcome: "declined", labelIds: [] });

    await chooseOne();

    expect(await screen.findByText("no match — server will file it")).toBeTruthy();
  });

  it("goes quiet once the person has picked a label themselves", async () => {
    // The staging rows' own guard: an ending is worth reading beside an empty picker and is
    // noise beside a folder somebody chose.
    asked.mockResolvedValue({ outcome: "declined", labelIds: [] });

    await chooseOne();
    await screen.findByText("no match — server will file it");

    await act(async () => {
      fireEvent.click(screen.getByText("tick finance"));
    });

    expect(screen.queryByText("no match — server will file it")).toBeNull();
  });
});
