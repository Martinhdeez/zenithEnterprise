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

    @classmethod
    def for_tenant(cls, tenant_id: UUID, label_ids: Iterable[UUID] = ()) -> "TenantContext":
        return cls(tenant_id=tenant_id, label_ids=tuple(label_ids))

    def reaches(self, label_id: UUID) -> bool:
        return label_id in self.label_ids
