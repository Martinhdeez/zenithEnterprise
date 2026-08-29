"""A document is not always a PDF.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-26

Manuals, tickets, policies and runbooks are the documents most organisations have most of,
and none of them was ever a PDF. Until now the only thing this product could ingest was one,
enforced in four places — the magic-number gate on upload, the hardcoded `.pdf` suffix in
storage, the single parser, and the hardcoded `application/pdf` on download.

## What this adds

**`documents.media_type`.** Explicit, not inferred from the filename at render time. The
column is what decides which viewer opens a citation, and a guess made in the browser from a
file extension is exactly the kind of inference that is right until somebody uploads
`notes.pdf.txt`. Defaults to `application/pdf` so every existing row is correct without a
backfill, and a `CHECK` keeps the set closed — a media type the pipeline has no parser for
must not be storable.

**`chunks.page_num` becomes nullable.** This is the honest half and the reason this is a
migration rather than a config change.

A PDF citation is a page and a rectangle on it. A text file has neither. The alternative
considered was storing `page_num = 1` for text documents, and it was rejected: a column that
holds a placeholder teaches every future reader that the value is always there, and the first
one to render it puts "page 1" under a document with no pages. `NULL` makes the reader
decide, which is the property worth paying a migration for.

`char_start` and `char_end` need no change. They have been `NOT NULL` on every chunk since
migration 0001 and nothing has ever read them — the offsets a text citation highlights with
were already being written, for years, unused.

**They are relative to the stored text unit, not the document.** `chunk_page` restarts at
zero for each page, so for a PDF they index into that page's text and for a text file into
the whole file. A reader that assumed document-relative offsets would highlight the wrong
span in every multi-page PDF and would do it silently.

## Downgrade

Reverting `page_num` to `NOT NULL` fails if any text document has been ingested, and that is
correct: there is no page number to put back. The downgrade deletes nothing — an operator who
truly wants to go back removes those documents first, deliberately.
"""

import sqlalchemy as sa

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


#: Imported rather than repeated: the model's `CHECK` is built from the same tuple, and
#: `test_schema_matches_models` compares the two. A literal here would drift.
from app.features.documents.media import MEDIA_TYPES  # noqa: E402


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "media_type",
            sa.Text(),
            nullable=False,
            server_default="application/pdf",
        ),
    )
    op.create_check_constraint(
        op.f("ck_documents_media_type_conocido"),
        "documents",
        "media_type IN " + str(MEDIA_TYPES),
    )
    op.alter_column("chunks", "page_num", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.alter_column("chunks", "page_num", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint(op.f("ck_documents_media_type_conocido"), "documents", type_="check")
    op.drop_column("documents", "media_type")
