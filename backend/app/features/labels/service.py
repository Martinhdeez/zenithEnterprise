from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.common.exceptions import ConflictError, NotFoundError
from app.core.database import tenant_session
from app.features.auth.permissions import CATALOGUE
from app.features.labels.model import AccessLabel
from app.features.labels.repository import LabelRepository
from app.features.tenancy.context import TenantContext

MANAGE = "labels.manage"
assert MANAGE in CATALOGUE, "the permission this service is gated on must exist"


class LabelService:
    """Everything runs inside the caller's own context.

    No `owner_session` anywhere in this file, deliberately: RLS is what stops an
    administrator of one tenant touching another's labels, and stepping outside it here
    would remove the only thing enforcing that.
    """

    def __init__(self, context: TenantContext) -> None:
        self.context = context

    async def create(self, name: str, is_default: bool = False) -> AccessLabel:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            if is_default:
                await labels.clear_default()
            label = AccessLabel(tenant_id=self.context.tenant_id, name=name, is_default=is_default)
            session.add(label)
            try:
                await session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"a label named {name!r} already exists") from exc
            await session.refresh(label)
            return label

    async def visible(self, may_manage: bool) -> list[AccessLabel]:
        async with tenant_session(self.context) as session:
            labels = LabelRepository(session)
            return await (labels.all_in_tenant() if may_manage else labels.reachable())

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
