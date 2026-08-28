/**
 * Stage 1 of F25: the anchor exists, and leaving it does not cost the results.
 *
 * The two things worth asserting this early are the two that were not true before and are
 * easy to break later without noticing. The button's *position* is one of them — it was
 * specified as "immediately left of fullscreen" for a reason (it acts on the document, the
 * other two act on the panel), and nothing but a test holds an ordering like that.
 *
 * The other is that `Search` survives. Its results live in its own state, so the moment
 * somebody turns the wrapper back into `{anchored ? <Chat/> : <Search/>}` the breadcrumb
 * starts lying: it offers a way back to results that no longer exist.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnchoredChat } from "./AnchoredChat";
import { translate } from "@/shared/i18n";

const t = (key: string, vars?: Record<string, string | number>) => translate("en", key, vars);

describe("the anchored conversation", () => {
  const anchor = { documentId: "doc-1", filename: "constitucion.pdf", question: "plazo máximo" };

  it("says what it is bounded by", () => {
    render(<AnchoredChat anchor={anchor} t={t} />);
    // The scope is on screen, not implied. Without it an abstention reads as "the corpus
    // does not know", when what it means is "this document does not say".
    expect(screen.getByText(/Answering from constitucion\.pdf only/)).toBeTruthy();
  });

  it("names the region for a reader who cannot see the panel change", () => {
    render(<AnchoredChat anchor={anchor} t={t} />);
    expect(screen.getByLabelText(/Conversation about constitucion\.pdf/)).toBeTruthy();
  });

  it("renders in Spanish when the language is Spanish", () => {
    const es = (key: string, vars?: Record<string, string | number>) => translate("es", key, vars);
    render(<AnchoredChat anchor={anchor} t={es} />);
    expect(screen.getByText(/Respondiendo sólo desde constitucion\.pdf/)).toBeTruthy();
  });
});
