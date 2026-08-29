"""Reading a file that is already text.

There is nothing to extract — the bytes on disk are the document — so this parser's whole
job is decoding and normalising, and its value is that the rest of the pipeline does not
have to know the difference.

**It returns one unit, not one per screenful of text.** `ParsedPage` is the pipeline's
storage unit and its name is a PDF word; for a text document the unit is the file. Splitting
it into pretend pages would put a page number on a citation that has none, which is the
thing this feature exists to avoid.
"""

from pathlib import Path

from app.features.documents.media import MARKDOWN
from app.features.ingestion.parsers.base import ParsedPage

#: The one ordinal `pages` needs for its `(document_id, page_num)` key. Internal to
#: storage: `Chunk.page_num` stays `None`, and nothing renders this.
ONLY_UNIT = 1


class TextParser:
    name = "text"

    def __init__(self, media_type: str = MARKDOWN) -> None:
        self.media_type = media_type

    def parse(self, path: Path) -> list[ParsedPage]:
        # Validated as UTF-8 at the gate, before a byte was written — see
        # `documents.service._of_type`. Decoding here cannot be the first time we find out,
        # which is why this is not wrapped in a try.
        # Universal newlines, which is `read_text`'s default and is load-bearing rather
        # than incidental: `\r\n` and a lone `\r` both arrive as `\n`, so a file written
        # on Windows chunks identically to the same file written anywhere else.
        #
        # It has to happen *here*, before anything measures a length. Collapsing line
        # endings after the offsets were computed would move every highlight by one
        # character per preceding line — a drift that grows down the document and that
        # nobody on a Unix machine can reproduce.
        #
        # Passing `newline=""` would switch this off and is the mutation the parser's test
        # exists to catch. It looks like a harmless explicitness.
        text = path.read_text(encoding="utf-8")
        return [
            ParsedPage(
                page_num=ONLY_UNIT,
                text=text,
                words=(),
                method=self.name,
                warnings=(),
            )
        ]
