from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.features.auth.dependencies import CurrentProfile, requires
from app.features.labels.schemas import (
    LabelAssignment,
    LabelCreate,
    LabelRename,
    LabelResponse,
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


@router.post("/labels", status_code=status.HTTP_201_CREATED, dependencies=[manage])
async def create_label(request: LabelCreate, profile: CurrentProfile) -> LabelResponse:
    label = await LabelService(profile.context).create(request.name, request.is_default)
    return LabelResponse.model_validate(label)


@router.patch("/labels/{label_id}", dependencies=[manage])
async def rename_label(
    label_id: UUID, request: LabelRename, profile: CurrentProfile
) -> LabelResponse:
    label = await LabelService(profile.context).rename(label_id, request.name)
    return LabelResponse.model_validate(label)


@router.put("/labels/{label_id}/default", dependencies=[manage])
async def set_default_label(label_id: UUID, profile: CurrentProfile) -> LabelResponse:
    label = await LabelService(profile.context).set_default(label_id)
    return LabelResponse.model_validate(label)


@router.delete("/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[manage])
async def delete_label(label_id: UUID, profile: CurrentProfile) -> None:
    await LabelService(profile.context).delete(label_id)


@router.put("/roles/{role_id}/labels", dependencies=[manage])
async def set_role_labels(role_id: UUID, request: LabelAssignment, profile: CurrentProfile) -> None:
    await LabelService(profile.context).set_role_labels(role_id, request.label_ids)


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
