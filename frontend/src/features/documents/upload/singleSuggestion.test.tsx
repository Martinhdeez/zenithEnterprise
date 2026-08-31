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
 *
 * **Every label here used to carry `is_default: false`, and that is why eleven passing tests
 * described a panel that was silent on every real tenant.** A tenant always has a default,
 * the panel pre-checks it on load, and the note's guard read "carries no labels" as "has not
 * decided" — so on an installation the note never appeared once. The fixture was not a
 * tenant; it was the one arrangement of a tenant in which the bug is invisible. The last
 * group below builds the ordinary one.
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

const label = (id: string, name: string, is_default = false): Label =>
  ({ id, name, is_default }) as unknown as Label;

const FINANCE = label("l-finance", "finance");
const LEGAL = label("l-legal", "legal");
/** What every tenant has and this file used to pretend none had. Pre-checked on load. */
const EVERYTHING = label("l-everything", "everything", true);

/**
 * The real picker searches server-side; this one is a button per label over the same
 * contract — `selected` and `known` in, a whole `Label` out — which is all this file needs
 * in order to tick one by hand before the model answers, or to untick the default the panel
 * pre-checked. `TagChips` is deliberately **not** replaced: the dashed, unfilled chip is the
 * thing that makes a proposal visibly a proposal, so the real one is what renders.
 *
 * It draws `known` rather than a hard-coded pair so that a tenant with a default label is
 * reachable through the same buttons as any other. Hard-coding the pair is how the default
 * stayed out of this file for as long as it did.
 */
vi.mock("@/features/labels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/labels")>()),
  labels: vi.fn(async () => [FINANCE, LEGAL]),
  LabelPicker: ({
    selected,
    known,
    onToggle,
  }: {
    selected: Set<string>;
    known: Map<string, Label>;
    onToggle: (label: Label) => void;
  }) => (
    <div>
      {[...known.values()].map((one) => (
        <button key={one.id} type="button" onClick={() => onToggle(one)}>
          {selected.has(one.id) ? `untick ${one.name}` : `tick ${one.name}`}
        </button>
      ))}
    </div>
  ),
}));

const { Upload } = await import("./Upload");
const { uploadDocument, suggestLabels, suggestionAvailability } = await import("../api");
const { labels: listLabels } = await import("@/features/labels");

const uploads = vi.mocked(uploadDocument);
const asked = vi.mocked(suggestLabels);
const offered = vi.mocked(suggestionAvailability);
const listed = vi.mocked(listLabels);

const pdf = () => new File(["%PDF-1.4"], "invoice.pdf", { type: "application/pdf" });

beforeEach(() => {
  uploads.mockReset();
  asked.mockReset();
  offered.mockReset();
  listed.mockReset();
  offered.mockResolvedValue({ reason: null });
  listed.mockResolvedValue([FINANCE, LEGAL]);
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
  // The picker's buttons only exist once the label list has arrived, and the panel reads the
  // same map to name a proposed id.
  await screen.findByText("tick finance");
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Upload PDFs"), { target: { files: [pdf()] } });
  });
}

/**
 * The same, on a tenant that has a default label — which is every tenant.
 *
 * `everything` comes back ticked without anybody ticking it, which is the arrangement the
 * rest of this file never built.
 */
async function chooseOneOnAnOrdinaryTenant() {
  listed.mockResolvedValue([EVERYTHING, FINANCE, LEGAL]);
  await chooseOne();
  expect(screen.getByText("untick everything")).toBeTruthy();
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

/**
 * The tenant's default is pre-checked, and a pre-check is not a decision.
 *
 * This is the group that would have caught the defect. Everything above runs on a tenant
 * with no default label, which is not a tenant — so `selected` stayed empty until somebody
 * ticked something, the note's guard held, and eleven tests agreed the panel worked while it
 * said nothing at all on the installation it was written for.
 */
describe("a tenant with a default label", () => {
  it("still reports an ending, though the default arrived ticked", async () => {
    // The reported defect, in the ending the reporter actually hit: the tenant's key has no
    // credit, so the call comes back `failed`, and `failed` is the one ending that can leave
    // the document waiting for an administrator. It was suppressed by a label nobody chose.
    asked.mockResolvedValue({
      outcome: "failed",
      labelIds: [],
      detail: "your prepayment credits are depleted",
    });

    await chooseOneOnAnOrdinaryTenant();

    const note = await screen.findByText(/suggestion failed/);
    expect(note.textContent).toMatch(/prepayment credits are depleted/);
  });

  it("says the model read it and found nothing, with the default still ticked", async () => {
    asked.mockResolvedValue({ outcome: "declined", labelIds: [] });

    await chooseOneOnAnOrdinaryTenant();

    expect(await screen.findByText("no match — server will file it")).toBeTruthy();
  });

  it("goes quiet once a folder is ticked beside the default", async () => {
    // The half of the guard that was never broken, kept: an ending is worth reading while
    // nobody has answered, and noise beside a folder somebody chose.
    asked.mockResolvedValue({ outcome: "declined", labelIds: [] });

    await chooseOneOnAnOrdinaryTenant();
    await screen.findByText("no match — server will file it");

    await act(async () => {
      fireEvent.click(screen.getByText("tick finance"));
    });

    expect(screen.queryByText("no match — server will file it")).toBeNull();
  });

  it("keeps reporting when the default is unticked and nothing replaces it", async () => {
    // "They touched the picker" alone would go quiet here, and this is the one place it must
    // not: unticking the default and choosing nothing is the document going up carrying no
    // labels at all, which is exactly what the ending is about. Touching is not deciding —
    // a decision is a tick that survives.
    asked.mockResolvedValue({ outcome: "declined", labelIds: [] });

    await chooseOneOnAnOrdinaryTenant();
    await screen.findByText("no match — server will file it");

    await act(async () => {
      fireEvent.click(screen.getByText("untick everything"));
    });

    expect(screen.getByText("no match — server will file it")).toBeTruthy();
  });

  it("keeps the default when the suggestion is accepted beside it", async () => {
    // A label is a permission and the policy is a union, so the two candidates here are
    // "widen to both folders" and "drop the one the panel pre-checked". Widening is kept.
    // The default is where this document was going anyway if nobody accepted anything, so
    // adding the suggested folder exposes it no further than doing nothing would have, and
    // the result is visible in the picker, one click from being unticked. Dropping the
    // default would be the interface revoking a readership on its own initiative, and an
    // absence is the one change nobody notices.
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOneOnAnOrdinaryTenant();
    await screen.findByTitle("Suggested — not yet applied");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply the suggestion/ }));
    });
    await upload();

    expect(sentLabels()).toEqual([EVERYTHING.id, LEGAL.id]);
  });

  it("does not call an accepted document untagged", async () => {
    // Answering the proposal is deciding. Without that, the panel would follow an accepted
    // suggestion with the note for a cleared `chose` — the word `untagged`, over two labels.
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOneOnAnOrdinaryTenant();
    await screen.findByTitle("Suggested — not yet applied");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Apply the suggestion/ }));
    });

    expect(screen.queryByText("untagged")).toBeNull();
  });

  it("says nothing more after the proposal is dismissed", async () => {
    // Dismissing is the other way of answering. The document still carries the default, so
    // `untagged` would be false, and a note after a dismissal is the panel asking again.
    asked.mockResolvedValue({ outcome: "chose", labelIds: [LEGAL.id] });

    await chooseOneOnAnOrdinaryTenant();
    await screen.findByTitle("Suggested — not yet applied");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Dismiss the suggestion/ }));
    });

    expect(screen.queryByTitle("Suggested — not yet applied")).toBeNull();
    expect(screen.queryByText("untagged")).toBeNull();
  });
});
