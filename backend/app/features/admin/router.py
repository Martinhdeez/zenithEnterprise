"""The M3 administration surface: roles and the generation connector.

Labels already have their own routes from F3, so they are absent here rather than
duplicated — one resource with two routers is how two screens start disagreeing about what
a label is.

Every route is gated on an explicit permission from the catalogue. None of them infers
authority from a role name, because a customer may rename `admin` to anything and the check
must survive it.
"""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.features.admin.schemas import (
    AssignRolesRequest,
    LlmConfigRequest,
    LlmConfigResponse,
    RoleRequest,
    RoleResponse,
)
from app.features.auth.dependencies import CurrentProfile, requires
from app.features.auth.roles import MANAGE as ROLES_MANAGE
from app.features.auth.roles import RoleService
from app.features.generation.config_service import MANAGE as LLM_MANAGE
from app.features.generation.config_service import LlmConfigService

router = APIRouter(tags=["admin"])

roles_manage = Depends(requires(ROLES_MANAGE))
llm_manage = Depends(requires(LLM_MANAGE))


@router.get(
    "/roles",
    operation_id="listRoles",
    summary="Roles in this tenant, with their permissions and how many users hold them",
    dependencies=[roles_manage],
)
async def list_roles(profile: CurrentProfile) -> list[RoleResponse]:
    return [RoleResponse(**asdict(role)) for role in await RoleService(profile).visible()]


@router.post(
    "/roles",
    operation_id="createRole",
    status_code=status.HTTP_201_CREATED,
    summary="Create a role",
    responses={404: {"description": "A permission code the software does not enforce"}},
    dependencies=[roles_manage],
)
async def create_role(profile: CurrentProfile, request: RoleRequest) -> RoleResponse:
    """Unknown permission codes are refused rather than stored.

    A permission nobody checks is a lie in the administration screen: it appears granted
    and grants nothing.
    """
    role = await RoleService(profile).create(request.name, request.permissions)
    return RoleResponse(**asdict(role))


@router.put(
    "/roles/{role_id}/permissions",
    operation_id="setRolePermissions",
    summary="Replace a role's permissions",
    responses={
        404: {"description": "No such role, or an unknown permission code"},
        409: {"description": "A system role, or an edit leaving the tenant with no administrator"},
    },
    dependencies=[roles_manage],
)
async def set_permissions(
    profile: CurrentProfile, role_id: UUID, request: RoleRequest
) -> RoleResponse:
    """Replace rather than patch, and refuse an edit that locks the tenant out.

    Replace, because a caller sending the full set knows what the role will hold
    afterwards; add/remove makes the result depend on state they did not read.

    Refuse, because recovering from "nobody holds roles.manage" on an on-premise install
    means somebody in `psql` on the customer's server.
    """
    role = await RoleService(profile).set_permissions(role_id, request.permissions)
    return RoleResponse(**asdict(role))


@router.put(
    "/users/{user_id}/roles",
    operation_id="assignRoles",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Replace a user's roles",
    responses={404: {"description": "No such user or role in this tenant"}},
    dependencies=[roles_manage],
)
async def assign_roles(profile: CurrentProfile, user_id: UUID, request: AssignRolesRequest) -> None:
    await RoleService(profile).assign(user_id, request.role_ids)


@router.get(
    "/llm-config",
    operation_id="getLlmConfig",
    summary="The generation connector for this tenant",
    dependencies=[llm_manage],
)
async def get_llm_config(profile: CurrentProfile) -> LlmConfigResponse:
    """The key is never returned, only whether one is stored.

    An administration screen that displays a credential turns every support screenshot
    into a disclosure, and nobody needs to read it — only to replace it.
    """
    return LlmConfigResponse(**asdict(await LlmConfigService(profile).get()))


@router.put(
    "/llm-config",
    operation_id="setLlmConfig",
    summary="Configure the generation connector",
    responses={500: {"description": "ZENITH_ENCRYPTION_KEY is unset or unusable"}},
    dependencies=[llm_manage],
)
async def set_llm_config(profile: CurrentProfile, request: LlmConfigRequest) -> LlmConfigResponse:
    """Omitting `api_key` leaves the stored one untouched; sending `""` clears it.

    An administrator editing a model name should not have to re-enter a credential they
    cannot read, and wiping it on a save that looked harmless would break generation.
    """
    settings = await LlmConfigService(profile).put(
        request.endpoint_url, request.model_name, request.api_key
    )
    return LlmConfigResponse(**asdict(settings))


@router.delete(
    "/llm-config",
    operation_id="clearLlmConfig",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Fall back to the installation default",
    dependencies=[llm_manage],
)
async def clear_llm_config(profile: CurrentProfile) -> None:
    await LlmConfigService(profile).clear()
