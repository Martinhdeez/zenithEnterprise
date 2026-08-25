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
    Classifier,
    Outcome,
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
    assert filing.outcome is Outcome.UNAVAILABLE


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
