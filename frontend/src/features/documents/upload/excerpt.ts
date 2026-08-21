/**
 * Enough of a staged PDF to tell what it is, read without uploading it.
 *
 * The staging area's whole point is that nothing leaves the browser until somebody confirms
 * it. Asking the server for a tag suggestion would break that — the file would go up to be
 * read and again to be kept — so the text is extracted here and only the text is sent.
 *
 * `pdfjs-dist` is already a dependency: the citation viewer renders with it, worker and
 * all. This is the same library doing the cheaper half of its job.
 */

import * as pdfjs from "pdfjs-dist";
import PdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";

// Set here as well as in `PdfViewer`, and that is not a duplicate to tidy away. The viewer
// is code-split — it is only loaded when somebody opens a citation — so a staging area that
// relied on the viewer having run first works perfectly once you have opened a document and
// silently reads nothing on a fresh page load. Every entry point that calls `getDocument`
// has to name the worker itself; the assignment is idempotent.
pdfjs.GlobalWorkerOptions.workerSrc = PdfWorker;

/** Pages read. A contract looks like a contract by the second page; a hundred-page report
    costs the same as a one-page invoice. */
const PAGES = 3;

/** Matches the server's own cap on a suggestion request, so nothing is sent to be discarded. */
const MAX_CHARACTERS = 8_000;

/**
 * The opening of a PDF as plain text, or an empty string.
 *
 * Never throws. A corrupt file, a scan with no text layer, an encrypted document — all of
 * them are ordinary here and all of them mean the same thing: no suggestion for this one,
 * and the row stays where it is. Failing the whole batch over a file that could not be read
 * would be worse than the batch going untagged.
 */
export async function excerpt(file: File): Promise<string> {
  try {
    const data = new Uint8Array(await file.arrayBuffer());
    const document_ = await pdfjs.getDocument({ data }).promise;
    const pages: string[] = [];

    for (let number = 1; number <= Math.min(PAGES, document_.numPages); number++) {
      const page = await document_.getPage(number);
      const content = await page.getTextContent();
      pages.push(
        content.items
          .map((item) => ("str" in item ? item.str : ""))
          .join(" ")
          .trim(),
      );
      // Released per page. The viewer learned this the expensive way on the server side —
      // page objects hold everything they decoded — and a batch of a thousand files read
      // three pages deep is the browser-side version of the same problem.
      page.cleanup();
    }

    await document_.destroy();
    return pages.join("\n").slice(0, MAX_CHARACTERS);
  } catch {
    return "";
  }
}
