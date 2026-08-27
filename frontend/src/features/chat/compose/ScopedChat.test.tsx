/**
 * The `@` mention, from keystroke to request body.
 *
 * `mentions.test.ts` proves the parsing. This proves the wiring, which is where the harm
 * would be: a mention that parses, renders a chip, and then never reaches `documents` on
 * the request produces an answer drawn from the whole corpus while the screen says it came
 * from one document. That is worse than the feature not existing, because the user has been
 * told something false about where the answer came from.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../stream/stream", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../stream/stream")>()),
  streamQuery: vi.fn(() => new Promise<void>(() => {})),
}));

vi.mock("@/features/history", () => ({ history: vi.fn(() => Promise.resolve({ entries: [] })) }));

const DOCUMENTS = [
  { id: "doc-a", filename: "handbook.pdf" },
  { id: "doc-b", filename: "policy.pdf" },
];

vi.mock("@/features/documents", () => ({
  listDocuments: vi.fn(() => Promise.resolve({ items: DOCUMENTS, next_cursor: null })),
}));

import { Chat } from "../Chat";

const composer = () => screen.getByLabelText("Question") as HTMLInputElement;

const type = (value: string) =>
  fireEvent.change(composer(), { target: { value, selectionStart: value.length } });

beforeEach(() => {
  vi.clearAllMocks();
});

describe("typing an @ mention", () => {
  it("offers the documents the caller can read", async () => {
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@");

    expect(await screen.findByText("handbook.pdf")).toBeTruthy();
    expect(screen.getByText("policy.pdf")).toBeTruthy();
  });

  it("searches on the server rather than filtering one page in the browser", async () => {
    // A client-side filter finds the documents near the top of the first page and misses
    // the rest, which reads to a user as the document not existing.
    const { listDocuments } = await import("@/features/documents");
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");

    await waitFor(() =>
      expect(listDocuments).toHaveBeenCalledWith("t", null, null, "hand"),
    );
  });

  it("puts the filename in the composer when one is picked", async () => {
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    fireEvent.mouseDown(await screen.findByText("handbook.pdf"));

    await waitFor(() => expect(composer().value).toBe("@handbook.pdf "));
  });

  it("says what the question is restricted to before it is sent", async () => {
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    fireEvent.mouseDown(await screen.findByText("handbook.pdf"));

    expect(await screen.findByText("Answering from")).toBeTruthy();
  });

  it("sends the document ids with the question", async () => {
    const { streamQuery } = await import("../stream/stream");
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    fireEvent.mouseDown(await screen.findByText("handbook.pdf"));
    await waitFor(() => expect(composer().value).toBe("@handbook.pdf "));
    type("@handbook.pdf what is severance?");
    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() =>
      expect(streamQuery).toHaveBeenCalledWith(
        "@handbook.pdf what is severance?",
        "t",
        expect.anything(),
        expect.objectContaining({ documents: ["doc-a"] }),
      ),
    );
  });

  it("sends no scope when the mention has been deleted again", async () => {
    // The regression that separate chip state would produce: an answer still restricted to
    // a document the user removed from the box and can no longer see anywhere.
    const { streamQuery } = await import("../stream/stream");
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    fireEvent.mouseDown(await screen.findByText("handbook.pdf"));
    await waitFor(() => expect(composer().value).toBe("@handbook.pdf "));
    type("what is severance?");
    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() =>
      expect(streamQuery).toHaveBeenCalledWith(
        "what is severance?",
        "t",
        expect.anything(),
        expect.objectContaining({ documents: [] }),
      ),
    );
  });

  it("an ordinary question sends no documents at all", async () => {
    const { streamQuery } = await import("../stream/stream");
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("what is severance?");
    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() => expect(streamQuery).toHaveBeenCalled());
    const [, , , options] = vi.mocked(streamQuery).mock.calls[0]!;
    expect(options?.documents).toEqual([]);
  });
});

describe("the mention menu on the keyboard", () => {
  it("Enter accepts the highlighted document instead of sending the question", async () => {
    // The one outcome nobody wants: the question goes off unscoped with `@han` sitting in
    // the middle of it.
    const { streamQuery } = await import("../stream/stream");
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    await screen.findByText("handbook.pdf");
    fireEvent.keyDown(composer(), { key: "Enter" });

    await waitFor(() => expect(composer().value).toBe("@handbook.pdf "));
    expect(streamQuery).not.toHaveBeenCalled();
  });

  it("the arrows move the highlight", async () => {
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@");
    await screen.findByText("policy.pdf");
    fireEvent.keyDown(composer(), { key: "ArrowDown" });
    fireEvent.keyDown(composer(), { key: "Enter" });

    await waitFor(() => expect(composer().value).toBe("@policy.pdf "));
  });

  it("Escape closes the menu without changing the text", async () => {
    render(<Chat token="t" onCitation={() => {}} searchable />);

    type("@hand");
    await screen.findByText("handbook.pdf");
    fireEvent.keyDown(composer(), { key: "Escape" });

    await waitFor(() => expect(screen.queryByText("handbook.pdf")).toBeNull());
    expect(composer().value).toBe("@hand");
  });
});
