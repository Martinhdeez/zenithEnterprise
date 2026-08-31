/**
 * The five ways a suggestion can end, and why they must stay five.
 *
 * The server distinguishes four — it chose, it declined, it was never asked, it broke — and
 * three of them come back with no ids. The staging area used to receive only the ids, so it
 * read all three as "the model declined" and told the person `no match — server will file
 * it`. Under `failed` the document is not filed at all: ingestion leaves it in a quarantine
 * label only `admin` reaches, so the interface stated the opposite of what happens in the
 * ending where being wrong costs the most.
 *
 * The fifth was added by the call site: `suggestLabels(...).catch(() => [])` turned a 500,
 * an expired token and an unplugged network into the same empty array as a working model
 * with nothing to suggest.
 *
 * One test per ending, named for the ending. "The field is present" is a weaker question
 * than "are these two still told apart", and it is the second one that regresses.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { applySuggestion, noteFor, suggestFor } from "./suggestion";
import type { StagedFile, StagedOutcome } from "./stagingState";

vi.mock("../api", () => ({ suggestLabels: vi.fn() }));

const { suggestLabels } = await import("../api");
const asked = vi.mocked(suggestLabels);

const row = (id: string, labelIds: string[] = []): StagedFile => ({
  id,
  file: new File(["x"], `${id}.pdf`),
  labelIds,
});

/** The identity translator: keys are the English source text, so this reads them back. */
const t = (key: string) => key;

beforeEach(() => asked.mockReset());

describe("the four endings the server reports", () => {
  it("chose: the ids it named are proposed, and nothing is filed", async () => {
    asked.mockResolvedValueOnce({ outcome: "chose", labelIds: ["finance"] });

    const suggested = await suggestFor("token", "an invoice");
    const rows = applySuggestion([row("a"), row("b")], "a", suggested);

    expect(suggested.outcome).toBe("chose");
    // `proposed`, not `labelIds`. A label is a permission here, so a model's answer must not
    // land in the array a person's own ticks live in — accepting it is a separate act, and
    // this assertion is what stops the two collapsing back together.
    expect(rows[0]).toMatchObject({ proposed: ["finance"], suggestion: "chose" });
    expect(rows[0]!.labelIds).toEqual([]);
    // Only the row that was asked about. The pass runs one file at a time.
    expect(rows[1]!.proposed).toBeUndefined();
    expect(rows[1]!.suggestion).toBeUndefined();
  });

  it("declined: no labels, and the one ending the old copy was right about", async () => {
    asked.mockResolvedValueOnce({ outcome: "declined", labelIds: [] });

    const suggested = await suggestFor("token", "a birthday card");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(tagged!.labelIds).toEqual([]);
    expect(tagged!.suggestion).toBe("declined");
    // The model read it and nothing fitted, so the document does go to the tenant default.
    expect(noteFor(t, tagged!.suggestion)).toBe("no match — server will file it");
  });

  it("unavailable: nobody was asked, which is not a breakdown", async () => {
    asked.mockResolvedValueOnce({ outcome: "unavailable", labelIds: [] });

    const suggested = await suggestFor("token", "an invoice");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(tagged!.suggestion).toBe("unavailable");
    // An installation with no model configured is an ordinary one, and the row must not
    // report an error at it.
    expect(noteFor(t, "unavailable")).toBe("not offered — no model configured");
    expect(noteFor(t, "unavailable")).not.toBe(noteFor(t, "failed"));
  });

  it("failed: the row does not promise the server has filed it", async () => {
    // The defect, pinned to the ending rather than to the schema field. `failed` is the
    // model breaking: nothing vouched for the document, so ingestion leaves it in a
    // quarantine label only `admin` reaches. Saying `server will file it` here is not a
    // vaguer truth, it is the opposite one.
    asked.mockResolvedValueOnce({ outcome: "failed", labelIds: [] });

    const suggested = await suggestFor("token", "an invoice");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(tagged!.suggestion).toBe("failed");
    expect(noteFor(t, tagged!.suggestion)).toBe(
      "suggestion failed — tag it, or it may be held for review",
    );
    expect(noteFor(t, "failed")).not.toBe(noteFor(t, "declined"));
    expect(noteFor(t, "failed")).not.toMatch(/server will file it/);
  });

  it("failed: it says what the provider said, when the provider said anything", async () => {
    // The live failure. `failed` alone is true and useless: it is the same value whether
    // the connector is misconfigured or the billing account is empty, and the operator who
    // read only the outcome spent half an hour checking an endpoint, a model name and a key
    // that were all correct.
    asked.mockResolvedValueOnce({
      outcome: "failed",
      labelIds: [],
      detail:
        "the language model returned 429: Your prepayment credits are depleted. Please go " +
        "to AI Studio at https://ai.studio/projects to manage your project and billing.",
    });

    const suggested = await suggestFor("token", "an invoice");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(tagged!.reason).toMatch(/prepayment credits are depleted/);
    // What happens to the document is still said first, and still said. The provider's
    // sentence is added to it, not put in its place — the document really is waiting for an
    // administrator, whatever the reason turns out to be.
    const note = noteFor(t, tagged!.suggestion, tagged!.reason);
    expect(note).toMatch(/^suggestion failed — tag it, or it may be held for review/);
    expect(note).toMatch(/prepayment credits are depleted/);
  });

  it("failed: with nothing from the provider, the row reads exactly as it did", async () => {
    // The pair to the test above, and the reason there are two. A provider that says
    // nothing useful must not produce a trailing empty parenthesis, and it must not lose
    // the sentence that was already right.
    asked.mockResolvedValueOnce({ outcome: "failed", labelIds: [] });

    const suggested = await suggestFor("token", "an invoice");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(tagged!.reason).toBeUndefined();
    expect(noteFor(t, tagged!.suggestion, tagged!.reason)).toBe(
      "suggestion failed — tag it, or it may be held for review",
    );
  });
});

describe("the fifth ending, which only the client can see", () => {
  it("a rejected call is not an empty suggestion", async () => {
    // `.catch(() => [])` is what this replaces: a 500, an expired token and an unplugged
    // network all became the same value as a working model with nothing to suggest.
    asked.mockRejectedValueOnce(new Error("500"));

    const suggested = await suggestFor("token", "an invoice");
    const [tagged] = applySuggestion([row("a")], "a", suggested);

    expect(suggested.outcome).toBe("unreachable");
    expect(tagged!.suggestion).toBe("unreachable");
    expect(noteFor(t, "unreachable")).toBe("suggestion failed — the server could not be reached");
    expect(noteFor(t, "unreachable")).not.toBe(noteFor(t, "declined"));
    // Distinct from `failed` too: one is a statement about the installation's model, the
    // other about this browser's request, and only the second is worth retrying.
    expect(noteFor(t, "unreachable")).not.toBe(noteFor(t, "failed"));
  });

  it("does not reject, so a pass over a hundred files keeps going", async () => {
    asked.mockRejectedValueOnce(new Error("offline"));

    await expect(suggestFor("token", "text")).resolves.toMatchObject({
      outcome: "unreachable",
      labelIds: [],
    });
  });
});

describe("what the row is told", () => {
  it("says nothing has been asked before the pass reaches it", () => {
    expect(noteFor(t, undefined)).toBe("untagged");
  });

  it("gives every ending a sentence of its own", () => {
    const endings: StagedOutcome[] = ["declined", "unavailable", "failed", "unreachable"];
    expect(new Set(endings.map((ending) => noteFor(t, ending))).size).toBe(endings.length);
  });

  it("keeps the labels the row already had when the model did not choose", () => {
    // A file somebody tagged by hand and then included in a bulk pass. An ending that is not
    // `chose` carries no ids, and overwriting with an empty list would silently discard a
    // deliberate choice — the closest thing this screen has to losing work.
    const [tagged] = applySuggestion([row("a", ["legal"])], "a", {
      outcome: "failed",
      labelIds: [],
    });

    expect(tagged!.labelIds).toEqual(["legal"]);
    expect(tagged!.suggestion).toBe("failed");
  });
});
