# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false
#
# `pypdfium2` ships no type stubs, so every call through it is `Unknown` under strict mode.
# Suppressed once here, for this file only, rather than scattered across six inline
# comments that make the code harder to read than the risk warrants. Nothing else in the
# project relaxes strictness, and this file is twenty lines of laboratory tooling.

"""A PDF with no text layer at all.

**The dangerous case, and the corpus does not contain it.** Every scan we could find had
already been OCR'd by its publisher — NASA, Wikimedia and the Library of Congress all run
OCR before serving. That gives *degraded* text, which is realistic and worth having, but it
is not the failure this fixture exists for.

A PDF with no text layer ingests successfully, produces zero chunks, and retrieves nothing.
No exception, no failed status, nothing in a log. The document appears in the list, the
user asks a question about it, and the system answers about something else. That is the
worst kind of bug this product can have short of a data leak, and it is invisible until a
customer notices.

**Why this one is generated rather than downloaded.** It is a fixture, not a corpus
document — it never takes part in a recall measurement, so it does not need to be a real
document anyone would upload. What it needs is to be *exactly* zero-text, reproducibly, on
any machine. Rasterising pages of a document already in the corpus gives precisely that:
real content, real layout, delivered the way a flatbed scanner delivers it, with the text
layer gone because the pixels are all that is left.

Which is, of course, how a large share of enterprise scans are actually produced.
"""

from pathlib import Path

from eval.corpus import DOCUMENTS, load_manifest

FIXTURE = DOCUMENTS / "image-only.pdf"

# Enough pages to be a real ingestion, few enough to render in seconds.
SOURCE_ID = "gdpr"
PAGE_COUNT = 6
# 150 dpi: legible to a person and to OCR, without producing a file whose size distracts
# from the point.
DPI = 150


def build(force: bool = False) -> Path:
    """Rasterise the first pages of a corpus document into a text-free PDF."""
    import pypdfium2
    from PIL import Image

    if FIXTURE.exists() and not force:
        return FIXTURE

    source = next(document for document in load_manifest() if document.id == SOURCE_ID)
    if not source.path.exists():
        raise FileNotFoundError(f"{source.id} is not downloaded — run `python -m eval fetch` first")

    pdf = pypdfium2.PdfDocument(source.path)
    images: list[Image.Image] = []
    for index in range(min(PAGE_COUNT, len(pdf))):
        rendered = pdf[index].render(scale=DPI / 72)
        page: Image.Image = rendered.to_pil().convert("RGB")
        images.append(page)

    images[0].save(FIXTURE, save_all=True, append_images=images[1:])
    return FIXTURE


def extractable_characters(path: Path) -> int:
    """How much text a parser can pull out. For the fixture this must be exactly zero."""
    import pdfplumber

    with pdfplumber.open(path) as document:
        return sum(len((page.extract_text() or "").strip()) for page in document.pages)
