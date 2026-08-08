from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.common.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.database import tenant_session
from app.features.auth.permissions import CATALOGUE
from app.features.labels.model import AccessLabel
from app.features.labels.pagination import DEFAULT_SORT, Key, LabelCursor, Sort, clamp
from app.features.labels.repository import LabelRepository
from app.features.tenancy.context import TenantContext

MANAGE = "labels.manage"
assert MANAGE in CATALOGUE, "the permission this service is gated on must exist"


@dataclass(frozen=True, slots=True)
class SearchResult:
    #: `(label, documents the caller can see carrying it, when it was last applied)`.
    labels: list[tuple[AccessLabel, int, datetime | None]]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MergeResult:
    target: AccessLabel
    merged: list[UUID]
    documents_relabelled: int
    visibility_widening: int
    dry_run: bool


class LabelService:
    """Everything runs inside the caller's own context.

    No `owner_session` anywhere in this file, deliberately: RLS is what stops an
    administrator of one tenant touching another's labels, and stepping outside it here
    would remove the only thing enforcing that.
    """

    def __init__(self, context: TenantContext) -> None:
        self.context = context

    async def create(
        self,
        name: str,
        is_default: bool = False,
        created_by: UUID | None = None,
        priority_level: int = 0,
    ) -> AccessLabel:
        """Create a label, and give its creator's roles access to it.

        `created_by` is a parameter rather than being read from `self.context.user_id`,
        and that is not a style choice: `AuthService.profile` builds the request context
        with `TenantContext.for_tenant(tenant_id, labels)` and no user, so `context.user_id`
        is `None` on every ordinary request. Depending on it here made the grant a silent
        no-op in production while passing a unit test that had constructed a context by
        hand — the caller has the id, so the caller passes it.

        The grant is not a convenience. Without it the new label is reachable by nobody,
        including the person who just made it — so the upload form's "+ New label", which
        exists to file the document in front of you under a category you are inventing on
        the spot, created the label and was then refused with "you cannot file a document
        under label(s) you do not hold". `provisioning.py` already reasoned this out for
        the seeded label; it applies to a hand-made one for the same reason.

        It grants nothing away: the label is new, so no document carries it and the reach
        it confers is over an empty set. Every *other* role still gets it only when an
        administrator says so, which is the "narrow by default" the seeding comment is
        actually about.
        """
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            if is_default:
                await labels.clear_default()
            label = AccessLabel(
                tenant_id=self.context.tenant_id,
                name=name,
                is_default=is_default,
                priority_level=priority_level,
            )
            session.add(label)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"a label named {name!r} already exists") from exc
            await labels.grant_to_creator(label.id, created_by)
            await session.refresh(label)
            return label

    async def set_clearance(self, label_id: UUID, priority_level: int) -> AccessLabel:
        """How much clearance this label demands of anyone reaching it through a group.

        Zero means it demands none, which is not the same as being public: a label reachable
        by no group and granted to no role is still reachable by nobody. Clearance narrows
        the group route; it never opens anything on its own.
        """
        async with tenant_session(self.context) as session:
            label = await session.get(AccessLabel, label_id)
            if label is None:
                raise NotFoundError(f"no label {label_id}")
            label.priority_level = priority_level
            await session.flush()
            await session.refresh(label)
            return label

    async def visible(self, may_manage: bool) -> list[AccessLabel]:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            return await (labels.all_in_tenant() if may_manage else labels.reachable())

    async def search(
        self,
        *,
        may_manage: bool,
        query: str | None = None,
        sort: Sort = DEFAULT_SORT,
        cursor: str | None = None,
        limit: int | None = None,
        in_use: bool = False,
        uploaded_by: UUID | None = None,
    ) -> SearchResult:
        """One page of labels matching `query`, with usage counts.

        The admin/non-admin split is `visible()`'s, not a second one: a manager searches
        every label in the tenant, everyone else searches only what they reach, because a
        label name discloses something merely by existing.

        `in_use` and `uploaded_by` are the two filters that make a list of thousands
        usable: what the corpus actually carries, and what *this person* files things
        under. Neither can be applied in the browser, which is the point — the client
        never holds the whole list.
        """
        size = clamp(limit)
        decoded = LabelCursor.decode(cursor, sort) if cursor else None

        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            rows = await labels.search(
                query=query,
                sort=sort,
                cursor=decoded,
                limit=size,
                restrict_to=None if may_manage else list(self.context.label_ids),
                in_use=in_use,
                uploaded_by=uploaded_by,
            )

        # The extra row fetched by the repository is the answer to "is there a next page",
        # and it is dropped rather than returned: a page of `size + 1` would hand the
        # client a row it did not ask for and break the invariant that a full page is
        # exactly `limit` long.
        page, has_more = rows[:size], len(rows) > size
        next_cursor = _cursor_for(sort, page[-1]).encode() if has_more and page else None
        return SearchResult(labels=page, next_cursor=next_cursor)

    async def merge(
        self,
        sources: list[UUID],
        target: UUID,
        *,
        dry_run: bool = False,
        acknowledge_widening: bool = False,
    ) -> MergeResult:
        """Fold labels together, once the caller has seen what it costs.

        Three guards, each closing a different way this can go wrong silently.

        **The target cannot be a source.** Merging a label into itself would delete it at
        the end of its own reassignment, taking every document with it — the cascade in
        migration 0003 would then strip the label from documents whose only label it was,
        leaving them visible to the entire tenant. A typo should not be able to do that.

        **The caller must reach every label involved.** Not because they lack the
        permission — `labels.manage` is already checked at the router — but because the
        widening count below is computed under RLS, from the documents and roles the
        caller can see. An administrator who does not reach a source label cannot see its
        documents, so the count would come back reassuringly small for a merge that
        exposes a compartment they were never admitted to. Refusing is the only honest
        answer available without `owner_session`, which this file deliberately never
        opens.

        **A merge that widens visibility must be acknowledged.** Same reasoning as
        `delete`'s guard: the dangerous consequence here is not the one the caller is
        thinking about. They are tidying up a duplicate tag; the effect is that documents
        change hands between roles.
        """
        if target in set(sources):
            raise ConflictError("a label cannot be merged into itself")

        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            involved = [*sources, target]
            await self._require_all(labels, involved)
            self._require_reach(involved)

            destination = await self._require(labels, target)
            source_ids = set(sources)

            before = await labels.documents_carrying(source_ids | {target})
            reach = await labels.role_labels()
            widening = _widening(before, reach.values(), source_ids, target)
            relabelled = sum(1 for document in before if document & source_ids)

            if widening and not (dry_run or acknowledge_widening):
                raise ConflictError(
                    f"this merge makes {widening} document(s) visible to roles that cannot "
                    "see them today. Re-send with acknowledge_widening=true to proceed, or "
                    "with dry_run=true to inspect it first."
                )

            if not dry_run:
                await labels.reassign(source_ids, target)
                for label in [await self._require(labels, source) for source in source_ids]:
                    await labels.delete(label)

            return MergeResult(
                target=destination,
                merged=sorted(source_ids, key=str),
                documents_relabelled=relabelled,
                visibility_widening=widening,
                dry_run=dry_run,
            )

    async def rename(self, label_id: UUID, name: str) -> AccessLabel:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            label = await self._require(labels, label_id)
            label.name = name
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"a label named {name!r} already exists") from exc
            return label

    async def set_default(self, label_id: UUID) -> AccessLabel:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            label = await self._require(labels, label_id)
            # Cleared first: the partial unique index rejects a second default, and doing
            # both in one statement would depend on the order Postgres happens to process
            # the rows in.
            await labels.clear_default()
            await session.flush()
            label.is_default = True
            await session.flush()
            return label

    async def delete(self, label_id: UUID) -> None:
        """Refuse while any document carries the label.

        An unlabelled document is visible to the whole tenant — chosen deliberately in
        §2.2, because denying by default makes the product look broken. The consequence is
        that deleting a document's last label *widens* access, silently, through a
        cascade. So the label has to be taken off the documents first, which is a decision
        someone makes rather than a side effect they discover.

        Roles are not part of the guard: removing a label from a role only narrows what
        that role reaches, and that fails closed.
        """
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            label = await self._require(labels, label_id)
            in_use = await labels.documents_using(label_id)
            if in_use:
                raise ConflictError(
                    f"{in_use} document(s) still carry {label.name!r}. Remove it from them "
                    "first — deleting it now would leave them visible to the whole tenant."
                )
            await labels.delete(label)

    async def set_role_labels(self, role_id: UUID, label_ids: list[UUID]) -> None:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            if not await labels.role_exists(role_id):
                raise NotFoundError(f"no role {role_id}")
            await self._require_all(labels, label_ids)
            await labels.set_role_labels(role_id, label_ids)

    async def set_document_labels(self, document_id: UUID, label_ids: list[UUID]) -> None:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            if not await labels.document_exists(document_id):
                # Also the answer when the document exists but the caller cannot reach it.
                # Saying "forbidden" would confirm it exists, which §2.2 forbids.
                raise NotFoundError(f"no document {document_id}")
            await self._require_all(labels, label_ids)
            await labels.set_document_labels(document_id, label_ids)

    async def _require(self, labels: LabelRepository, label_id: UUID) -> AccessLabel:
        label = await labels.get(label_id)
        if label is None:
            raise NotFoundError(f"no label {label_id}")
        return label

    async def _require_all(self, labels: LabelRepository, label_ids: list[UUID]) -> None:
        found = await labels.label_ids_in_tenant(label_ids)
        missing = [str(label_id) for label_id in label_ids if label_id not in found]
        if missing:
            raise NotFoundError(f"unknown label(s): {', '.join(missing)}")

    def _require_reach(self, label_ids: list[UUID]) -> None:
        """See `merge`'s docstring — this is the guard that keeps its count honest."""
        beyond = sorted(str(label_id) for label_id in set(label_ids) - set(self.context.label_ids))
        if beyond:
            raise PermissionDeniedError(
                f"you do not reach label(s) {', '.join(beyond)}. Merging a label you cannot "
                "see would move documents you cannot see, and the visibility report would "
                "not count them."
            )


def _cursor_for(sort: Sort, row: tuple[AccessLabel, int, datetime | None]) -> LabelCursor:
    label, documents, last_used = row
    key: Key | None = {
        "name": label.name,
        "created_at": label.created_at,
        "usage_count": documents,
        "last_used": last_used,
    }[sort]
    # `key` is deliberately left null for a never-used label under `last_used`: that is
    # what tells the next page it is resuming inside the null tail rather than among the
    # timestamped rows. Substituting anything here loops the tail forever.
    return LabelCursor(sort=sort, key=key, id=label.id)


def _after_merge(labels: set[UUID], sources: set[UUID], target: UUID) -> set[UUID]:
    """What a label set becomes. Applies to documents and roles alike — a merge moves both
    the same way, which is why one function serves both."""
    if labels & (sources | {target}):
        return (labels - sources) | {target}
    return labels


def _widening(
    documents: list[set[UUID]],
    roles: Iterable[set[UUID]],
    sources: set[UUID],
    target: UUID,
) -> int:
    """Documents that at least one role can see after the merge but not before.

    Both directions of the exposure are counted here, and only one of them is obvious.

    *Forwards*: a document carrying a source label ends up carrying the target, so every
    role holding the target picks it up — the "merge Legal-only into Everyone" case.

    *Backwards*: a role holding only a source label ends up holding the target, so it
    picks up every document that already carried the target. Nothing about that document
    changed; the role's reach did. A count that only looked at relabelled documents would
    report zero for a merge that hands a role an entire compartment.

    Documents with no labels are visible tenant-wide before and after, so they can never
    be part of a widening — which is also why the caller only passes documents carrying
    one of the labels involved.
    """
    after_roles = [(role, _after_merge(role, sources, target)) for role in roles]
    widened = 0
    for before in documents:
        after = _after_merge(before, sources, target)
        for role_before, role_after in after_roles:
            visible_before = not before or bool(before & role_before)
            visible_after = not after or bool(after & role_after)
            if visible_after and not visible_before:
                widened += 1
                break
    return widened
