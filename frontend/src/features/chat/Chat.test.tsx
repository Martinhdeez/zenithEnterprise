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
