"""Resolution of permissions and labels — the part where a mistake becomes a leak."""

from uuid import UUID, uuid4

import pytest

from app.features.auth.access.permissions import CATALOGUE
from app.features.auth.service import AuthService
from conftest import Account

pytestmark = pytest.mark.asyncio


async def test_permissions_come_from_the_database(account: Account) -> None:
    """`admin` holds the whole catalogue and `member` holds two entries, and neither
    set is written down in the application: both are read from `role_permissions`."""
    admin = await AuthService().profile(account.admin_id, account.tenant_id)
    member = await AuthService().profile(account.member_id, account.tenant_id)

    assert admin.permissions == frozenset(CATALOGUE)
    assert member.permissions == {"query.execute", "query.history.own"}


async def test_the_context_carries_exactly_the_labels_of_the_users_roles(
    account: Account,
) -> None:
    """The single most important assertion in this feature.

    RLS enforces whatever context it is handed, with complete confidence. A policy that
    is correct in every respect, fed a context holding one label too many, produces a
    silent leak that looks like a successful request. So the resolution is checked
    directly rather than inferred from an endpoint returning 200.
    """
    admin = await AuthService().profile(account.admin_id, account.tenant_id)
    member = await AuthService().profile(account.member_id, account.tenant_id)

    assert admin.context.tenant_id == account.tenant_id
    assert set(admin.context.label_ids) == {
        account.finance_label,
        account.hr_label,
        account.default_label,
        # `admin` alone reaches the quarantine label, and that asymmetry is the feature:
        # it is what lets an unfiled upload be readable by somebody who can classify it
        # without being readable by the whole tenant. See migration 0017.
        account.quarantine_label,
    }
    # `member` reaches the tenant default and nothing else — in particular *not* the
    # quarantine label. Both system roles hold the default because that is where the
    # classifier files a document when it declines, and a default no role reaches would
    # store every such document invisible to everyone who could fix it.
    assert set(member.context.label_ids) == {account.default_label}


async def test_two_roles_give_the_union_of_their_labels(
    account: Account, extra_label_for_member: UUID
) -> None:
    member = await AuthService().profile(account.member_id, account.tenant_id)

    assert set(member.context.label_ids) == {extra_label_for_member, account.default_label}


async def test_a_user_of_another_tenant_resolves_to_nothing(account: Account) -> None:
    """The tenant travels in the token, so the pairing has to be checked somewhere.

    Asking for a real user under a foreign tenant must not return their permissions:
    RLS makes the user invisible, so the joins find no roles at all.
    """
    profile = await AuthService().profile(account.admin_id, uuid4())

    assert profile.permissions == frozenset()
    assert profile.context.label_ids == ()
