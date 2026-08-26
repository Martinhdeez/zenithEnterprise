/**
 * An answer, as something you can paste somewhere else.
 *
 * Until now nothing in this product could leave the screen it was written on: a search of
 * the whole frontend finds `navigator.clipboard` in exactly one place, the invitation
 * panel. Somebody gets a good answer, and the only way to send it to a colleague is a
 * screenshot or retyping it.
 *
 * **The sources come with it, and that is the point rather than a nicety.** The answer text
 * alone is a claim; the citations are what make it checkable, and this product's whole
 * argument is that a generated answer should be traceable to a page. Copying the prose
 * without them would strip exactly the property being sold, and the pasted paragraph would
 * look like any other chatbot output.
 *
 * Markdown, not plain text and not HTML. It pastes as readable text into a chat window and
 * as formatted text into anything that understands it, which covers where these actually go.
 */

import type { QueryResult } from "../stream/stream";

/**
 * The answer and its sources, ready for the clipboard.
 *
 * `[1]`-style markers are left exactly as the model wrote them, so the numbers in the prose
 * still point at the list underneath. Rewriting them into links would produce something
 * that renders as a link to nothing once it is out of this application.
 */
export function transcript(answer: string, result: QueryResult): string {
  const body = answer.trim();
  // Deduplicated by marker: the same passage can be cited twice in one answer, and a source
  // list that repeats an entry reads as two different sources saying the same thing.
  const seen = new Map<number, QueryResult["citations"][number]>();
  for (const citation of result.citations) {
    if (!seen.has(citation.marker)) seen.set(citation.marker, citation);
  }

  const sources = [...seen.values()]
    .sort((a, b) => a.marker - b.marker)
    // A document with no pages is named without one. "page null" in a transcript somebody
    // pastes into a ticket is worse than a filename on its own.
    .map((citation) =>
      citation.page_num === null
        ? `[${citation.marker}] ${citation.filename}`
        : `[${citation.marker}] ${citation.filename} — page ${citation.page_num}`,
    );

  if (sources.length === 0) {
    // An abstention, or an answer drawn from the conversation rather than the corpus.
    // "Sources" followed by nothing would suggest something went missing.
    return body;
  }

  return `${body}\n\n**Sources**\n${sources.join("\n")}`;
}
