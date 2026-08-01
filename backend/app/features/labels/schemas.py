from uuid import UUID

from pydantic import BaseModel, Field


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

    model_config = {"from_attributes": True}
