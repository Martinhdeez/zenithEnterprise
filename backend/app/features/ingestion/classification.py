"""Filing a document that arrived with no label, by asking a model to choose from a list.

A bulk migration is where this earns its place: two hundred files dropped into the uploader
at once, nobody willing to tick labels two hundred times, and the alternative is two hundred
documents in the tenant-wide default where nothing is compartmented at all.

Three rules hold it inside the access model rather than beside it.

**It only ever runs on a document with no labels.** Labels are a union — the policy is
`label_ids && zenith_current_labels()`, so holding *any* one of a document's labels opens it.
Adding a label therefore always widens access. An unlabelled document is already visible to
the whole tenant, so a label added to *that* can only narrow, and the classifier is
monotonically narrowing by construction. Let it touch a document that somebody deliberately
filed under `legal/confidential` and it could add `general` beside it and hand the file to
everybody. The check is not a preference about manual overrides winning; it is what stops a
language model from being able to widen access at all.

**It chooses from a list it is given, by number.** The model never sees a label id. It sees
`[1] finance/invoices`, and anything it writes that is not a number in range is discarded —
the same reasoning `generation/prompt.py` records for citations, where a UUID in a prompt is
tokens of nothing to reason about and an invitation to invent one. A hallucinated label
cannot be created here because creating labels is not something this code can do.

**The list is the uploader's own reach.** Resolved through `UserRepository.label_ids`, the
one function that answers that question, so the classifier cannot file a document under a
compartment the person who uploaded it could not have chosen by hand.

Filing never fails an ingestion. It does, however, **say which way it ended**, and since 0017
that distinction carries weight: an upload now waits in a quarantine label that only `admin`
reaches, and what happens next depends on why the classifier produced nothing.

A model that read the document and found no folder that fits has done its job — the document
belongs in the tenant default, which is where it would have gone before any of this existed.
A model that was never configured has not been asked, and an installation without one is an
ordinary, supported installation whose documents must not all pile up in quarantine. A model
that was configured and *broke* is the only case where the document stays where it is: nobody
has vouched for it, so it remains readable by an administrator and by whoever uploaded it,
rather than being released to the tenant on the strength of a timeout.

Collapsing those three into an empty list was safe while "unchanged" meant "tenant-wide". It
is not safe now, and the same shape of bug — several endings, one return value, the caller
guessing — is the one `uploadWatch.ts` was extracted to fix on the other side of the product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import structlog
from sqlalchemy import text

from app.common.llm import BaseLLMProvider
from app.core.database import tenant_session
from app.features.auth.repository import UserRepository
from app.features.generation.connector.resolve import provider_for
from app.features.tenancy.context import TenantContext

log = structlog.get_logger()

#: How much of the document the model reads. Roughly a thousand tokens at four characters
#: each — enough that a contract looks like a contract, and short enough that a hundred-page
#: report costs the same as a one-page invoice.
EXCERPT_CHARACTERS = 4_000

#: Above this, the list stops being something a model can weigh and starts being something
#: it skims. A tenant with more labels than this gets no automatic filing rather than bad
#: filing — the labels are ordered by name, so silently taking the first sixty would file
#: everything under whatever begins with "a".
MAX_LABELS = 60

#: More than this and the model is guessing rather than classifying. A document that is
#: genuinely three things is rare; a model that names three is usually hedging.
MAX_CHOSEN = 3

SYSTEM = """You file documents into an existing set of folders.

You are given a numbered list of folders and the opening of a document. Reply with the
numbers of the folders it belongs in, separated by commas — for example: 2, 5

Rules:
1. Only use numbers from the list. Never invent a folder or suggest a new one.
2. Choose at most three, and only ones that clearly fit.
3. If none of them fit, reply with exactly: NONE
4. Reply with the numbers and nothing else. No explanation, no folder names."""

_NUMBERS = re.compile(r"\d+")


def build(names: list[str], excerpt: str) -> str:
    listed = "\n".join(f"[{number}] {name}" for number, name in enumerate(names, start=1))
    return f'Folders:\n\n{listed}\n\nDocument:\n"""\n{excerpt}\n"""\n\nNumbers:'


def read(reply: str, count: int) -> list[int]:
    """The numbers the model wrote, keeping only those that name a folder it was shown.

    Range-checked rather than trusted. An out-of-range number is the one failure mode this
    prompt has — it is what "invent a folder" looks like once the model has been given
    numbers instead of names — and it is silently dropped, exactly as an invented citation
    marker is.
    """
    if reply.strip().upper().startswith("NONE"):
        return []
    chosen: list[int] = []
    for match in _NUMBERS.findall(reply):
        number = int(match)
        if 1 <= number <= count and number not in chosen:
            chosen.append(number)
    return chosen[:MAX_CHOSEN]


class Outcome(StrEnum):
    """Why the classifier produced what it produced. See the module docstring."""

    #: It read the document and named folders. `Filing.labels` is non-empty.
    CHOSE = "chose"
    #: It read the document and none of the folders fitted. A real answer, just not a label.
    DECLINED = "declined"
    #: It was never asked — no model configured, or no folders to offer it.
    UNAVAILABLE = "unavailable"
    #: It was asked and the call broke. The only outcome that leaves a document quarantined.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Filing:
    labels: list[UUID]
    outcome: Outcome


class Classifier:
    def __init__(self, context: TenantContext, provider: BaseLLMProvider | None = None) -> None:
        self.context = context
        self._provider = provider

    async def file(
        self, document_id: UUID | None, uploaded_by: UUID | None, text_excerpt: str
    ) -> Filing:
        """Choose labels for a document, or explain why it did not. Never raises.

        The caller writes the labels; this decides them, and the split is deliberate —
        everything here is a guess, and the write is not.
        """
        if uploaded_by is None:
            # `documents.uploaded_by` is `ON DELETE SET NULL`, so this is a document whose
            # uploader has since been removed. There is no reach to draw a list from, and
            # inventing one from the tenant's full label set would file the document under
            # compartments nobody chose. Nobody was asked, so it is not a failure.
            return Filing([], Outcome.UNAVAILABLE)

        try:
            candidates = await self._candidates(uploaded_by)
        except Exception as error:  # noqa: BLE001 - filing must never fail an ingestion
            log.warning("classification_failed", document_id=str(document_id), error=str(error))
            return Filing([], Outcome.FAILED)

        if not candidates:
            return Filing([], Outcome.UNAVAILABLE)

        # Resolving the provider is separated from calling it, and that is the whole point of
        # this arrangement: "there is no model configured" and "the model broke" are different
        # facts about the installation, and since 0017 they lead to different access outcomes.
        # Folded into one `try`, an unconfigured installation would look like a broken one and
        # quarantine every document it ever ingests.
        try:
            provider = self._provider or await provider_for(self.context)
        except Exception as error:  # noqa: BLE001
            log.info("classification_unavailable", document_id=str(document_id), error=str(error))
            return Filing([], Outcome.UNAVAILABLE)

        try:
            names = [name for _, name in candidates]
            excerpt = text_excerpt[:EXCERPT_CHARACTERS]
            reply = await provider.complete(SYSTEM, build(names, excerpt))
            chosen = read(reply.text, len(candidates))
        except Exception as error:  # noqa: BLE001 - filing must never fail an ingestion
            log.warning("classification_failed", document_id=str(document_id), error=str(error))
            return Filing([], Outcome.FAILED)

        applied = [candidates[number - 1][0] for number in chosen]
        log.info(
            "classified",
            document_id=str(document_id),
            offered=len(candidates),
            applied=len(applied),
        )
        # An empty `chosen` here is the model answering `NONE`, or writing only numbers that
        # named no folder it was shown. Both are the model declining, not the model failing.
        return Filing(applied, Outcome.CHOSE if applied else Outcome.DECLINED)

    async def suggest(self, user_id: UUID, excerpt: str) -> list[UUID]:
        """The same decision, offered rather than applied.

        For the staging area, where files sit in the browser until somebody confirms them.
        Identical constraints — the caller's own reach, chosen by number, range-checked —
        because a suggestion that could name a label the user does not hold would be a
        disclosure even if nothing were written.

        Separate from `file` only in that it takes the user directly: staging has no
        document yet, so there is no `uploaded_by` to read.

        Returns the labels alone. A suggestion the user can ignore does not need to explain
        why there is nothing to suggest — the four outcomes exist so that an *access*
        decision can be made from them, and staging makes none.
        """
        return (await self.file(document_id=None, uploaded_by=user_id, text_excerpt=excerpt)).labels

    async def _candidates(self, uploaded_by: UUID) -> list[tuple[UUID, str]]:
        """The labels the uploader reaches, by name, ordered so the prompt is stable.

        `UserRepository.label_ids` rather than a query written here: it is the single place
        a person's reach is resolved, it already accounts for both the grant and the
        group-plus-clearance routes, and it is tested directly. A second implementation of
        it would be a second answer to "what may this person see".
        """
        async with tenant_session(self.context) as session:
            reachable = await UserRepository(session).label_ids(uploaded_by)
            if not reachable or len(reachable) > MAX_LABELS:
                return []

            rows = await session.execute(
                text("SELECT id, name FROM access_labels WHERE id = ANY(:ids) ORDER BY name"),
                {"ids": [str(label_id) for label_id in reachable]},
            )
            return [(row.id, row.name) for row in rows]
