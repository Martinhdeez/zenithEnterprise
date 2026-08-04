from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Everything the database needs to scope a transaction.

    Frozen, and `label_ids` is a tuple rather than a list, for one reason: a context
    mutated after the transaction opened would widen visibility without the
    `set_config` calls that already ran ever knowing. Immutability makes the value
    passed around and the value the database is enforcing the same thing by
    construction.

    It exists as a type instead of two loose arguments because `tenant_id` and
    `label_ids` are never independently correct. Passing them separately invites a
    call site that supplies the tenant and forgets the labels, which silently widens
    scope to the whole tenant — a leak that no test would catch unless it was
    looking for exactly that.
    """

    tenant_id: UUID
    label_ids: tuple[UUID, ...] = ()
    #: Who is asking. Bound as `zenith.user_id` so the policy on `queries` can enforce
    #: history privacy in the database rather than in a `WHERE` clause somebody has to
    #: remember to write — see migration 0005 and ADR 0001.
    #:
    #: Optional because most contexts do not need it: the tenant and label policies decide
    #: everything else, and a background worker has no user. Unset binds to NULL, and
    #: `user_id = NULL` is never true, so a session that forgets it reads *nothing* rather
    #: than everything.
    user_id: UUID | None = None
    #: Whether this caller may read the whole tenant's history, resolved from their
    #: permissions by the application. A boolean rather than a permission string parsed in
    #: SQL: the catalogue already lives in one place, and asking Postgres to re-derive
    #: authority would put it in two that can disagree.
    reads_all_history: bool = False

    @classmethod
    def for_tenant(
        cls,
        tenant_id: UUID,
        label_ids: Iterable[UUID] = (),
        user_id: UUID | None = None,
        reads_all_history: bool = False,
    ) -> "TenantContext":
        return cls(
            tenant_id=tenant_id,
            label_ids=tuple(label_ids),
            user_id=user_id,
            reads_all_history=reads_all_history,
        )

    def reaches(self, label_id: UUID) -> bool:
        return label_id in self.label_ids
