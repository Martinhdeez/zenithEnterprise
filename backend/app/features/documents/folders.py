"""Folders, computed rather than stored.

`mvp.md` §2.14 lists folder hierarchies as an explicit non-goal, and this does not
contradict it — nothing here is a folder a user can create, move or nest. It is the label
structure the tenant already has, aggregated into the shape a file tree wants, so the
client can render one without inventing the grouping itself.

**The grouping is computed here, not in the browser.** Three reasons, and the third is the
one that matters:

1. A client grouping documents itself needs every document to do it, which is the listing
   endpoint's whole page budget spent to draw a sidebar.
2. Two clients would group differently. A count that disagrees between the sidebar and the
   list is a bug report nobody can reproduce.
3. **A document with no labels is visible to the whole tenant, and one with labels is
   visible only to a role that reaches them.** That rule lives in the RLS policy, and a
   client reconstructing the tree from a flat list would have to re-implement it — which is
   exactly how a folder appears in a sidebar for someone who cannot open anything inside it.

Everything runs inside `tenant_session`, so a folder the caller cannot reach is not empty:
it is absent, along with any hint that it exists.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text

from app.core.database import tenant_session
from app.features.documents.model import IN_FLIGHT
from app.features.tenancy.context import TenantContext

#: The bucket for documents carrying no label at all. Not a label, so it has no id — and it
#: must exist, because those documents are visible to the entire tenant and a tree that
#: omitted them would hide most of a new installation's corpus.
UNLABELLED = "Unlabelled"


@dataclass(frozen=True, slots=True)
class Folder:
    label_id: UUID | None
    name: str
    is_default: bool
    documents: int
    #: Broken out because a folder holding three ready documents and one failed one is a
    #: different thing from a folder holding four, and the failed one is invisible in
    #: search — the only place its absence can be explained is a count like this.
    ready: int
    processing: int
    failed: int


@dataclass(frozen=True, slots=True)
class FolderTree:
    folders: list[Folder] = field(default_factory=list[Folder])
    #: How many documents the caller can actually reach — counted, not summed.
    #:
    #: Summing the folders was right only while a document had at most one label. It has
    #: had several since documents began carrying independent `label_ids`, and a document
    #: filed under five labels appears in five folders and was counted five times: nine
    #: documents were reported to the user as twenty-one, on the first screen.
    #:
    #: A folder count and a corpus size are different questions, and the second one cannot
    #: be derived from the first when the sets overlap.
    total_documents: int = 0


async def tree(context: TenantContext) -> FolderTree:
    """Every label the caller can reach that has at least one document behind it.

    A label with no documents is omitted deliberately. An empty folder in a sidebar is a
    place to click that does nothing, and the tenant's full label list is already available
    from `GET /labels` for the screens that need to manage them rather than browse them.
    """
    async with tenant_session(context) as session:
        rows = await session.execute(
            text(
                # One pass, grouped in Postgres. The alternative — a query per label — is
                # a round trip per folder on a screen that renders before anything else.
                "SELECT l.id AS label_id, l.name, l.is_default, "
                "       count(*) AS documents, "
                "       count(*) FILTER (WHERE d.status = 'ready') AS ready, "
                f"       count(*) FILTER (WHERE d.status IN {IN_FLIGHT}) AS processing, "
                "       count(*) FILTER (WHERE d.status = 'failed') AS failed "
                "FROM documents d "
                "JOIN document_labels dl ON dl.document_id = d.id "
                "JOIN access_labels l ON l.id = dl.label_id "
                "GROUP BY l.id, l.name, l.is_default "
                "ORDER BY l.is_default DESC, l.name"
            )
        )
        folders = [
            Folder(
                label_id=row.label_id,
                name=row.name,
                is_default=row.is_default,
                documents=int(row.documents),
                ready=int(row.ready),
                processing=int(row.processing),
                failed=int(row.failed),
            )
            for row in rows
        ]

        # Counted separately rather than as a `GROUP BY` branch: `label_ids = '{}'` is the
        # condition the RLS policy itself uses for "visible to the whole tenant", and
        # expressing it the same way here keeps the two readings of that rule identical.
        unlabelled = (
            await session.execute(
                text(
                    "SELECT count(*) AS documents, "
                    "       count(*) FILTER (WHERE status = 'ready') AS ready, "
                    f"       count(*) FILTER (WHERE status IN {IN_FLIGHT}) AS processing, "
                    "       count(*) FILTER (WHERE status = 'failed') AS failed "
                    "FROM documents WHERE label_ids = '{}'::uuid[]"
                )
            )
        ).one()

        # The whole reachable corpus, in its own query. Every row `documents` would return
        # to this caller, counted once regardless of how many labels it carries — which is
        # the one thing a sum over the folders above cannot say.
        total = await session.scalar(text("SELECT count(*) FROM documents")) or 0

    if unlabelled.documents:
        folders.append(
            Folder(
                label_id=None,
                name=UNLABELLED,
                is_default=False,
                documents=int(unlabelled.documents),
                ready=int(unlabelled.ready),
                processing=int(unlabelled.processing),
                failed=int(unlabelled.failed),
            )
        )

    return FolderTree(folders=folders, total_documents=int(total))
