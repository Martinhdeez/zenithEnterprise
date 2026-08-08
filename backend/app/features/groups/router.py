"""The group administration surface.

Mounted under `/admin` beside roles, because these are the same job: deciding who reads
what. Gated on the same permission for the same reason — a screen that could add somebody
to `Finance` without holding `roles.manage` would be a second, weaker route to the access
the roles screen is careful about.
"""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.features.auth.dependencies import CurrentProfile, requires
from app.features.groups.schemas import (
    GroupLabelsRequest,
    GroupMembersRequest,
    GroupRequest,
    GroupResponse,
    UserGroupsRequest,
)
from app.features.groups.service import MANAGE, GroupService

router = APIRouter(tags=["admin"])

manage = Depends(requires(MANAGE))


@router.get(
    "/groups",
    operation_id="listGroups",
    summary="Functional groups in this tenant, with their members and mapped labels",
    dependencies=[manage],
)
async def list_groups(profile: CurrentProfile) -> list[GroupResponse]:
    return [GroupResponse(**asdict(group)) for group in await GroupService(profile).visible()]


@router.post(
    "/groups",
    operation_id="createGroup",
    status_code=status.HTTP_201_CREATED,
    summary="Create a group",
    responses={409: {"description": "A group with that name already exists here"}},
    dependencies=[manage],
)
async def create_group(profile: CurrentProfile, request: GroupRequest) -> GroupResponse:
    group = await GroupService(profile).create(request.name, request.description)
    return GroupResponse(**asdict(group))


@router.put(
    "/groups/{group_id}",
    operation_id="updateGroup",
    summary="Rename a group or change its description",
    responses={404: {"description": "No such group in this tenant"}},
    dependencies=[manage],
)
async def update_group(
    profile: CurrentProfile, group_id: UUID, request: GroupRequest
) -> GroupResponse:
    group = await GroupService(profile).rename(group_id, request.name, request.description)
    return GroupResponse(**asdict(group))


@router.delete(
    "/groups/{group_id}",
    operation_id="deleteGroup",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a group, removing the access it conferred",
    responses={404: {"description": "No such group in this tenant"}},
    dependencies=[manage],
)
async def delete_group(profile: CurrentProfile, group_id: UUID) -> None:
    """Every member loses whatever this group opened. Nothing is transferred."""
    await GroupService(profile).delete(group_id)


@router.put(
    "/groups/{group_id}/labels",
    operation_id="setGroupLabels",
    summary="Replace the labels this group opens",
    responses={404: {"description": "No such group, or a label that is not in this tenant"}},
    dependencies=[manage],
)
async def set_group_labels(
    profile: CurrentProfile, group_id: UUID, request: GroupLabelsRequest
) -> GroupResponse:
    """Mapping a label here does not by itself let members read it.

    They must also carry the clearance the label demands. The two halves are independent
    on purpose: membership is which part of the business you are in, clearance is how
    sensitive the material is, and neither implies the other.
    """
    group = await GroupService(profile).set_labels(group_id, request.label_ids)
    return GroupResponse(**asdict(group))


@router.put(
    "/groups/{group_id}/members",
    operation_id="setGroupMembers",
    summary="Replace this group's membership",
    responses={404: {"description": "No such group, or a user who is not in this tenant"}},
    dependencies=[manage],
)
async def set_group_members(
    profile: CurrentProfile, group_id: UUID, request: GroupMembersRequest
) -> GroupResponse:
    group = await GroupService(profile).set_members(group_id, request.user_ids)
    return GroupResponse(**asdict(group))


@router.put(
    "/users/{user_id}/groups",
    operation_id="setUserGroups",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Replace one user's groups",
    responses={404: {"description": "No such user or group in this tenant"}},
    dependencies=[manage],
)
async def set_user_groups(
    profile: CurrentProfile, user_id: UUID, request: UserGroupsRequest
) -> None:
    """The same relation as `PUT /groups/{id}/members`, from the person's side.

    Both exist because both screens exist: one edits a group's roster, the other edits one
    person's memberships, and making either go through the other means reading state the
    caller did not ask about.
    """
    await GroupService(profile).set_user_groups(user_id, request.group_ids)
