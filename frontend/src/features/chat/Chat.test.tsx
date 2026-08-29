/**
 * The composer, which has one job between two questions: be empty.
 *
 * Both ways a question can start are covered, because they are different code paths and
 * only one of them was clearing. Typing and pressing send cleared from the beginning;
 * asking from History filled the composer programmatically and left the text there, so the
 * next question began with deleting the last one.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./stream/stream", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./stream/stream")>()),
  // Never resolves on its own: the assertion is about the composer at the moment the
  // question is sent, not about what comes back, and a stream that settles would race it.
  streamQuery: vi.fn(() => new Promise<void>(() => {})),
}));

vi.mock("@/features/history", () => ({ history: vi.fn(() => Promise.resolve({ entries: [] })) }));

import { Chat } from "./Chat";

const composer = () => screen.getByLabelText("Question") as HTMLInputElement;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("after a question is sent", () => {
  it("is empty when the question was typed", async () => {
    await act(async () => {
      render(<Chat token="t" onCitation={() => {}} searchable />);
    });

    fireEvent.change(composer(), { target: { value: "what does the contract say" } });
    expect(composer().value).toBe("what does the contract say");

    fireEvent.click(screen.getByLabelText("Send"));

    await waitFor(() => expect(composer().value).toBe(""));
  });

  it("is empty when the question came from History", async () => {
    // The regression. `prefill` put its text in the composer, where it stayed after the
    // answer had streamed — the question is already shown at the top of its own turn, so
    // the composer was holding a copy nobody had asked it to keep.
    render(
      <Chat
        token="t"
        onCitation={() => {}}
        searchable
        prefill={{ text: "an earlier question", nonce: 1 }}
      />,
    );

    await waitFor(() => expect(composer().value).toBe(""));
  });

  it("does not send an empty question", async () => {
    const { streamQuery } = await import("./stream/stream");
    await act(async () => {
      render(<Chat token="t" onCitation={() => {}} searchable />);
    });

    fireEvent.change(composer(), { target: { value: "   " } });
    fireEvent.submit(composer());

    expect(streamQuery).not.toHaveBeenCalled();
  });
});

describe("the source opens with the answer", () => {
  /**
   * The preview panel mounts only while a document is open. Rather than leave a third of
   * the viewport holding "click a citation", the first citation opens itself the moment an
   * answer has one — which is this product's argument, made without a sentence.
   */
  const answer = (citations: unknown[]) =>
    vi.fn(async (_q: string, _t: string, handlers: { onResult: (r: unknown) => void }) => {
      handlers.onResult({
        query_id: "q1",
        answer: citations.length ? "Forty hours a week [1]." : "No answer was found.",
        citations,
        abstained: citations.length === 0,
        consulted: [],
        model: "test",
        degraded: false,
        reason: null,
        took_retrieval_ms: 1,
        took_generation_ms: 1,
      });
    });

  const citation = {
    marker: 1,
    chunk_id: "c1",
    document_id: "d1",
    filename: "ley-estatuto-trabajadores.pdf",
    media_type: "application/pdf",
    page_num: 33,
    char_start: 0,
    char_end: 10,
    text: "cuarenta horas semanales",
    bboxes: [],
  };

  const ask = async (onCitation: () => void, citations: unknown[]) => {
    const { streamQuery } = await import("./stream/stream");
    vi.mocked(streamQuery).mockImplementation(answer(citations) as never);
    await act(async () => {
      render(<Chat token="t" onCitation={onCitation} searchable />);
    });
    fireEvent.change(composer(), { target: { value: "jornada ordinaria" } });
    await act(async () => {
      fireEvent.submit(composer().closest("form")!);
    });
  };

  it("opens the passage the answer leaned on", async () => {
    const onCitation = vi.fn();

    await ask(onCitation, [citation]);

    await waitFor(() =>
      expect(onCitation).toHaveBeenCalledWith(expect.objectContaining({ chunk_id: "c1" })),
    );
  });

  it("opens nothing when the answer abstained", async () => {
    // An abstention has no citations, and leaving the previous document on screen beside
    // "no answer was found in your documents" would be the interface contradicting the
    // sentence next to it.
    const onCitation = vi.fn();

    await ask(onCitation, []);

    expect(onCitation).not.toHaveBeenCalled();
  });
});

describe("the empty screen", () => {
  /**
   * It used to explain the product in four bullet points — every fact carries a citation,
   * it abstains rather than inventing, and so on. All true, and all things a single
   * question demonstrates. The record shows them instead.
   */
  const withHistory = async (entries: unknown[]) => {
    const { history } = await import("@/features/history");
    vi.mocked(history).mockResolvedValue({ entries, next_cursor: null } as never);
    await act(async () => {
      render(<Chat token="t" onCitation={() => {}} searchable />);
    });
  };

  const asked = (question: string, citations: number) => ({
    query_id: question,
    question,
    answer: "…",
    model_used: "test",
    citations,
    latency_retrieval_ms: 1,
    latency_generation_ms: 1,
    created_at: "2026-08-26T10:00:00Z",
    mine: true,
  });

  it("shows what a past question actually produced", async () => {
    await withHistory([asked("How long is the working week?", 2)]);

    expect(screen.getByText("How long is the working week?")).toBeTruthy();
    expect(screen.getByText("2 cited")).toBeTruthy();
  });

  it("shows an abstention as an abstention", async () => {
    // The claim that used to be a bullet point — it says so instead of inventing an answer
    // — shown as a fact about this corpus rather than asserted about the software.
    await withHistory([asked("How many goals did Messi score?", 0)]);

    expect(screen.getByText("found nothing")).toBeTruthy();
  });

  it("does not lecture about citations or abstention", async () => {
    // The four bullets, gone. A reader arriving here should meet a tool, not a manual.
    await withHistory([asked("How long is the working week?", 2)]);

    expect(screen.queryByText(/Every fact is followed by a citation/i)).toBeNull();
    expect(screen.queryByText(/instead of inventing an answer/i)).toBeNull();
  });

  it("offers a way in when nothing has been asked yet", async () => {
    // Nothing to show, so nothing is manufactured: a fresh installation gets starters.
    await withHistory([]);

    expect(screen.getByText(/Try/)).toBeTruthy();
    expect(screen.queryByText(/cited/)).toBeNull();
  });

  it("shows a question once however often it was asked", async () => {
    await withHistory([asked("Same question", 2), asked("Same question", 3)]);

    expect(screen.getAllByText("Same question")).toHaveLength(1);
  });
});
