from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.features.ingestion.classification import Outcome


class LabelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_default: bool = False


class LabelRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class LabelAssignment(BaseModel):
    """The complete set, not a delta.

    Replacing rather than adding means the caller states the intended end state, so two
    administrators editing at once cannot compose their changes into a set neither of them
    chose — which, for a value that decides visibility, is a leak with no author.
    """

    label_ids: list[UUID]


class LabelResponse(BaseModel):
    id: UUID
    name: str
    is_default: bool
    #: How much clearance this label demands of anyone reaching it through a group. Zero
    #: demands none — which is not the same as public: a label mapped to no group and
    #: granted to no role is still reachable by nobody.
    priority_level: int = 0

    model_config = {"from_attributes": True}


class LabelSuggestion(BaseModel):
    """An excerpt of a document that has not been uploaded yet.

    Text rather than the file: the staging area holds files in the browser until the user
    confirms, and sending the bytes to get a suggestion would upload everything twice — once
    to be read, once to be kept.
    """

    excerpt: str = Field(min_length=1, max_length=8000)


class SuggestedLabels(BaseModel):
    """The suggestion, and which ending produced it.

    `label_ids` alone could not say. Every ending but `chose` comes back with nothing to
    suggest, and a client holding only an empty list has to guess between them. The staging
    area guessed, and told the person `no match — server will file it` under `FAILED`, which
    is the one ending where that is false: a document nothing vouched for is left in a
    quarantine label only `admin` reaches.

    `Outcome` itself rather than a parallel enum of this feature's own. It is the same
    decision — `Classifier.suggest` is `Classifier.file` without the write — and a second
    vocabulary for it would be a second thing to keep in step with `0017`'s access rules.
    Being a `StrEnum` it crosses the wire as `chose` / `declined` / `unavailable` /
    `no_folders` / `too_many_folders` / `failed`.

    The last three of those were one value until `unavailable` was found to be doing exactly
    what this class was written to stop: several endings, one value, the caller guessing.
    `unavailable` now means only that the installation has no model, `no_folders` that this
    person reaches nothing that could be offered, and `too_many_folders` that their reach is
    past the ceiling `MAX_LABELS` sets — a permanent ending, since the shortlist that would
    have lifted it was measured and refused. An operator sent to check a working connector
    because the server said "no model" is the cost the split removes.
    """

    #: Ids from the caller's own reach, or empty. Never a name, and never a new label.
    label_ids: list[UUID]
    outcome: Outcome


class LabelClearance(BaseModel):
    priority_level: int = Field(ge=0, le=10)


class LabelSearchItem(BaseModel):
    """A label and how much of the corpus actually carries it.

    `documents` counts what the *caller* can see, not the tenant. `document_labels`
    inherits its RLS policy from `documents`, so an administrator who does not reach a
    label is told how many of its documents they could open — never how large the
    compartment really is. See `LabelRepository.search`.
    """

    id: UUID
    name: str
    is_default: bool
    documents: int
    #: When this label was last applied to a document the caller can see; null if never.
    #: Lets the picker say "used yesterday" without a second request per label.
    last_used: datetime | None


class LabelSearchPage(BaseModel):
    """`next_cursor` is null on the last page, and that is the only end-of-list signal.

    No total, for the reason `DocumentPage` gives: counting under RLS evaluates the policy
    over every row in the tenant to produce a number that is stale before it is read.
    """

    items: list[LabelSearchItem]
    next_cursor: str | None


class LabelMerge(BaseModel):
    """Fold `sources` into `target`, then delete them.

    Two flags rather than one, because previewing and consenting are different acts and a
    client can skip either. `dry_run` answers "what would this do" without doing it;
    `acknowledge_widening` is the caller stating they have seen the answer. A merge that
    would widen visibility and carries neither is refused — the same shape of guard
    `LabelService.delete` already applies to the same class of accident.
    """

    sources: list[UUID] = Field(min_length=1)
    target: UUID
    dry_run: bool = False
    acknowledge_widening: bool = False


class LabelMergeResult(BaseModel):
    """What the merge did, or — under `dry_run` — what it would do.

    `visibility_widening` is the number the caller is being asked to look at. It counts
    *documents that become visible to at least one role that could not see them before*,
    which is a different question from how many documents were relabelled: a merge can
    move a thousand documents and widen nothing, or move one and expose it to everybody.
    """

    target: LabelResponse
    merged: list[UUID]
    documents_relabelled: int
    visibility_widening: int
    dry_run: bool
