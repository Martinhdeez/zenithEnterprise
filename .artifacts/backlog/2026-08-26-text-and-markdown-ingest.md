# Ingesting text and markdown

**Status:** backlog. Not before the demonstration — the demo's proof is the bounding-box
highlight on the measured PDF corpus, and nothing here improves that.

## Why it is worth doing

A mixed archive that only accepts PDF leaves out the documents most organisations have most
of: manuals, tickets, policies, runbooks, notes. None of those were ever PDFs, and
converting them to one to get them into the product is a tax the customer pays for our
parser's convenience.

## Why it is smaller than it looks

Search does not change. Once a file is chunks of text plus embeddings, the lexical half,
the dense half, fusion, the cross-encoder and chat have no idea it was ever a PDF. `Hit` is
already text plus a pointer back to the source.

**The offsets already exist.** `chunks.char_start` and `char_end` have been `NOT NULL` since
migration 0001 and are written on every chunk (`pipeline.py:433`). Nothing reads them: there
is no `char_start` anywhere in `frontend/src`, and `HitResponse` does not carry them.

The reason citations use page plus boxes rather than offsets is specific to PDFs, and it is
not a preference. pdfplumber's extracted text and pdf.js's text layer do not agree —
whitespace, ligatures, hyphenation — so an offset computed against one highlights the wrong
span in the other. A native text file has one text, the bytes on disk, and that objection
does not apply to it.

## The one trap

**`char_start` is page-relative, not document-relative.** `chunk_page` restarts at zero for
each page (`chunker.py:53`), and that page's text is persisted next to the chunks. The rule
that holds for both file types is *offsets index into the stored text unit* — the page for a
PDF, the whole file for a `.txt`. A viewer that assumed document-relative offsets would
highlight the wrong place in every multi-page PDF, and would do it silently.

## Order

1. **Accept `text/plain` and `text/markdown`.** Magic bytes will not serve here — a text file
   has none — so this is extension plus a decode check. Store the real suffix instead of the
   hardcoded `.pdf` (`storage.py:81`) and serve the real content type instead of the
   hardcoded `application/pdf` (`documents/router.py:159`).
2. **Chunk on offsets.** A second splitter over the whole stream with the same overlap, not a
   change to `chunk_page`. `bboxes` empty, `page_num` nullable, `char_start`/`char_end`
   surfaced on `HitResponse`. This is the schema change, and it is small.
3. **A text viewer that underlines the range.** Same result list, two openers, chosen by the
   document's type. Plain for `.txt`, rendered for `.md`.
4. **`.docx` last, converted to PDF on ingest.** A native Word renderer is a third viewer for
   a format whose users will accept a PDF view. Converting keeps the boxes and the viewer we
   already have.

## What not to do

Do not give a `.txt` `page_num = 1` and empty boxes and open it in the PDF frame. It reads as
a broken PDF sitting next to real ones in the same result list — worse than not supporting
the format.

Audio is a different product, not a fifth step: a transcription model, and timestamps where
pages and boxes are now. Judge it on its own.
