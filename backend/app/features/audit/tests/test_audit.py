"""The record of who changed access, and the property that makes it worth having.

Every other test in this suite asks whether the system does the right thing. These ask
whether the system can be *shown* to have done it, months later, to somebody who does not
trust us — which is a different question and the one an enterprise buyer actually asks.

The load-bearing test is `test_the_application_cannot_erase_a_record`. Everything else here
is about writing good rows; that one is about whether the rows mean anything.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.database import owner_session, tenant_session
from app.features.audit.service import AUTOMATIC, AuditService, record, record_system
from app.features.auth.service import AccessProfile
from app.features.tenancy.context import TenantContext
from conftest import Account

pytestmark = pytest.mark.asyncio


def profile_for(account: Account) -> AccessProfile:
    return AccessProfile(
        user_id=account.admin_id,
        context=TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        permissions=frozenset({"audit.read"}),
        email=account.admin_email,
    )


async def rows(tenant_id: UUID) -> list[dict[str, object]]:
    async with owner_session() as session:
        result = await session.execute(
            text(
                "SELECT action, actor_email, target_name, details FROM audit_events "
                "WHERE tenant_id = :t ORDER BY created_at"
            ),
            {"t": tenant_id},
        )
        # `mappings()` rather than `row._mapping`: the same rows, through the public API.
        return [dict(row) for row in result.mappings()]


class TestTheRecordCannotBeRewritten:
    """The whole argument. An audit log the application can edit is not evidence.

    A reviewer's second question, after "who granted this", is "could that row have been
    changed afterwards" — and a comment in a service saying we would never do that is not
    an answer. Migration 0015 answers it in the grant table, where no future bug can reach.
    """

    async def test_the_application_cannot_erase_a_record(self, account: Account) -> None:
        await record(profile_for(account), "role.created", target_name="Auditors")

        async with tenant_session(profile_for(account).context) as session:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text("DELETE FROM audit_events"))

    async def test_the_application_cannot_alter_a_record(self, account: Account) -> None:
        await record(profile_for(account), "role.created", target_name="Auditors")

        async with tenant_session(profile_for(account).context) as session:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await session.execute(text("UPDATE audit_events SET actor_email = 'someone.else'"))

    async def test_but_it_can_still_append(self, account: Account) -> None:
        # The grant is INSERT and SELECT. Narrowing it further would stop the log being
        # written at all, which is the failure mode this is not.
        await record(profile_for(account), "role.created", target_name="First")
        await record(profile_for(account), "role.created", target_name="Second")

        assert [row["target_name"] for row in await rows(account.tenant_id)] == ["First", "Second"]


class TestWhatIsWorthRecording:
    async def test_a_permission_change_keeps_both_sides(self, account: Account) -> None:
        # "Who has access to what" is answered by the difference. Storing only the result
        # makes an auditor reconstruct it from a chain of end-states, which is precisely
        # the work this table exists to spare them.
        await record(
            profile_for(account),
            "role.permissions_set",
            target_type="role",
            target_name="Analyst",
            before=["query.execute"],
            after=["query.execute", "documents.upload"],
        )

        details = (await rows(account.tenant_id))[0]["details"]
        assert details == {
            "before": ["query.execute"],
            "after": ["query.execute", "documents.upload"],
        }

    async def test_the_actor_is_named_by_address_not_only_by_id(self, account: Account) -> None:
        # The id is a foreign key that goes NULL when the user is deleted. The address is
        # copied in, so a departed administrator's actions still say who took them — which
        # is exactly the case the record exists for.
        await record(profile_for(account), "group.created", target_name="Finance")

        assert (await rows(account.tenant_id))[0]["actor_email"] == account.admin_email

    async def test_the_action_is_a_token_rather_than_a_sentence(self, account: Account) -> None:
        # Prose in the log would be a log nothing can filter, count or alert on, and it
        # would have to be translated at read time by whoever wanted to.
        await record(profile_for(account), "group.members_set", target_name="Finance")

        assert (await rows(account.tenant_id))[0]["action"] == "group.members_set"


class TestIsolation:
    async def test_one_tenant_never_reads_another_trail(self, account: Account) -> None:
        # The audit log is the most revealing table in the product — it names people and
        # what they were given. It gets the same policy as everything else, and this is the
        # test that says so rather than assuming it because the column is there.
        stranger = TenantContext.for_tenant(uuid4(), [])
        await record(profile_for(account), "role.created", target_name="Ours")

        async with tenant_session(stranger) as session:
            found = await session.scalar(text("SELECT count(*) FROM audit_events"))

        assert found == 0

    async def test_the_reader_sees_their_own(self, account: Account) -> None:
        await record(profile_for(account), "role.created", target_name="Ours")

        page = await AuditService(profile_for(account)).page()

        assert [event.target_name for event in page.events] == ["Ours"]


class TestFailureDoesNotUndoTheChange:
    async def test_a_broken_write_is_swallowed(self, account: Account) -> None:
        """The grant already committed. Raising here would roll back a change that worked.

        An administrator whose permission edit fails because a log table is full is a worse
        product *and* a worse security posture — people route around tools that refuse to
        work. The failure is logged loudly instead.
        """
        broken = AccessProfile(
            user_id=account.admin_id,
            # A tenant that does not exist: the foreign key will refuse the row.
            context=TenantContext.for_tenant(uuid4(), []),
            permissions=frozenset(),
            email=account.admin_email,
        )

        await record(broken, "role.created", target_name="Doomed")  # must not raise


class TestSystemEvents:
    async def test_a_purge_is_recorded_against_the_organisation_it_ended(
        self, account: Account
    ) -> None:
        # Written through `platform_session`, because a system administrator acting on
        # somebody else's organisation is the one case ordinary RLS exists to prevent.
        await record_system(
            account.admin_id,
            account.admin_email,
            "tenant.purged",
            tenant_id=account.tenant_id,
            target_name="Acme",
        )

        recorded = await rows(account.tenant_id)
        assert [row["action"] for row in recorded] == ["tenant.purged"]
        assert recorded[0]["target_name"] == "Acme"

    async def test_the_record_outlives_the_organisation(self, account: Account) -> None:
        """`tenant_id` is `ON DELETE SET NULL`, and that choice is the whole point.

        Purging is the one irreversible action in the product. A CASCADE here would destroy
        the evidence of the destruction along with it — the row saying who ordered the purge
        would be the first casualty of the purge.
        """
        # A throwaway organisation, not the shared fixture's: this test destroys what it
        # names, and taking the other tests' tenant with it would make the failure look
        # like a bug somewhere else entirely.
        name = f"Doomed {uuid4()}"
        async with owner_session() as session:
            doomed = UUID(
                str(
                    await session.scalar(
                        text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
                    )
                )
            )

        await record_system(
            account.admin_id,
            account.admin_email,
            "tenant.purged",
            tenant_id=doomed,
            target_name=name,
        )

        async with owner_session() as session:
            await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": doomed})

        # A second session, so the count is read after the delete has committed rather than
        # from inside the transaction that performed it.
        async with owner_session() as session:
            surviving = await session.scalar(
                text("SELECT count(*) FROM audit_events WHERE target_name = :n"), {"n": name}
            )

        assert surviving == 1


async def test_the_classifier_leaves_a_record(account: Account) -> None:
    """A change to who may read a document, made by no person.

    Every *human* label change was recorded and the automatic one was not, so the reach of the
    trail stopped exactly where automation began — on a product whose audit story is "changes
    to who may read what". A model that quietly moved a document out of an administrator-only
    label and into a compartment left nothing behind.
    """
    from app.features.ingestion.classification import Filing, Outcome
    from app.features.ingestion.pipeline import IngestionPipeline

    class Declining:
        async def file(self, *_args: object, **_kwargs: object) -> Filing:
            return Filing([], Outcome.DECLINED)

    async with owner_session() as session:
        document_id = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:t, 'unfiled.pdf', :sha, 10, 'classifying') RETURNING id"
            ),
            {"t": account.tenant_id, "sha": str(uuid4())},
        )
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document_id, "l": account.quarantine_label},
        )

    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=Declining(),  # type: ignore[arg-type]
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    events = await rows(account.tenant_id)
    classified = [event for event in events if event["action"] == "document.classified"]
    assert len(classified) == 1
    # No person as the actor: the uploader chose nothing, so borrowing their id would record a
    # decision they did not make. They are context, and go in the details.
    assert classified[0]["actor_email"] == AUTOMATIC


async def test_every_route_that_changes_access_records_an_event() -> None:
    """The gap this closes, stated as a rule rather than four fixes.

    `set_role_labels` — which compartments a role reaches — was the one operation on the label
    router with no entry, while *deleting* a label had one. That is the change an auditor asks
    about first. `merge_labels` moves documents between compartments and can widen visibility;
    it had none either. And the model configuration decides where a tenant's passages are sent
    for generation.

    Asserted over the source rather than by exercising each endpoint: the property is "no
    mutating route is missing a `record` call", and a test that listed the routes it knew
    about would pass the day somebody adds the fifth.
    """
    import re
    from pathlib import Path

    #: Nothing is written, so nothing is recorded. The only exemption, and it is named.
    WRITES_NOTHING = {"suggest_labels"}

    root = Path(__file__).resolve().parents[3] / "features"
    missing: list[str] = []
    for router in root.rglob("router.py"):
        source = router.read_text()
        for match in re.finditer(
            r"@router\.(post|put|patch|delete)\(.*?\)\n(?:async )?def (\w+)\(.*?(?=\n@router|\Z)",
            source,
            re.S,
        ):
            name, body = match.group(2), match.group(0)
            if name in WRITES_NOTHING or "record(" in body or "record_system(" in body:
                continue
            missing.append(f"{router.parent.name}.{name}")

    # `auth` acts on the caller's own account and `documents` records through its service;
    # both are listed so that a *new* omission stands out rather than joining a crowd.
    expected = {
        "auth.login",
        "auth.refresh",
        "auth.rename",
        "auth.change_password",
        "auth.sign_out_everywhere",
        "auth.redeem_credential",
        "documents.upload_document",
        "documents.delete_document",
        "query.ask",
        "query.ask_streaming",
    }
    assert set(missing) <= expected, f"a route that changes access records nothing: {missing}"


async def test_every_recorded_action_has_a_sentence_in_the_interface() -> None:
    """The two halves of the trail, checked against each other.

    An action token is a stable `subject.verb` read by machines; the screen writes the prose.
    When the two drift, the screen falls back to the token with its dots removed — "label
    merged", "llm config cleared" — which is legible enough that nobody notices, and is how a
    new event ends up looking like a bug report for months.

    Reading the frontend from a Python test is unusual and has a precedent here:
    `test_compose_matches_profiles` checks the same kind of agreement across a file boundary
    the type system does not span. The alternative is a duplicated list, which is the thing
    being guarded against.
    """
    import re
    from pathlib import Path

    repository = Path(__file__).resolve().parents[5]
    trail = repository / "frontend/src/features/admin/audit/AuditTrail.tsx"
    if not trail.exists():  # pragma: no cover - a backend-only checkout
        pytest.skip("the frontend is not present in this checkout")

    described = set(re.findall(r'"([a-z_]+\.[a-z_]+)":', trail.read_text()))

    recorded: set[str] = set()
    for source in (repository / "backend/app").rglob("*.py"):
        if "tests" in source.parts:
            continue
        recorded |= set(
            re.findall(r'record(?:_automatic|_system)?\(\s*[^,]+,\s*"([^"]+)"', source.read_text())
        )

    assert recorded, "the sweep found no recorded actions, which means it stopped working"
    assert recorded <= described, (
        f"recorded but not described on screen: {sorted(recorded - described)}"
    )
