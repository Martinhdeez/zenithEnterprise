"""What kinds of file this product can hold, and what each one implies.

One table, read everywhere, for the reason `hardware.py` gives about profiles: the
alternative is `if pdf:` appearing in the upload gate, then in storage, then in the parser
router, then on the download response, with each site free to drift. Four places already
hardcoded `pdf` before this module existed, and they were four opportunities to disagree.

**A media type is decided once, on upload, from the bytes.** Never re-derived later from the
filename: an extension is a claim the uploader makes, and `notes.pdf.txt` is the case that
makes a re-derivation wrong. It is stored on the row (`documents.media_type`) and everything
downstream reads it from there.
"""

from dataclasses import dataclass
from typing import Final

PDF: Final = "application/pdf"
PLAIN: Final = "text/plain"
MARKDOWN: Final = "text/markdown"


@dataclass(frozen=True, slots=True)
class Media:
    media_type: str
    #: What the file is stored as under its content-addressed name. Content addressing
    #: makes the suffix decorative for lookup, but an operator opening the storage
    #: directory should not have to guess, and a `.txt` written as `.pdf` is a trap laid
    #: for whoever debugs this next.
    suffix: str
    #: Whether a citation into this document points at a page and a rectangle. `False`
    #: means the citation is a character range instead — see `Chunk.char_start`.
    paginated: bool


MEDIA: Final[dict[str, Media]] = {
    PDF: Media(media_type=PDF, suffix=".pdf", paginated=True),
    PLAIN: Media(media_type=PLAIN, suffix=".txt", paginated=False),
    MARKDOWN: Media(media_type=MARKDOWN, suffix=".md", paginated=False),
}

#: For the `CHECK` constraint and the migration that installs it. A media type storable
#: without a parser is a document that ingestion will accept and then fail.
MEDIA_TYPES: Final[tuple[str, ...]] = tuple(MEDIA)

#: Filename suffix -> media type, for the text formats only. A PDF is recognised by its
#: magic number and never by its name, because it has one and text does not.
BY_SUFFIX: Final[dict[str, str]] = {
    ".txt": PLAIN,
    ".text": PLAIN,
    ".md": MARKDOWN,
    ".markdown": MARKDOWN,
}

PDF_MAGIC: Final = b"%PDF-"


def suffix_for(media_type: str) -> str:
    return MEDIA[media_type].suffix


def is_paginated(media_type: str) -> bool:
    return MEDIA[media_type].paginated
