"""Automatic filing, and the three things that keep a language model inside the access model.

The security assertions come first because they are the reason this feature is allowed to
exist at all. A classifier that can widen access is a classifier that can hand a
compartmented document to the whole tenant, and it would do it silently, on a guess.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.llm import BaseLLMProvider, GenerationResponse, GenerationUnavailableError
from app.core.database import owner_session, tenant_session
from app.features.ingestion.classification import (
    MAX_CHOSEN,
    MAX_LABELS,
    Classifier,
    Filing,
    Outcome,
    Ready,
    Refused,
    build,
    read,
)
from app.features.tenancy.context import TenantContext
from conftest import Account


class Replying(BaseLLMProvider):
    """Answers with whatever the test hands it, and remembers what it was shown."""

    name = "replying"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen = ""

    async def complete(self, system: str, user: str) -> GenerationResponse:
        self.seen = user
        return GenerationResponse(text=self.reply, model="test")


class Broken(BaseLLMProvider):
    name = "broken"

    async def complete(self, system: str, user: str) -> GenerationResponse:
        raise GenerationUnavailableError("no model is configured")


def context(account: Account) -> TenantContext:
    return TenantContext.for_tenant(account.tenant_id, [])


async def document(tenant_id: UUID, uploaded_by: UUID | None, label_id: UUID | None = None) -> UUID:
    """A document filed exactly as an upload would file it.

    `label_id` is what the uploader picked; passing the tenant's default is what "picked
    nothing" looks like, since `DocumentService` refuses to store a document with no label
    at all — an empty `label_ids` means visible tenant-wide.
    """
    async with owner_session() as session:
        document_id = UUID(
            str(
                await session.scalar(
                    text(
                        "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, "
                        "status, uploaded_by) VALUES (:t, 'x.pdf', :sha, 10, 'ready', :u) "
                        "RETURNING id"
                    ),
                    {"t": tenant_id, "sha": uuid4().hex + uuid4().hex[:32], "u": uploaded_by},
                )
            )
        )
        if label_id:
            await session.execute(
                text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
                {"d": document_id, "l": label_id},
            )
        return UUID(str(document_id))


# --- the prompt, and what it refuses to read back ------------------------------------


def test_the_model_is_shown_numbers_rather_than_identifiers() -> None:
    """A uuid in a prompt is tokens of nothing to reason about, and an invitation to invent
    one — the same reasoning `generation/prompt.py` records for citations."""
    prompt = build(["finance/invoices", "legal/contracts"], "an invoice")

    assert "[1] finance/invoices" in prompt
    assert "[2] legal/contracts" in prompt


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("2", [2]),
        ("1, 3", [1, 3]),
        ("NONE", []),
        ("none", []),
        ("", []),
        # The failure this prompt actually has: a number naming a folder nobody offered.
        # It is what "invent a folder" looks like once the model has numbers instead of
        # names, and it is dropped exactly as an invented citation marker is.
        ("7", []),
        ("2, 99", [2]),
        # Prose instead of an answer. A small model asked for numbers will sometimes
        # explain itself, and an explanation containing a stray digit must not become a
        # filing decision it did not make.
        ("I think this belongs in finance", []),
        ("2, 2, 2", [2]),
    ],
)
def test_only_numbers_naming_an_offered_folder_survive(reply: str, expected: list[int]) -> None:
    assert read(reply, count=3) == expected


def test_a_model_that_names_everything_is_capped() -> None:
    """A reply listing every folder is hedging, not classifying, and filing a document
    under all of them is the same as not filing it."""
    assert len(read("1,2,3,4,5,6,7,8", count=8)) == MAX_CHOSEN


# --- the security properties ----------------------------------------------------------


async def labels_on(document_id: UUID) -> set[UUID]:
    async with owner_session() as session:
        return set(
            await session.scalars(
                text("SELECT label_id FROM document_labels WHERE document_id = :d"),
                {"d": document_id},
            )
        )


async def test_a_document_the_uploader_labelled_is_never_touched(account: Account) -> None:
    """Not deference to manual choice — there is no rearrangement of somebody's deliberate
    compartments that a guess is allowed to make.

    Labels are a union, so adding one always widens: a document filed under
    `legal/confidential` that also gained `general` would be readable by the whole tenant.
    """
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.finance_label)
    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.finance_label]),
        classifier=Classifier(context(account), provider=Replying("1")),
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    assert await labels_on(document_id) == {account.finance_label}


async def test_filing_replaces_the_quarantine_rather_than_adding_to_it(account: Account) -> None:
    """The arrangement that makes this narrowing instead of widening.

    Labels are a union, so leaving the quarantine label beside the chosen compartment would
    keep every administrator on the document permanently — and `_file` would never look at it
    again, because those are no longer "exactly the quarantine label".
    """
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)
    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=Classifier(context(account), provider=Replying("1")),
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]
    applied = await labels_on(document_id)

    assert account.quarantine_label not in applied
    assert len(applied) == 1


async def test_a_document_somebody_labelled_is_left_alone(account: Account) -> None:
    """Not deference to manual choice: there is no rearrangement of somebody's deliberate
    compartments that a guess is allowed to make. A document carrying anything other than
    exactly the quarantine label has been decided by a person."""
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.hr_label)
    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.hr_label]),
        classifier=Classifier(context(account), provider=Replying("1")),
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    assert await labels_on(document_id) == {account.hr_label}


async def test_a_declined_document_is_released_into_the_default(account: Account) -> None:
    """The model read it and no folder fitted. That is an answer, not a breakdown, so the
    document goes where an unfiled document went before quarantine existed — leaving it
    locked up would punish a working installation for a correct reply."""
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)
    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=Classifier(context(account), provider=Replying("NONE")),
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    assert await labels_on(document_id) == {account.default_label}


async def test_a_broken_model_leaves_the_document_quarantined(account: Account) -> None:
    """The one outcome that keeps a document locked up, and the reason the classifier had to
    start reporting *why* it produced nothing. Releasing it into the tenant default on the
    strength of a timeout would undo the whole point of quarantine — and the status says what
    happened, so an administrator can file it rather than wonder."""
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)
    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=Classifier(context(account), provider=Broken()),
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    assert await labels_on(document_id) == {account.quarantine_label}
    # Read through a context that reaches the quarantine label. `context(account)` holds no
    # labels at all, and a quarantined document is correctly invisible to it — which is the
    # protection working, and would have made this assertion pass against a `None` that meant
    # something else entirely.
    reader = TenantContext.for_tenant(account.tenant_id, [account.quarantine_label])
    async with tenant_session(reader) as session:
        detail = await session.scalar(
            text("SELECT status_detail FROM documents WHERE id = :d"), {"d": document_id}
        )
    assert detail is not None and "administrator" in detail


async def test_it_can_only_choose_labels_the_uploader_reaches(account: Account) -> None:
    """The list is the uploader's own reach, so this cannot file a document under a
    compartment they could not have ticked by hand.

    The member holds only the default label; Finance is the admin's. Offering it would be
    the leak, and the model is never shown it.
    """
    model = Replying("1")
    document_id = await document(account.tenant_id, account.member_id)

    await Classifier(context(account), provider=model).file(document_id, account.member_id, "text")

    assert "Finance" not in model.seen


async def test_it_returns_the_label_the_model_chose(account: Account) -> None:
    model = Replying("1")
    document_id = await document(account.tenant_id, account.admin_id)

    filing = await Classifier(context(account), provider=model).file(
        document_id, account.admin_id, "an invoice"
    )
    applied = filing.labels
    assert filing.outcome is Outcome.CHOSE

    async with tenant_session(context(account)) as session:
        names = list(
            await session.scalars(
                text("SELECT name FROM access_labels WHERE id = ANY(:ids)"),
                {"ids": [str(label_id) for label_id in applied]},
            )
        )
    assert len(applied) == 1
    assert names  # a real label, resolved from the tenant's own set


async def test_an_invented_number_files_nothing(account: Account) -> None:
    """A hallucination is dropped rather than mapped to whatever happens to be at that
    index — and a new label cannot appear, because creating labels is not something this
    code is able to do."""
    model = Replying("99")
    document_id = await document(account.tenant_id, account.admin_id)

    filing = await Classifier(context(account), provider=model).file(
        document_id, account.admin_id, "text"
    )

    assert filing.labels == []
    # Declined, not failed. The model answered; nothing it said named a folder it was shown.
    # Since 0017 the distinction decides whether the document is released into the tenant
    # default or left in quarantine, so it is asserted rather than assumed.
    assert filing.outcome is Outcome.DECLINED


# --- failing safely -------------------------------------------------------------------


async def test_a_model_that_breaks_mid_call_reports_failure(account: Account) -> None:
    """Configured and broken, which since 0017 is the one outcome that keeps a document in
    quarantine: nobody has vouched for it, and a timeout is not a reason to publish it."""
    document_id = await document(account.tenant_id, account.admin_id)

    filing = await Classifier(context(account), provider=Broken()).file(
        document_id, account.admin_id, "text"
    )

    assert filing.labels == []
    assert filing.outcome is Outcome.FAILED


async def test_no_model_configured_is_unavailable_rather_than_failed(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary state, not an error: search works without generation, and ingestion has
    to work without either. It must not read as a failure — if it did, an installation with
    no model would quarantine every document it ever ingested.

    Patched at `provider_for`, which is where resolution lives since it stopped being written
    twice. The injected-provider seam cannot express this case: the point is that there is no
    provider to inject.
    """
    from app.features.ingestion import classification

    document_id = await document(account.tenant_id, account.admin_id)

    async def unconfigured(_context: object) -> object:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(classification, "provider_for", unconfigured)

    filing = await Classifier(context(account)).file(document_id, account.admin_id, "text")

    assert filing.labels == []
    assert filing.outcome is Outcome.UNAVAILABLE


async def test_a_document_whose_uploader_is_gone_is_left_alone(account: Account) -> None:
    """`uploaded_by` is `ON DELETE SET NULL`. With no uploader there is no reach to draw a
    list from, and falling back to the tenant's whole label set would file the document
    under compartments nobody chose."""
    document_id = await document(account.tenant_id, None)

    filing = await Classifier(context(account), provider=Replying("1")).file(
        document_id, None, "text"
    )

    assert filing.labels == []
    # Nobody was asked, so it is not a failure — the document is released into the default.
    #
    # `NO_FOLDERS` rather than `UNAVAILABLE`, and the inequality is the point: a working
    # model is injected here. An installation reporting "no model configured" for a document
    # whose uploader was deleted sends an operator to a connector that is answering every
    # other request in the product.
    assert filing.outcome is Outcome.NO_FOLDERS
    assert filing.outcome is not Outcome.UNAVAILABLE


async def test_filing_writes_labels_that_reach_the_chunks(account: Account) -> None:
    """The reason filing happens after `_persist`: the sync triggers are what carry a
    label from `document_labels` down to the chunks, and they only reach chunks that
    exist."""
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.default_label)
    async with owner_session() as session:
        await session.execute(
            text(
                "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                "char_end, text) VALUES (:d, :t, 1, 0, 10, 'an invoice')"
            ),
            {"d": document_id, "t": account.tenant_id},
        )

    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.default_label]),
        classifier=Classifier(context(account), provider=Replying("1")),
    )
    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    async with owner_session() as session:
        document_labels = list(
            await session.scalars(
                text("SELECT label_ids FROM documents WHERE id = :d"), {"d": document_id}
            )
        )
        chunk_labels = list(
            await session.scalars(
                text("SELECT label_ids FROM chunks WHERE document_id = :d"), {"d": document_id}
            )
        )

    # Whatever the document ended up carrying, the chunks carry the same. A chunk that
    # disagrees with its document is a passage retrievable by somebody who cannot open the
    # file it came from.
    assert document_labels[0], "the classifier's choice reached the document"
    assert chunk_labels[0] == document_labels[0]


# --- the reserved labels ---------------------------------------------------------------


async def test_the_quarantine_label_is_never_offered_to_the_model(account: Account) -> None:
    """The leak that reached through the mechanism built to prevent it.

    An administrator reaches `Unclassified`, so without an exclusion the model could pick the
    label the document is already waiting in. `_file` would then insert a row that already
    exists — a no-op — and delete the quarantine label afterwards, leaving `label_ids = '{}'`.
    That is the one value meaning *visible to the whole tenant*, and it is exactly the state
    migration 0017 exists to make unreachable.
    """
    offered = await Classifier(context(account))._candidates(account.admin_id)  # type: ignore[reportPrivateUsage]

    assert account.quarantine_label not in [label_id for label_id, _ in offered.folders]
    assert offered.folders, "the admin reaches real compartments, so the list is not empty"
    assert offered.refusal is None, "and no refusal travels with a non-empty list"


async def test_the_default_label_is_never_offered_either(account: Account) -> None:
    """`_file` already sends a declined document there.

    Offering it as a choice would make "the model picked General" and "the model picked
    nothing" indistinguishable in the log, and one of those is a working classifier.
    """
    offered = await Classifier(context(account))._candidates(account.admin_id)  # type: ignore[reportPrivateUsage]

    assert account.default_label not in [label_id for label_id, _ in offered.folders]


async def test_filing_refuses_the_quarantine_label_even_if_it_arrives(account: Account) -> None:
    """The second lock on the same door.

    `_candidates` no longer offers it; this asserts the *write* refuses it too. A guess must
    not be able to empty a document's label set through any path, and the two ends are far
    enough apart that one of them changing without the other is exactly how this would come
    back.
    """
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)

    class PicksQuarantine:
        async def file(self, *_args: object, **_kwargs: object) -> Filing:
            return Filing([account.quarantine_label], Outcome.CHOSE)

    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=PicksQuarantine(),  # type: ignore[arg-type]
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    # Released to the default, as a document nothing was chosen for — and emphatically not
    # left with an empty array.
    assert await labels_on(document_id) == {account.default_label}


# --- the same four endings, offered rather than applied ---------------------------------
#
# `suggest` is `file` without the write, and until this it threw the ending away and handed
# back the labels alone. Three of the four endings have no labels, so the staging area
# received one value for three different facts and had to guess which — and it guessed that
# an empty answer meant the model had read the document and declined, which under `FAILED`
# is the opposite of what happens to the document.
#
# One test per ending, named for the ending, because "the field is present" is a weaker
# question than "these two cases are still told apart".


async def test_a_suggestion_the_model_made_says_it_chose(account: Account) -> None:
    suggestion = await Classifier(context(account), provider=Replying("1")).suggest(
        account.admin_id, "an invoice"
    )

    assert suggestion.outcome is Outcome.CHOSE
    assert len(suggestion.labels) == 1


async def test_a_suggestion_the_model_declined_is_not_a_failure(account: Account) -> None:
    """The model read it and no folder fitted. Nothing to suggest, and that is an answer:
    on the ingestion path this is what releases a document into the tenant default."""
    suggestion = await Classifier(context(account), provider=Replying("NONE")).suggest(
        account.admin_id, "a birthday card"
    )

    assert suggestion.outcome is Outcome.DECLINED
    assert suggestion.labels == []


async def test_a_suggestion_with_no_model_configured_is_unavailable(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented behaviour of `POST /labels/suggest`: an installation without
    generation still uploads documents, and must not be told anything broke.

    Patched at `provider_for`, since the point is that there is no provider to inject.
    """
    from app.features.ingestion import classification

    async def unconfigured(_context: object) -> object:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(classification, "provider_for", unconfigured)

    suggestion = await Classifier(context(account)).suggest(account.admin_id, "an invoice")

    assert suggestion.outcome is Outcome.UNAVAILABLE
    assert suggestion.labels == []


async def test_a_suggestion_whose_model_broke_says_failed_and_not_declined(
    account: Account,
) -> None:
    """The ending the interface was lying about.

    Configured and broken. It comes back with no labels, exactly like a decline — and if the
    client cannot tell the two apart it says `no match — server will file it` about a
    document that ingestion will leave in a quarantine label only `admin` reaches. The
    inequality is asserted rather than implied: it is the whole defect.
    """
    suggestion = await Classifier(context(account), provider=Broken()).suggest(
        account.admin_id, "an invoice"
    )

    assert suggestion.outcome is Outcome.FAILED
    assert suggestion.outcome is not Outcome.DECLINED
    assert suggestion.labels == []


# --- and "nobody was asked" was three facts wearing one name ----------------------------
#
# The same defect again, one level further down. `UNAVAILABLE` meant the installation has no
# model, *and* this person reaches no folder that could be offered, *and* this person reaches
# more folders than a model can weigh. One of those is fixed by configuring a connector; the
# other two are not, and an operator shown the first sentence goes and inspects a connector
# that is working. That is the entire cost, and it is the same argument that split
# `DECLINED` from `FAILED`.
#
# One test per value, named for what the value means, and each asserting it is *not* the
# others: the inequality is the bug, so the inequality is what gets pinned. A test that only
# read `outcome is NO_FOLDERS` would still pass on the day somebody folds them back together
# behind an alias.


async def a_user_reaching(account: Account, labels: int) -> UUID:
    """A user in a role of its own, holding exactly `labels` fresh compartments.

    Its own role rather than the seeded ones, because the seeded roles reach the default and
    quarantine labels and this needs to control the count exactly — the ceiling is measured
    against the uploader's reach, not against the tenant's label table.
    """
    from app.features.auth.model import Role
    from app.features.auth.onboarding.provisioning import create_user
    from app.features.labels.model import AccessLabel, RoleLabel
    from conftest import PASSWORD

    async with owner_session() as session:
        role = Role(tenant_id=account.tenant_id, name=f"reach-{uuid4()}")
        session.add(role)
        await session.flush()
        for index in range(labels):
            label = AccessLabel(tenant_id=account.tenant_id, name=f"Reach {index} {uuid4()}")
            session.add(label)
            await session.flush()
            session.add(RoleLabel(role_id=role.id, label_id=label.id))
        user = await create_user(
            session, account.tenant_id, f"reach-{uuid4()}@example.com", PASSWORD, [role.id]
        )
        return user.id


async def test_an_uploader_who_reaches_no_label_says_no_folders(account: Account) -> None:
    """A statement about that person, not about the installation.

    A working model is injected, so nothing here is unavailable in any sense an operator
    could act on by configuring a connector. The remedy is to grant this person a
    compartment, and the ending has to say so or nobody will look there.
    """
    uploader = await a_user_reaching(account, labels=0)

    filing = await Classifier(context(account), provider=Replying("1")).file(
        None, uploader, "an invoice"
    )

    assert filing.labels == []
    assert filing.outcome is Outcome.NO_FOLDERS
    # The three it must not be confused with. `UNAVAILABLE` is the expensive one — it names
    # the installation — but `TOO_MANY_FOLDERS` has the opposite remedy, and `FAILED` would
    # quarantine a document nothing is wrong with.
    assert filing.outcome is not Outcome.UNAVAILABLE
    assert filing.outcome is not Outcome.TOO_MANY_FOLDERS
    assert filing.outcome is not Outcome.FAILED
    assert filing.outcome is not Outcome.DECLINED


async def test_an_uploader_reaching_only_reserved_labels_says_no_folders_too(
    account: Account,
) -> None:
    """The sub-case that survived both guards and was invisible.

    The member holds the default label and nothing else. That is a non-empty reach well under
    the ceiling, so neither check fires — and `_candidates` then filters the default out,
    because offering it would make "the model picked General" and "the model picked nothing"
    indistinguishable. The list arrives empty for a third reason.

    It is not hypothetical: four of the six tenants in `eval/label-shortlist.json` record
    `offerable_labels: 0`.
    """
    filing = await Classifier(context(account), provider=Replying("1")).file(
        None, account.member_id, "an invoice"
    )

    assert filing.labels == []
    assert filing.outcome is Outcome.NO_FOLDERS
    assert filing.outcome is not Outcome.UNAVAILABLE
    assert filing.outcome is not Outcome.TOO_MANY_FOLDERS
    assert filing.outcome is not Outcome.FAILED


async def test_a_reach_above_the_ceiling_says_too_many_folders(account: Account) -> None:
    """The model is present and working; the list is too long for it to choose well.

    And this ending is permanent. `eval/label-shortlist.json` scored the embedding shortlist
    that would have lifted `MAX_LABELS` and recommended against adopting it — micro recall@25
    of 0.1818 at a pool of 2000, needing k=1822 to keep 95% of the labels a human chose — so
    a large tenant gets no automatic filing for good. An ending nobody is going to remove is
    an ending worth naming.

    Emphatically not `NO_FOLDERS`: there are folders, and the remedy is the opposite one.
    Granting this person more reach makes it worse.
    """
    uploader = await a_user_reaching(account, labels=MAX_LABELS + 1)

    filing = await Classifier(context(account), provider=Replying("1")).file(
        None, uploader, "an invoice"
    )

    assert filing.labels == []
    assert filing.outcome is Outcome.TOO_MANY_FOLDERS
    assert filing.outcome is not Outcome.NO_FOLDERS
    assert filing.outcome is not Outcome.UNAVAILABLE
    assert filing.outcome is not Outcome.FAILED
    assert filing.outcome is not Outcome.DECLINED


async def test_one_label_under_the_ceiling_is_still_offered(account: Account) -> None:
    """The other side of the boundary, so `TOO_MANY_FOLDERS` cannot quietly become the
    answer for everybody. Exactly `MAX_LABELS` reachable folders is a list the model is
    shown."""
    uploader = await a_user_reaching(account, labels=MAX_LABELS)

    filing = await Classifier(context(account), provider=Replying("1")).file(
        None, uploader, "an invoice"
    )

    assert filing.outcome is Outcome.CHOSE
    assert len(filing.labels) == 1


async def test_only_a_missing_model_says_unavailable(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inequality read from the other end.

    `UNAVAILABLE` is the one word that accuses the installation, so it has to be worth
    accusing it. Here there really is no model and the uploader's reach is fine; the two
    tests above are the same claim with the two halves swapped, and together they are what
    stops the value from drifting back into meaning "something was missing".
    """
    from app.features.ingestion import classification

    async def unconfigured(_context: object) -> object:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(classification, "provider_for", unconfigured)

    filing = await Classifier(context(account)).file(None, account.admin_id, "an invoice")

    assert filing.outcome is Outcome.UNAVAILABLE
    assert filing.outcome is not Outcome.NO_FOLDERS
    assert filing.outcome is not Outcome.TOO_MANY_FOLDERS


async def test_a_suggestion_for_someone_with_no_folders_does_not_blame_the_installation(
    account: Account,
) -> None:
    """The staging area is where a person reads this, and the two sentences are different
    actions: "ask an administrator for access to a folder" against "tell whoever runs this
    that the model is not configured". The second is wrong here and unactionable by them."""
    uploader = await a_user_reaching(account, labels=0)

    suggestion = await Classifier(context(account), provider=Replying("1")).suggest(
        uploader, "an invoice"
    )

    assert suggestion.labels == []
    assert suggestion.outcome is Outcome.NO_FOLDERS
    assert suggestion.outcome is not Outcome.UNAVAILABLE
    assert suggestion.outcome is not Outcome.DECLINED


async def test_a_suggestion_above_the_ceiling_says_so_rather_than_declining(
    account: Account,
) -> None:
    """`DECLINED` would be the worst reading of this one — it claims the model looked at the
    document and found no folder that fits, when it was never shown the list at all, and a
    person told "no match" about a taxonomy too large to search has been told nothing."""
    uploader = await a_user_reaching(account, labels=MAX_LABELS + 1)

    suggestion = await Classifier(context(account), provider=Replying("1")).suggest(
        uploader, "an invoice"
    )

    assert suggestion.labels == []
    assert suggestion.outcome is Outcome.TOO_MANY_FOLDERS
    assert suggestion.outcome is not Outcome.DECLINED
    assert suggestion.outcome is not Outcome.NO_FOLDERS
    assert suggestion.outcome is not Outcome.UNAVAILABLE


# --- and none of them quarantines a document -------------------------------------------


@pytest.mark.parametrize(
    "outcome", [Outcome.UNAVAILABLE, Outcome.NO_FOLDERS, Outcome.TOO_MANY_FOLDERS]
)
async def test_no_way_of_not_being_asked_quarantines_a_document(
    account: Account, outcome: Outcome
) -> None:
    """The reasoning that had to survive the split, stated as a test.

    `UNAVAILABLE` carried it: an installation without a model is an ordinary, supported
    installation whose documents must not all pile up in a quarantine label only `admin`
    reaches. Splitting one value into three is exactly how that guarantee gets lost for two
    of them, so each is asserted rather than argued. `FAILED` remains the only ending that
    keeps a document where it is, and it has its own test above.
    """
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)

    class NotAsked:
        async def file(self, *_args: object, **_kwargs: object) -> Filing:
            return Filing([], outcome)

    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=NotAsked(),  # type: ignore[arg-type]
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    assert await labels_on(document_id) == {account.default_label}
    assert account.quarantine_label not in await labels_on(document_id)


#: The distinguishing phrase of each note, and they have to stay disjoint — three sentences
#: that differ only in wording would be the same collapse written out longhand.
NOTES = {
    Outcome.UNAVAILABLE: "no classification model is configured",
    Outcome.NO_FOLDERS: "reaches no labels",
    Outcome.TOO_MANY_FOLDERS: f"more than {MAX_LABELS} labels",
}


@pytest.mark.parametrize("outcome", list(NOTES))
async def test_each_way_of_not_being_asked_writes_its_own_note(
    account: Account, outcome: Outcome
) -> None:
    """Where the collapse was actually paid for.

    All three wrote "no classification model is configured", and for two of them that is
    false — it describes a component the administrator will then go and inspect, find
    healthy, and learn nothing from. The note is the only place the reason reaches a human on
    the ingestion path, so the value existing is not enough: it has to be spent.
    """
    from app.features.ingestion.pipeline import IngestionPipeline

    document_id = await document(account.tenant_id, account.admin_id, account.quarantine_label)

    class NotAsked:
        async def file(self, *_args: object, **_kwargs: object) -> Filing:
            return Filing([], outcome)

    pipeline = IngestionPipeline(
        TenantContext.for_tenant(account.tenant_id, [account.quarantine_label]),
        classifier=NotAsked(),  # type: ignore[arg-type]
    )

    await pipeline._file(document_id, [])  # type: ignore[reportPrivateUsage]

    reader = TenantContext.for_tenant(account.tenant_id, [account.default_label])
    async with tenant_session(reader) as session:
        detail = await session.scalar(
            text("SELECT status_detail FROM documents WHERE id = :d"), {"d": document_id}
        )

    assert detail is not None
    assert NOTES[outcome] in detail
    # And it says none of the others. This is the assertion that would have caught the
    # original defect: every one of the three used to render the first sentence.
    for other, phrase in NOTES.items():
        if other is not outcome:
            assert phrase not in detail


# --- the same three reasons, answered before anybody presses anything --------------------
#
# `Classifier.availability` is the front half of `Classifier.file`: the reach, the reserved
# filter, the ceiling and whether a provider resolves, evaluated once and then consumed by
# the call. So these tests ask two things of it. One per reason, each asserting it is *not*
# the others — the inequality is what has broken repeatedly here, and the three remedies are
# different enough that naming the wrong one is worse than saying nothing. And, at the end,
# that what it predicts is what `suggest` actually reaches; if those two can disagree, this
# is the divergence it was built to prevent rather than the cure.


async def test_the_preflight_says_no_folders_when_the_person_reaches_none(
    account: Account,
) -> None:
    """A working model is injected, so nothing here is unavailable in any sense an operator
    could act on. The remedy is to grant this person a compartment."""
    person = await a_user_reaching(account, labels=0)

    state = await Classifier(context(account), provider=Replying("1")).availability(person)

    assert isinstance(state, Refused)
    assert state.reason is Outcome.NO_FOLDERS
    assert state.reason is not Outcome.UNAVAILABLE
    assert state.reason is not Outcome.TOO_MANY_FOLDERS


async def test_the_preflight_says_no_folders_when_every_label_they_reach_is_reserved(
    account: Account,
) -> None:
    """The state a fresh tenant is actually in, and the reason this endpoint exists.

    The member holds the default label and nothing else: a non-empty reach, well under the
    ceiling, filtered to nothing because neither reserved label is ever offered. Four of the
    six tenants in `eval/label-shortlist.json` record `offerable_labels: 0`, so the button
    would be dead on arrival for most of them — which is the common case, not an edge one.
    """
    state = await Classifier(context(account), provider=Replying("1")).availability(
        account.member_id
    )

    assert isinstance(state, Refused)
    assert state.reason is Outcome.NO_FOLDERS
    assert state.reason is not Outcome.UNAVAILABLE
    assert state.reason is not Outcome.TOO_MANY_FOLDERS


async def test_the_preflight_says_too_many_folders_above_the_ceiling(account: Account) -> None:
    """The model is present and working; the list is too long for it to choose well, and
    `eval/label-shortlist.json` established that the ceiling stays.

    Emphatically not `NO_FOLDERS` — there are folders, and the remedy is the opposite one:
    granting this person more reach makes it worse.
    """
    person = await a_user_reaching(account, labels=MAX_LABELS + 1)

    state = await Classifier(context(account), provider=Replying("1")).availability(person)

    assert isinstance(state, Refused)
    assert state.reason is Outcome.TOO_MANY_FOLDERS
    assert state.reason is not Outcome.NO_FOLDERS
    assert state.reason is not Outcome.UNAVAILABLE


async def test_the_preflight_says_unavailable_only_when_there_is_no_model(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UNAVAILABLE` is the one word that accuses the installation, so it has to be worth
    accusing it. Here there really is no model and the caller's reach is fine — the three
    tests above are the same claim with the halves swapped."""
    from app.features.ingestion import classification

    async def unconfigured(_context: object) -> object:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(classification, "provider_for", unconfigured)

    state = await Classifier(context(account)).availability(account.admin_id)

    assert isinstance(state, Refused)
    assert state.reason is Outcome.UNAVAILABLE
    assert state.reason is not Outcome.NO_FOLDERS
    assert state.reason is not Outcome.TOO_MANY_FOLDERS


async def test_the_preflight_offers_it_when_there_is_a_list_and_a_model(account: Account) -> None:
    """The other side of every refusal above, so none of them can quietly become the answer
    for everybody. A refusal that is always returned would pass all four tests above and hide
    a working button from the entire product."""
    state = await Classifier(context(account), provider=Replying("1")).availability(
        account.admin_id
    )

    assert isinstance(state, Ready)
    assert state.folders, "the admin reaches real compartments"


async def test_exactly_the_ceiling_is_still_offered(account: Account) -> None:
    """The boundary, from the pre-flight's side. `MAX_LABELS` reachable folders is a list."""
    person = await a_user_reaching(account, labels=MAX_LABELS)

    state = await Classifier(context(account), provider=Replying("1")).availability(person)

    assert isinstance(state, Ready)
    assert len(state.folders) == MAX_LABELS


async def test_the_ceiling_is_a_fact_about_the_person_not_the_tenant(account: Account) -> None:
    """The nuance that is easy to get backwards, pinned in one tenant.

    `len(reachable) > MAX_LABELS` measures the reach of whoever would press the button, not
    how many labels the tenant holds. So the same tenant, at the same moment, answers
    differently for two people — and anything the interface says about `too_many_folders` has
    to be a sentence about that person's own reach, not about the organisation's taxonomy.
    """
    wide = await a_user_reaching(account, labels=MAX_LABELS + 1)
    narrow = await a_user_reaching(account, labels=10)
    classifier = Classifier(context(account), provider=Replying("1"))

    refused = await classifier.availability(wide)
    offered = await classifier.availability(narrow)

    assert isinstance(refused, Refused)
    assert refused.reason is Outcome.TOO_MANY_FOLDERS
    assert isinstance(offered, Ready)
    assert len(offered.folders) == 10


async def test_an_unreadable_reach_raises_rather_than_refusing(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the pre-flight deliberately differs from `file`, and why.

    `file` turns a broken `_candidates` into `FAILED`, because filing must never fail an
    ingestion. A pre-flight has the opposite duty: a refusal invented out of an error is
    indistinguishable from a real one, and it would hide a button that works. So it raises,
    the route answers with an error, and the client can tell "I could not ask" apart from
    "the answer is no".

    The second half asserts the difference is only in this direction — `file` still reports
    `FAILED` for the same breakage, so extracting the front half changed nothing about what
    an ingestion does when the database will not answer.
    """
    from app.features.ingestion import classification

    async def broken(_self: object, _user_id: UUID) -> tuple[UUID, ...]:
        raise RuntimeError("the reach could not be read")

    monkeypatch.setattr(classification.UserRepository, "label_ids", broken)
    classifier = Classifier(context(account), provider=Replying("1"))

    with pytest.raises(RuntimeError):
        await classifier.availability(account.admin_id)

    filing = await classifier.file(None, account.admin_id, "an invoice")
    assert filing.outcome is Outcome.FAILED


# --- and the pre-flight agrees with the suggestion it predicts ---------------------------
#
# The test that matters most. A pre-flight that can disagree with the pass it precedes is
# the divergence this was built to prevent, wearing the costume of the cure: the button is
# offered and the pass says `no_folders`, or the button is hidden and the pass would have
# worked. They cannot disagree here because `file` *calls* `availability` and continues from
# what it returns — but "cannot by construction" is exactly the claim that has to be pinned,
# because the construction is one refactor away from being undone.


@pytest.mark.parametrize(
    "scenario", ["reaches nothing", "reaches only reserved labels", "past the ceiling", "no model"]
)
async def test_the_reason_the_preflight_gives_is_the_ending_the_suggestion_reaches(
    account: Account, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Equality, asserted for every reason the pre-flight can give.

    Not "both are falsy" and not a mapping from one vocabulary to another: the pre-flight
    returns the same `Outcome` value the suggestion does, so the assertion is `is`. A
    translation table between two enums would be a third place for this to be got wrong.
    """
    classifier, person = await _scenario(account, monkeypatch, scenario)

    state = await classifier.availability(person)
    suggestion = await classifier.suggest(person, "an invoice for consulting services")

    assert isinstance(state, Refused)
    assert state.reason is suggestion.outcome
    assert suggestion.labels == []


async def test_a_preflight_that_offers_it_never_precedes_one_of_those_three(
    account: Account,
) -> None:
    """The other direction, and the one that decides whether the button lies.

    A pre-flight saying yes must not be followed by a pass that reports one of the three
    endings it exists to predict — that is a person pressing a prominent action and being
    told afterwards that it could never have worked, which is the whole defect. `CHOSE`,
    `DECLINED` and `FAILED` are all legitimate here: they describe how the call went, and
    nothing short of making it can know.
    """
    classifier = Classifier(context(account), provider=Replying("1"))

    state = await classifier.availability(account.admin_id)
    suggestion = await classifier.suggest(account.admin_id, "an invoice for consulting services")

    assert isinstance(state, Ready)
    assert suggestion.outcome not in {
        Outcome.UNAVAILABLE,
        Outcome.NO_FOLDERS,
        Outcome.TOO_MANY_FOLDERS,
    }
    assert suggestion.outcome is Outcome.CHOSE


async def _scenario(
    account: Account, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> tuple[Classifier, UUID]:
    """One caller in one of the four states that refuse, with a model injected wherever the
    model is not the point."""
    if scenario == "reaches nothing":
        return Classifier(context(account), provider=Replying("1")), await a_user_reaching(
            account, labels=0
        )
    if scenario == "reaches only reserved labels":
        return Classifier(context(account), provider=Replying("1")), account.member_id
    if scenario == "past the ceiling":
        return Classifier(context(account), provider=Replying("1")), await a_user_reaching(
            account, labels=MAX_LABELS + 1
        )

    from app.features.ingestion import classification

    async def unconfigured(_context: object) -> object:
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(classification, "provider_for", unconfigured)
    return Classifier(context(account)), account.admin_id
