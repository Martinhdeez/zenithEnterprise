from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    UnaryExpression,
    delete,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert

from app.common.repositories.base import ScopedRepository
from app.features.auth.model import Role, UserRole
from app.features.documents.model import Document
from app.features.labels.model import AccessLabel, DocumentLabel, RoleLabel
from app.features.labels.pagination import LabelCursor, Sort


class LabelRepository(ScopedRepository[AccessLabel]):
    model = AccessLabel

    async def by_name(self, name: str) -> AccessLabel | None:
        return await self.session.scalar(select(AccessLabel).where(AccessLabel.name == name))

    async def reachable(self) -> list[AccessLabel]:
        """Only the labels the current context reaches.

        A label name is itself a disclosure — "Project Titan acquisition" says something
        merely by existing. RLS on `access_labels` is tenant-wide, because the rows have to
        be readable for the joins that resolve a context, so this restriction is
        application-level. That makes it exactly the kind of rule that gets forgotten,
        which is why it has its own test.
        """
        if not self.context.label_ids:
            return []
        statement = select(AccessLabel).where(AccessLabel.id.in_(self.context.label_ids))
        return list(await self.session.scalars(statement.order_by(AccessLabel.name)))

    async def all_in_tenant(self) -> list[AccessLabel]:
        """Every label. For holders of `labels.manage`: managing a set you cannot
        enumerate is not management."""
        return list(await self.session.scalars(select(AccessLabel).order_by(AccessLabel.name)))

    async def search(
        self,
        *,
        query: str | None,
        sort: Sort,
        cursor: LabelCursor | None,
        limit: int,
        restrict_to: list[UUID] | None,
        in_use: bool = False,
        uploaded_by: UUID | None = None,
    ) -> list[tuple[AccessLabel, int, datetime | None]]:
        """One page of labels, each with a document count and when it was last applied.

        `restrict_to` is the caller's reachable set, or `None` for a holder of
        `labels.manage` — the same admin/non-admin split `visible()` makes, threaded
        through rather than re-decided here, so the two can never disagree about who sees
        what.

        `in_use` drops labels nothing carries. At the scale this endpoint exists for —
        thousands of labels, most of them typos and abandoned experiments — the useful
        question is usually "which of these does the corpus actually use", and that is not
        answerable by name.

        `uploaded_by` narrows to labels on documents that user uploaded: "the ones I file
        things under", which is a far shorter list than the tenant's and the one somebody
        reaches for by muscle memory. It is a parameter rather than something read from
        the context because `AuthService.profile` leaves `context.user_id` unset on every
        ordinary request — the same trap that made `grant_to_creator` a silent no-op.

        **Both the count and the last-used timestamp are RLS-scoped, and that is a real
        semantic rather than an oversight.** `document_labels` inherits its policy from
        `documents`, so these describe *documents the caller can see* carrying the label,
        never the size or activity of a compartment they were not admitted to.

        One row more than `limit` is fetched, so the caller can tell a full page from the
        last one without a second query or a count.
        """
        applied = select(
            DocumentLabel.label_id.label("label_id"),
            func.count().label("documents"),
            func.max(Document.created_at).label("last_used"),
        ).join(Document, Document.id == DocumentLabel.document_id)
        if uploaded_by is not None:
            applied = applied.where(Document.uploaded_by == uploaded_by)
        usage_by_label = applied.group_by(DocumentLabel.label_id).subquery()

        usage = func.coalesce(usage_by_label.c.documents, 0)
        last_used = usage_by_label.c.last_used

        statement = select(AccessLabel, usage, last_used).outerjoin(
            usage_by_label, usage_by_label.c.label_id == AccessLabel.id
        )
        if restrict_to is not None:
            # An empty reachable set is not "no filter" — it is "nothing". `in_([])` is
            # false for every row, which is the correct answer and the one `reachable()`
            # short-circuits to.
            statement = statement.where(AccessLabel.id.in_(restrict_to))
        if query:
            statement = statement.where(AccessLabel.name.ilike(_contains(query), escape="\\"))
        if in_use or uploaded_by is not None:
            # `uploaded_by` implies it: a label on none of my documents is not one of mine,
            # and without this the join would keep every label in the tenant with a zero.
            statement = statement.where(usage > 0)

        statement = statement.where(*_after(sort, usage, last_used, cursor)).order_by(
            *_ordering(sort, usage, last_used)
        )
        rows = await self.session.execute(statement.limit(limit + 1))
        return [(label, count, used) for label, count, used in rows]

    async def grant_to_creator(self, label_id: UUID, user_id: UUID | None) -> None:
        """Give the caller's own roles access to a label they just created.

        Without this a new label is reachable by nobody, and `provisioning.py` already
        spelled out why that is the wrong default — for the seeded label it says a label
        no role reaches "would make every unclassified upload invisible to everyone,
        including the administrator who uploaded it, and the product would look broken in a
        way no error message explains". The same reasoning applies to a label created by
        hand; it just took the upload form's "+ New label" button to make it visible,
        because that flow creates a label expressly to file the document in front of you
        under it and was then refused with a 403.

        Widening is not a concern here and that is worth stating rather than assuming: the
        label is new, so no document carries it. Granting it reaches an empty compartment.

        `user_id` is optional on a context — a worker has none — and no roles is not an
        error, only nothing to grant.
        """
        if user_id is None:
            return
        roles = select(UserRole.role_id).where(UserRole.user_id == user_id).distinct().subquery()
        await self.session.execute(
            insert(RoleLabel)
            .from_select(["role_id", "label_id"], select(roles.c.role_id, literal(label_id)))
            .on_conflict_do_nothing()
        )
        await self.session.flush()

    async def role_labels(self) -> dict[UUID, set[UUID]]:
        """Every role in the tenant and the labels it reaches.

        For the merge preview. Roles are tenant-scoped under RLS and `role_labels`
        inherits from them, so this is complete for the caller's own tenant — which is
        what makes a widening count computable without stepping outside the policy.
        """
        reach: dict[UUID, set[UUID]] = {
            role_id: set() for role_id in await self.session.scalars(select(Role.id))
        }
        pairs = await self.session.execute(select(RoleLabel.role_id, RoleLabel.label_id))
        for role_id, label_id in pairs:
            reach.setdefault(role_id, set()).add(label_id)
        return reach

    async def documents_carrying(self, label_ids: set[UUID]) -> list[set[UUID]]:
        """The full label set of every document carrying at least one of these.

        `documents.label_ids` rather than a join back through `document_labels`: it is the
        column RLS itself reads, so a document whose two copies had drifted would be
        counted here exactly as the policy will actually treat it.
        """
        if not label_ids:
            return []
        rows = await self.session.scalars(
            select(Document.label_ids).where(Document.label_ids.overlap(list(label_ids)))
        )
        return [set(row or []) for row in rows]

    async def reassign(self, sources: set[UUID], target: UUID) -> None:
        """Point every document and role at `target` instead of the sources.

        Insert-then-delete, and the insert has to tolerate collisions: a document carrying
        both a source *and* the target already holds the row this would add, and the
        composite primary key rejects the duplicate. `ON CONFLICT DO NOTHING` makes that
        the no-op it should be rather than an error the caller cannot act on.

        Both sides move, and that is the part worth reading twice. Reassigning
        `document_labels` alone would strip every role that reached only a source label of
        the documents it was admitted to; reassigning `role_labels` alone would hand those
        roles the target's existing documents while taking away their own. Each direction
        changes who sees what, which is why `LabelService.merge` refuses to run either
        until the widening both of them cause has been counted and acknowledged.

        `documents.label_ids` is not touched — migration 0003's trigger recomputes it from
        `document_labels`, and writing both is how the copy RLS reads drifts from the
        source of truth.
        """
        if not sources:
            return
        for table, owner in (
            (DocumentLabel, DocumentLabel.document_id),
            (RoleLabel, RoleLabel.role_id),
        ):
            moved = (
                select(owner.label("owner"))
                .where(table.label_id.in_(sources))
                .distinct()
                .subquery()
            )
            await self.session.execute(
                insert(table)
                .from_select([owner.key, "label_id"], select(moved.c.owner, literal(target)))
                .on_conflict_do_nothing()
            )
            await self.session.execute(delete(table).where(table.label_id.in_(sources)))
        await self.session.flush()

    async def documents_using(self, label_id: UUID) -> int:
        """How many documents carry this label.

        Deleting a label that is a document's only label leaves that document with an
        empty array — and an empty array means visible to the entire tenant. A delete that
        widens access, silently, through a cascade. This is what the guard counts.
        """
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(DocumentLabel)
                .where(DocumentLabel.label_id == label_id)
            )
            or 0
        )

    async def clear_default(self) -> None:
        await self.session.execute(
            update(AccessLabel).where(AccessLabel.is_default).values(is_default=False)
        )

    async def default(self) -> AccessLabel | None:
        return await self.session.scalar(select(AccessLabel).where(AccessLabel.is_default))

    async def quarantine(self) -> AccessLabel | None:
        """Where an upload waits for the classifier. See migration 0017.

        No tenant filter, for the same reason `default` has none: RLS has already narrowed
        `access_labels` to this tenant, and a `WHERE tenant_id = …` here would be the
        application doing the isolation work that ADR 0001 puts in the database.
        """
        return await self.session.scalar(select(AccessLabel).where(AccessLabel.is_quarantine))

    async def reserved_among(self, label_ids: list[UUID]) -> list[AccessLabel]:
        """Which of these are the product's rather than the tenant's.

        One query for every caller that has to refuse them, so "what counts as reserved" is
        answered in a single place. Today that is the quarantine label; the shape takes a
        second without any caller changing.
        """
        if not label_ids:
            return []
        return list(
            await self.session.scalars(
                select(AccessLabel).where(AccessLabel.id.in_(label_ids), AccessLabel.is_quarantine)
            )
        )

    async def role_exists(self, role_id: UUID) -> bool:
        return await self.session.scalar(select(Role.id).where(Role.id == role_id)) is not None

    async def document_exists(self, document_id: UUID) -> bool:
        return (
            await self.session.scalar(select(Document.id).where(Document.id == document_id))
            is not None
        )

    async def label_ids_in_tenant(self, label_ids: list[UUID]) -> set[UUID]:
        """Which of these labels exist in the caller's tenant.

        RLS would reject a foreign label on write anyway, but as a constraint violation
        rather than as something the API can explain. The caller gets told which id was
        wrong instead of a 500.
        """
        if not label_ids:
            return set()
        return set(
            await self.session.scalars(select(AccessLabel.id).where(AccessLabel.id.in_(label_ids)))
        )

    async def set_role_labels(self, role_id: UUID, label_ids: list[UUID]) -> None:
        await self.session.execute(delete(RoleLabel).where(RoleLabel.role_id == role_id))
        self.session.add_all(
            RoleLabel(role_id=role_id, label_id=label_id) for label_id in label_ids
        )
        await self.session.flush()

    async def set_document_labels(self, document_id: UUID, label_ids: list[UUID]) -> None:
        """Replace a document's labels.

        `documents.label_ids` is not touched here: the trigger installed by migration 0003
        recomputes it. Doing it in both places is how the copy drifts from its source, and
        the copy is what RLS actually reads.
        """
        await self.session.execute(
            delete(DocumentLabel).where(DocumentLabel.document_id == document_id)
        )
        self.session.add_all(
            DocumentLabel(document_id=document_id, label_id=label_id) for label_id in label_ids
        )
        await self.session.flush()


def _contains(query: str) -> str:
    r"""A substring match, with the caller's own wildcards neutralised.

    Without the escaping, a search for `100%` matches every label in the tenant and a
    search for `_` matches all of them too — `%` and `_` are `LIKE` metacharacters, and a
    user typing them into a search box means the literal characters. The backslash is
    escaped first, or escaping the other two would introduce metacharacters of its own.

    Substring rather than trigram similarity: at the scale this product targets — mvp.md
    caps a tenant at 5,000 documents, and label counts are far below that — `ILIKE` over a
    few hundred short strings is a sequential scan of a few kilobytes. `pg_trgm` and a GIN
    index buy typo tolerance, and are worth adding when someone has measured that they are
    needed, not before.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ordering(
    sort: Sort, usage: ColumnElement[int], last_used: ColumnElement[Any]
) -> tuple[UnaryExpression[Any], ...]:
    """Every ordering ends in `id`, and that is what makes the cursor work at all.

    `name` is unique per tenant, but `created_at` and a usage count are emphatically not —
    migration 0007 gave every pre-existing label the same timestamp, and "used by zero
    documents" is the common case, not an edge one. Without a unique tiebreaker the
    database is free to order those rows differently between two queries, and a keyset
    cursor pointing into an unstable ordering skips rows or repeats them forever.
    """
    if sort == "name":
        return (AccessLabel.name.asc(), AccessLabel.id.asc())
    if sort == "created_at":
        return (AccessLabel.created_at.desc(), AccessLabel.id.desc())
    if sort == "last_used":
        # `NULLS LAST` explicitly: Postgres sorts NULLs first under `DESC`, which would put
        # every never-used label above every recently-used one — the exact inverse of what
        # "most recently used" is asked for.
        return (last_used.desc().nullslast(), AccessLabel.id.desc())
    return (usage.desc(), AccessLabel.id.desc())


def _after(
    sort: Sort,
    usage: ColumnElement[int],
    last_used: ColumnElement[Any],
    cursor: LabelCursor | None,
) -> tuple[ColumnElement[bool], ...]:
    """Where the previous page stopped, as a row-value comparison.

    `(a, b) < (x, y)` is one comparison to Postgres rather than the three-way `a < x OR (a
    = x AND b < y)` expansion, which is both what an index can walk and what a human can
    check for the off-by-one that would drop a row between two pages.
    """
    if cursor is None:
        return ()
    if sort == "last_used" and cursor.key is None:
        # Already inside the null tail. Everything with a timestamp came before it, so the
        # only rows left are the remaining untouched labels, ordered by id.
        return (last_used.is_(None), AccessLabel.id < literal(cursor.id))
    position = tuple_(literal(cursor.key), literal(cursor.id))
    if sort == "name":
        return (tuple_(AccessLabel.name, AccessLabel.id) > position,)
    if sort == "created_at":
        return (tuple_(AccessLabel.created_at, AccessLabel.id) < position,)
    if sort == "last_used":
        # Still in the timestamped part. A row-value comparison cannot express `NULLS
        # LAST`, so the tail is spelled out alongside it: what follows is either an earlier
        # timestamp or the untouched labels that sort after all of them.
        return (
            or_(
                tuple_(last_used, AccessLabel.id) < position,
                last_used.is_(None),
            ),
        )
    return (tuple_(usage, AccessLabel.id) < position,)
