/**
 * F25: the anchor holds, and it is asked exactly once.
 *
 * Two of these assertions are about money and one is about trust.
 *
 * The anchor being on the request is the trust one: if `documents` ever stops being sent,
 * the conversation silently widens to the whole corpus while the screen still says it is
 * answering from one file. Nothing else in the product would notice — retrieval would
 * return perfectly good passages from somewhere else, and the citations would validate.
 *
 * The "asked once" pair is the money one. `useEffect` runs twice under StrictMode, and a
 * second run here is a second generation billed to the customer's provider. The same is
 * true every time the reader steps back to the results and returns: the thread they were
 * reading would be replaced by a fresh one they did not ask for.
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../stream/stream", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../stream/stream")>()),
  // Never resolves: these assertions are about the request that goes out, not the answer
  // that comes back, and a stream that settles would race them.
  streamQuery: vi.fn(() => new Promise<void>(() => {})),
}));

import { AnchoredChat } from "./AnchoredChat";
import { streamQuery } from "../stream/stream";
import { setLanguage } from "@/shared/i18n/useT";

const anchor = { documentId: "doc-1", filename: "constitucion.pdf", question: "plazo máximo" };

beforeEach(() => {
  vi.clearAllMocks();
  setLanguage("en");
});

describe("the anchored conversation", () => {
  it("asks the search's own question, scoped to the one document", async () => {
    await act(async () => {
      render(<AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />);
    });

    expect(streamQuery).toHaveBeenCalledTimes(1);
    const [question, , , options] = vi.mocked(streamQuery).mock.calls[0]!;
    // Not re-typed by the user: this is what they searched for.
    expect(question).toBe("plazo máximo");
    // Exactly one, and the right one. A second id here would be a widened answer wearing
    // the label of a narrow one.
    expect(options?.documents).toEqual(["doc-1"]);
  });

  it("does not ask again when the reader comes back from the results", async () => {
    const { rerender } = render(
      <AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />,
    );
    await act(async () => {
      // What stepping through the breadcrumb and back does: the element stays mounted and
      // re-renders. It must not re-ask.
      rerender(<AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />);
    });

    expect(streamQuery).toHaveBeenCalledTimes(1);
  });

  it("says what it is bounded by, so an abstention can be read correctly", async () => {
    await act(async () => {
      render(<AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />);
    });

    // Without this on screen, "I could not find it" reads as "the corpus does not know"
    // when what it means is "this document does not say".
    expect(screen.getByText(/Answering from constitucion\.pdf only/)).toBeTruthy();
    expect(screen.getByLabelText(/Conversation about constitucion\.pdf/)).toBeTruthy();
  });

  it("renders in Spanish when the language is Spanish", async () => {
    setLanguage("es");
    await act(async () => {
      render(<AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />);
    });

    expect(screen.getByText(/Respondiendo sólo desde constitucion\.pdf/)).toBeTruthy();
    setLanguage("en");
  });

  it("carries the thread and the same anchor into a follow-up", async () => {
    // The first turn has to settle, or there is no thread to carry.
    vi.mocked(streamQuery).mockImplementationOnce((async (
      _question: string,
      _token: string,
      handlers: Parameters<typeof streamQuery>[2],
    ) => {
      handlers.onResult({
        answer: "Cuarenta horas semanales.",
        citations: [],
        consulted: [],
        abstained: false,
      } as never);
    }) as never);

    await act(async () => {
      render(<AnchoredChat anchor={anchor} token="t" onCitation={vi.fn()} />);
    });

    const box = screen.getByPlaceholderText(/Ask something else/);
    await act(async () => {
      fireEvent.change(box, { target: { value: "¿y en cómputo anual?" } });
      fireEvent.submit(box.closest("form")!);
    });

    expect(streamQuery).toHaveBeenCalledTimes(2);
    const [question, , , options] = vi.mocked(streamQuery).mock.calls[1]!;
    expect(question).toBe("¿y en cómputo anual?");
    // Still one document. A follow-up that dropped the anchor would widen to the corpus
    // while the line above still promised one file — the failure this whole screen is
    // built to make impossible.
    expect(options?.documents).toEqual(["doc-1"]);
    // And the first exchange travels, or "and what about..." resolves against nothing.
    expect(options?.history).toEqual([
      { question: "plazo máximo", answer: "Cuarenta horas semanales." },
    ]);
  });
});
