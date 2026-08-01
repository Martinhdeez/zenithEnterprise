"""The context is a value object, and the tests are about it staying one."""

import dataclasses
from uuid import uuid4

import pytest

from app.features.tenancy.context import TenantContext


def test_context_is_immutable() -> None:
    """A context mutated after the transaction opened would widen visibility without
    the `set_config` calls that already ran ever knowing."""
    context = TenantContext(tenant_id=uuid4())

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.tenant_id = uuid4()  # type: ignore[misc]


def test_labels_are_a_tuple_even_when_built_from_a_list() -> None:
    """A list would be mutable through the reference the caller kept."""
    labels = [uuid4(), uuid4()]
    context = TenantContext.for_tenant(uuid4(), labels)

    labels.append(uuid4())

    assert isinstance(context.label_ids, tuple)
    assert len(context.label_ids) == 2


def test_default_context_reaches_no_labels() -> None:
    """The default is the narrow one: a context built without labels sees only
    unlabelled documents, never everything."""
    context = TenantContext(tenant_id=uuid4())

    assert context.label_ids == ()
    assert not context.reaches(uuid4())


def test_reaches_reports_membership() -> None:
    label = uuid4()
    context = TenantContext.for_tenant(uuid4(), [label])

    assert context.reaches(label)
    assert not context.reaches(uuid4())
