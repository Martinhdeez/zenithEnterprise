from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.features.audit.service import record
from app.features.auth.dependencies import CurrentProfile, requires
from app.features.labels.pagination import DEFAULT_SORT, MAX_LIMIT, Sort
from app.features.labels.schemas import (
    LabelAssignment,
    LabelClearance,
    LabelCreate,
    LabelMerge,
    LabelMergeResult,
    LabelRename,
    LabelResponse,
    LabelSearchItem,
    LabelSearchPage,
    LabelSuggestion,
    SuggestedLabels,
)
from app.features.labels.service import MANAGE, LabelService

router = APIRouter(tags=["labels"])
manage = Depends(requires(MANAGE))


@router.get("/labels")
async def list_labels(profile: CurrentProfile) -> list[LabelResponse]:
    """Labels the caller may know about.

    Not gated on `labels.manage`, because a user has to see the label on a document they
    can already open. Gated on what they *reach*: a label name discloses something merely
    by existing, and there is no reason to enumerate labels you cannot use.
    """
    labels = await LabelService(profile.context).visible(may_manage=MANAGE in profile.permissions)
    return [LabelResponse.model_validate(label) for label in labels]


@router.get("/labels/search")
async def search_labels(
    profile: CurrentProfile,
    q: str | None = None,
    sort: Sort = DEFAULT_SORT,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    in_use: bool = False,
    mine: bool = False,
) -> LabelSearchPage:
    """Find labels by name, a page at a time, with usage counts.

    Same visibility rule as `GET /labels`, and deliberately not a stricter one: this is
    that list with a search box and a page size, so gating it on `labels.manage` would
    mean a user could see a label on a document and not find it here.

    Paginated with a cursor rather than an offset, for the reason
    `documents/pagination.py` sets out at length — under RLS an offset pays the policy
    cost of every row it discards, and labels created while someone is paging would shift
    the window under them.

    `sort=usage_count` counts only documents *this caller* can see; the count is
    RLS-scoped, not a tenant-wide total. `LabelRepository.search` explains why that is the
    only version of the number this endpoint is allowed to know. `sort=last_used` orders by
    when a label was last applied to a document, on the same RLS-scoped basis.

    `in_use=true` drops labels no document carries. `mine=true` narrows to labels on
    documents the caller uploaded — "the ones I file things under" — which with
    `sort=last_used` is the short list somebody reaches for by habit rather than by
    searching. Both are server-side because at the scale this endpoint is for, the client
    never holds the whole list to filter.
    """
    result = await LabelService(profile.context).search(
        may_manage=MANAGE in profile.permissions,
        query=q,
        sort=sort,
        cursor=cursor,
        limit=limit,
        in_use=in_use,
        # `profile.user_id`, never `context.user_id`: the request context is built without
        # a user, so reading it there would make `mine=true` silently match everything.
        uploaded_by=profile.user_id if mine else None,
    )
    return LabelSearchPage(
        items=[
            LabelSearchItem(
                id=label.id,
                name=label.name,
                is_default=label.is_default,
                documents=documents,
                last_used=last_used,
            )
            for label, documents, last_used in result.labels
        ],
        next_cursor=result.next_cursor,
    )


@router.post("/labels/merge", dependencies=[manage])
async def merge_labels(request: LabelMerge, profile: CurrentProfile) -> LabelMergeResult:
    """Fold redundant labels into one, and report what it does to visibility.

    Not a rename and not a tidy-up, whatever it looks like in the interface: labels are
    the access-control primitive, so folding two together moves documents between roles.
    The response says how many documents that is, and a merge that would widen anything is
    refused unless the caller either previewed it (`dry_run`) or said so
    (`acknowledge_widening`).

    `200` rather than `201`: nothing is created, and under `dry_run` nothing changes at
    all.
    """
    result = await LabelService(profile.context).merge(
        request.sources,
        request.target,
        dry_run=request.dry_run,
        acknowledge_widening=request.acknowledge_widening,
    )
    return LabelMergeResult(
        target=LabelResponse.model_validate(result.target),
        merged=result.merged,
        documents_relabelled=result.documents_relabelled,
        visibility_widening=result.visibility_widening,
        dry_run=result.dry_run,
    )


@router.post("/labels", status_code=status.HTTP_201_CREATED, dependencies=[manage])
async def create_label(request: LabelCreate, profile: CurrentProfile) -> LabelResponse:
    label = await LabelService(profile.context).create(
        request.name, request.is_default, created_by=profile.user_id
    )
    await record(
        profile, "label.created", target_type="label", target_id=label.id, target_name=label.name
    )
    return LabelResponse.model_validate(label)


@router.patch("/labels/{label_id}", dependencies=[manage])
async def rename_label(
    label_id: UUID, request: LabelRename, profile: CurrentProfile
) -> LabelResponse:
    label = await LabelService(profile.context).rename(label_id, request.name)
    await record(
        profile, "label.renamed", target_type="label", target_id=label.id, target_name=label.name
    )
    return LabelResponse.model_validate(label)


@router.put("/labels/{label_id}/default", dependencies=[manage])
async def set_default_label(label_id: UUID, profile: CurrentProfile) -> LabelResponse:
    label = await LabelService(profile.context).set_default(label_id)
    await record(
        profile,
        "label.default_set",
        target_type="label",
        target_id=label.id,
        target_name=label.name,
    )
    return LabelResponse.model_validate(label)


@router.delete("/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[manage])
async def delete_label(label_id: UUID, profile: CurrentProfile) -> None:
    await LabelService(profile.context).delete(label_id)
    await record(profile, "label.deleted", target_type="label", target_id=label_id)


@router.put("/roles/{role_id}/labels", dependencies=[manage])
async def set_role_labels(role_id: UUID, request: LabelAssignment, profile: CurrentProfile) -> None:
    await LabelService(profile.context).set_role_labels(role_id, request.label_ids)


@router.post("/labels/suggest")
async def suggest_labels(request: LabelSuggestion, profile: CurrentProfile) -> SuggestedLabels:
    """Which of *your* labels this text belongs under, according to the configured model.

    A suggestion, not an assignment: nothing is written, and the client is free to ignore it.
    The staging area calls this per file so somebody can review a hundred guesses before
    committing any of them, which is the difference between assistance and a model quietly
    filing a corpus.

    Gated on nothing beyond being signed in, deliberately. The candidate list is the
    caller's own reach — resolved by `Classifier` through `UserRepository.label_ids`, the
    single function that answers that question — so this can only ever name labels they
    already hold, and a suggestion of a label you hold tells you nothing you did not know.

    Answers with an empty list rather than an error when no model is configured. An
    installation without generation still uploads documents.
    """
    from app.features.ingestion.classification import Classifier

    suggested = await Classifier(profile.context).suggest(profile.user_id, request.excerpt)
    return SuggestedLabels(label_ids=suggested)


@router.put("/labels/{label_id}/clearance", dependencies=[manage])
async def set_label_clearance(
    label_id: UUID, request: LabelClearance, profile: CurrentProfile
) -> LabelResponse:
    """Classify a label, or declassify it back to zero.

    Zero is the default and means the group route asks for no clearance — not that the
    label is public. Clearance only ever narrows what a group opens; it is not a route of
    its own, so raising it can take access away and lowering it can never give access to
    somebody outside the group.
    """
    label = await LabelService(profile.context).set_clearance(label_id, request.priority_level)
    # Raising this can take access away from people who had it a moment ago, which is
    # precisely the kind of change somebody comes looking for an explanation of later.
    await record(
        profile,
        "label.clearance_set",
        target_type="label",
        target_id=label.id,
        target_name=label.name,
        clearance=request.priority_level,
    )
    return LabelResponse.model_validate(label)


@router.put("/documents/{document_id}/labels", dependencies=[manage])
async def set_document_labels(
    document_id: UUID, request: LabelAssignment, profile: CurrentProfile
) -> None:
    """Change which labels a document carries.

    Only reaches documents the caller can already see: RLS filters `documents` by the
    caller's own labels, so an administrator cannot relabel what is invisible to them.
    That is the right default — you cannot silently move a document you were never allowed
    to read — and it means an administrator needs the labels they intend to manage.
    """
    await LabelService(profile.context).set_document_labels(document_id, request.label_ids)
    await record(
        profile,
        "document.labels_set",
        target_type="document",
        target_id=document_id,
        labels=[str(label_id) for label_id in request.label_ids],
    )
