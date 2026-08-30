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

**And "it was never asked" was itself three endings wearing one name.** `UNAVAILABLE` covered
the installation having no model, the uploader having no folder that could be offered, and the
uploader reaching more folders than a model can weigh. Same bug, one level further down. They
are not the same fact and they do not have the same remedy:

* No model configured is a statement about the *installation*. An operator fixes it by
  configuring one — and this is what the collapse costs: an operator shown that sentence on an
  installation whose model is fine goes and inspects a connector that is working.
* No folder to offer is a statement about *that person's reach*. They hold no label, or every
  label they hold is a reserved one. Nothing about the installation is wrong. A document whose
  uploader has since been deleted lands here too, because `documents.uploaded_by` is
  `ON DELETE SET NULL` and there is then no reach to draw a list from.
* More folders than `MAX_LABELS` is a statement about the *taxonomy that person reaches*, and
  it is permanent. `eval/label-shortlist.json` measured the shortlist that would have lifted
  the ceiling and recommended against it — micro recall@25 of 0.1818 at a pool of 2000 labels,
  needing k=1822 of 2000 to keep 95% of the labels a human chose — so a wide reach gets no
  automatic filing for good, by decision rather than by omission.

None of the three is a failure and none of them quarantines a document: all three release it
into the tenant default, exactly as before, and `FAILED` remains the only ending that does
not. What changed is that the caller is told which one it was instead of guessing, and the
note written on the document gives the true reason rather than the comfortable one.

**And all three are settled before the model is spoken to, which means they can be answered
in advance.** `Classifier.availability` is that question, and it is the front half of `file`
rather than a copy of it: `file` calls it and continues from what it returns. So the staging
area can ask whether automatic filing is available to *this person* before offering the
button, instead of running a hundred files to write the same note on every row — which on
this installation is the ordinary first experience, since four of the six tenants in
`eval/label-shortlist.json` hold labels and no offerable ones. `CHOSE`, `DECLINED` and
`FAILED` are endings of a call and stay unpredictable; the other three were never about the
call at all.
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
#: it skims. A reach wider than this gets no automatic filing rather than bad filing — the
#: labels are ordered by name, so silently taking the first sixty would file everything under
#: whatever begins with "a".
#:
#: **The ceiling is permanent, and measured rather than assumed.** `eval/label-shortlist.json`
#: scored an embedding shortlist that would have replaced it and recommended against adopting
#: it: at a pool of 2000 labels micro recall@25 is 0.1818, and keeping 95% of the labels a
#: human chose needs k=1822 — a slightly shorter list, not a shortlist. `TOO_MANY_FOLDERS` is
#: therefore an ending the product keeps, not one it is waiting to remove, which is reason
#: enough to say it out loud instead of reporting it as "no model configured".
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
    """Why the classifier produced what it produced. See the module docstring.

    Five of the six carry no labels, so `Filing.labels` cannot tell them apart and no caller
    should try. Exactly one of them — `FAILED` — leaves a document quarantined.
    """

    #: It read the document and named folders. `Filing.labels` is non-empty.
    CHOSE = "chose"
    #: It read the document and none of the folders fitted. A real answer, just not a label.
    DECLINED = "declined"
    #: There is no model to ask. A fact about the *installation*, and an ordinary one: search
    #: works without generation, and ingestion has to work without either.
    UNAVAILABLE = "unavailable"
    #: There was nothing to offer it. A fact about *the uploader's reach* — they hold no
    #: label, or every label they hold is a reserved one — and, because `uploaded_by` is
    #: `ON DELETE SET NULL`, also a document whose uploader has been deleted. The
    #: installation is fine; this person has no folders.
    NO_FOLDERS = "no_folders"
    #: There were too many to offer it: the uploader reaches more than `MAX_LABELS`. The
    #: model is present and working and the list is too long for it to choose well. This one
    #: is permanent — see `MAX_LABELS` and the run that refuted the shortlist.
    TOO_MANY_FOLDERS = "too_many_folders"
    #: It was asked and the call broke. The only outcome that leaves a document quarantined.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Filing:
    labels: list[UUID]
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class Offer:
    """The folders to show the model, or — when there are none — which ending that is.

    `_candidates` used to return a bare list, and an empty one meant three different things:
    the uploader reaches nothing, the uploader reaches only reserved labels, and the uploader
    reaches more than `MAX_LABELS`. That is the module docstring's bug in miniature, so the
    reason travels back beside the list rather than being re-derived by whoever asked.
    """

    folders: list[tuple[UUID, str]]
    #: Why there are none; `None` exactly when `folders` is non-empty. Never `CHOSE`,
    #: `DECLINED` or `FAILED` — those are endings of a *call*, and no call has been made.
    refusal: Outcome | None


@dataclass(frozen=True, slots=True)
class Refused:
    """Nobody can be asked on this person's behalf, and which of the three reasons that is.

    Always one of `UNAVAILABLE`, `NO_FOLDERS` or `TOO_MANY_FOLDERS` — the endings that are
    settled before any call is made, and therefore the only ones knowable in advance. The
    other three describe how a call went and cannot be predicted by anything short of making
    it.
    """

    reason: Outcome


@dataclass(frozen=True, slots=True)
class Ready:
    """It can be asked: this list, that model.

    Holding both is what makes `Classifier.availability` usable as the front half of
    `Classifier.file` rather than a second opinion beside it. A pre-flight that recomputed
    the same predicate would be free to disagree with the call it predicts; this one cannot,
    because the call is the thing that consumes it.
    """

    folders: list[tuple[UUID, str]]
    provider: BaseLLMProvider


#: Whether automatic filing can be offered to one person at all. See `Classifier.availability`.
Availability = Refused | Ready


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
            # compartments nobody chose. Nobody was asked, so it is not a failure — and it is
            # not `UNAVAILABLE` either: the installation's model, if it has one, is fine, and
            # saying otherwise sends an operator to inspect a working connector.
            return Filing([], Outcome.NO_FOLDERS)

        # `availability` is the front half of this method, not a second opinion about it.
        # Everything decided before the model is spoken to is decided there, once.
        try:
            state = await self.availability(uploaded_by)
        except Exception as error:  # noqa: BLE001 - filing must never fail an ingestion
            log.warning("classification_failed", document_id=str(document_id), error=str(error))
            return Filing([], Outcome.FAILED)

        if isinstance(state, Refused):
            # Nothing to ask about, and *which* nothing decides what an administrator does
            # next: widen somebody's reach, or accept a ceiling that measurement says stays.
            log.info(
                "classification_not_offered",
                document_id=str(document_id),
                outcome=state.reason.value,
            )
            return Filing([], state.reason)
        candidates = state.folders

        try:
            names = [name for _, name in candidates]
            excerpt = text_excerpt[:EXCERPT_CHARACTERS]
            reply = await state.provider.complete(SYSTEM, build(names, excerpt))
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

    async def availability(self, user_id: UUID) -> Availability:
        """Whether this person can be offered automatic filing at all — before they ask.

        Everything `file` settles before the model is spoken to, settled here and nowhere
        else: the uploader's reach, the reserved-label filter, the ceiling, and whether there
        is a model to resolve. `file` then *continues* from what this returns, so the two
        cannot hold different opinions about the same person — there is one opinion, and the
        call consumes it.

        That is the whole reason this is a method on the classifier rather than a rule the
        client evaluates. "Offerable" is `NOT is_quarantine AND NOT is_default`, `MAX_LABELS`
        is 60, and a browser that knew both would still be a second implementation of a
        predicate this repository has already watched drift once. The client asks; it does not
        re-derive.

        **It answers about a person, never about a tenant.** The ceiling measures the reach of
        the individual who would press the button, so in one tenant an administrator reaching
        two hundred labels is refused while a member reaching ten is offered a list.

        Raises where `file` returns `FAILED`: an unreadable reach is a broken request, not a
        prediction, and a caller that cannot answer the question must not answer it with a
        refusal. `FAILED`, `CHOSE` and `DECLINED` are endings of a call and can never be
        returned here — nothing has been called.
        """
        offer = await self._candidates(user_id)
        if offer.refusal is not None:
            return Refused(offer.refusal)

        # Resolving the provider is separated from calling it, and that is the whole point of
        # this arrangement: "there is no model configured" and "the model broke" are different
        # facts about the installation, and since 0017 they lead to different access outcomes.
        # Folded into one `try`, an unconfigured installation would look like a broken one and
        # quarantine every document it ever ingests.
        #
        # This is the *only* site that reports `UNAVAILABLE`, and that is what makes the word
        # true: it says the installation has no model, and nothing else says it. It is also
        # why a pre-flight has to resolve a provider rather than assume one — the answer is
        # per tenant, read from `llm_config`.
        try:
            provider = self._provider or await provider_for(self.context)
        except Exception as error:  # noqa: BLE001
            log.info("classification_unavailable", error=str(error))
            return Refused(Outcome.UNAVAILABLE)

        return Ready(offer.folders, provider)

    async def suggest(self, user_id: UUID, excerpt: str) -> Filing:
        """The same decision, offered rather than applied.

        For the staging area, where files sit in the browser until somebody confirms them.
        Identical constraints — the caller's own reach, chosen by number, range-checked —
        because a suggestion that could name a label the user does not hold would be a
        disclosure even if nothing were written.

        Separate from `file` only in that it takes the user directly: staging has no
        document yet, so there is no `uploaded_by` to read.

        **Returns the whole `Filing`, outcome included.** It used to return the labels alone,
        on the reasoning that a suggestion the user can ignore need not explain why there is
        nothing to suggest — that the outcomes existed so an *access* decision could be made
        from them, and staging makes none.

        That was wrong about what staging *says*, not about what it decides. The staging row
        read `no match — server will file it` whenever the suggestion came back empty, and
        under `FAILED` that is the opposite of what happens: nothing vouched for the
        document, so ingestion leaves it in a quarantine label only `admin` reaches, and the
        person was told it had been filed. Three endings collapsed into an empty list is
        exactly the bug the module docstring above describes — the caller guessing — and the
        guess it made was the untrue one.
        """
        return await self.file(document_id=None, uploaded_by=user_id, text_excerpt=excerpt)

    async def _candidates(self, uploaded_by: UUID) -> Offer:
        """The labels the uploader reaches, by name, ordered so the prompt is stable.

        `UserRepository.label_ids` rather than a query written here: it is the single place
        a person's reach is resolved, it already accounts for both the grant and the
        group-plus-clearance routes, and it is tested directly. A second implementation of
        it would be a second answer to "what may this person see".

        **It returns why the list is empty, not merely that it is.** `if not reachable or
        len(reachable) > MAX_LABELS: return []` folded two unrelated statements — this person
        holds nothing, this person holds too much — into one value, and a third arrived
        underneath them: a reach made entirely of reserved labels, which passes both checks
        and is then filtered to nothing by the query below. That third one is not
        hypothetical. Four of the six tenants in `eval/label-shortlist.json` record
        `offerable_labels: 0`.
        """
        async with tenant_session(self.context) as session:
            reachable = await UserRepository(session).label_ids(uploaded_by)
            if not reachable:
                # No label at all. Nothing is wrong with the installation; this person has
                # simply not been granted a compartment.
                return Offer([], Outcome.NO_FOLDERS)
            if len(reachable) > MAX_LABELS:
                # The ceiling, and it is a decision rather than a gap. Note it measures the
                # *uploader's reach*, not the tenant's label count: in one tenant an
                # administrator reaching two hundred labels is refused while a member
                # reaching ten is filed normally, and an operator told "no model configured"
                # for the first has no way to discover that.
                return Offer([], Outcome.TOO_MANY_FOLDERS)

            # **Reserved labels are never offered**, and the quarantine one is why this
            # clause exists at all. An administrator reaches `Unclassified`, so without it the
            # model could pick the label the document is already waiting in — and `_file`
            # would then insert a row that already exists and delete the quarantine label
            # afterwards, leaving `label_ids = '{}'`, which is the one value that means
            # *visible to the whole tenant*. The exact outcome migration 0017 exists to
            # prevent, reached through the mechanism meant to prevent it.
            #
            # The default label is excluded for a quieter reason: `_file` already sends a
            # declined document there. Offering it as a choice would make "the model picked
            # General" and "the model picked nothing" indistinguishable in the log, and one
            # of those is a working classifier.
            rows = await session.execute(
                text(
                    "SELECT id, name FROM access_labels "
                    "WHERE id = ANY(:ids) AND NOT is_quarantine AND NOT is_default "
                    "ORDER BY name"
                ),
                {"ids": [str(label_id) for label_id in reachable]},
            )
            folders = [(row.id, row.name) for row in rows]
            # A reach made entirely of reserved labels. The same fact as holding none — there
            # is no folder to offer this person — so it is the same ending, and emphatically
            # not "the installation has no model".
            return Offer(folders, None if folders else Outcome.NO_FOLDERS)
