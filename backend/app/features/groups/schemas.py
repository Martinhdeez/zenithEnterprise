from uuid import UUID

from pydantic import BaseModel, Field


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class GroupLabelsRequest(BaseModel):
    """Replace, not patch: the caller sends what the group should open afterwards."""

    label_ids: list[UUID] = Field(default_factory=list[UUID])


class GroupMembersRequest(BaseModel):
    user_ids: list[UUID] = Field(default_factory=list[UUID])


class UserGroupsRequest(BaseModel):
    group_ids: list[UUID] = Field(default_factory=list[UUID])


class GroupResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    members: int
    #: What this group opens *to a member who also carries the clearance each label asks
    #: for*. Not a list of what any particular member can read.
    label_ids: list[UUID]
