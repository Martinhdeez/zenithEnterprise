"""Resolution of permissions and labels — the part where a mistake becomes a leak."""

from uuid import UUID, uuid4

import pytest

from app.features.auth.permissions import CATALOGUE
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
    assert admin.context.label_ids == (account.finance_label,)
    # `member` reaches no labels, so it sees only unlabelled documents. The narrow
    # default from F1 has to survive contact with real role resolution.
    assert member.context.label_ids == ()


async def test_two_roles_give_the_union_of_their_labels(
    account: Account, extra_label_for_member: UUID
) -> None:
    member = await AuthService().profile(account.member_id, account.tenant_id)

    assert set(member.context.label_ids) == {extra_label_for_member}


async def test_a_user_of_another_tenant_resolves_to_nothing(account: Account) -> None:
    """The tenant travels in the token, so the pairing has to be checked somewhere.

    Asking for a real user under a foreign tenant must not return their permissions:
    RLS makes the user invisible, so the joins find no roles at all.
    """
    profile = await AuthService().profile(account.admin_id, uuid4())

    assert profile.permissions == frozenset()
    assert profile.context.label_ids == ()
