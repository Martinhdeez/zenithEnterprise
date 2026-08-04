from pydantic import BaseModel


class ComponentsResponse(BaseModel):
    """What this installation is configured to have.

    Not liveness. A status route that probed three services would be the slowest endpoint
    in the product and still stale by the time it rendered. When a component is actually
    down, the answer that needed it says so through `degraded`.
    """

    embeddings: bool
    reranker: bool
    generation: bool


class TenantStatusResponse(BaseModel):
    #: Keyed by `documents.status`: pending, processing, ready, failed. Absent keys are
    #: zero — sending every state with a 0 would imply this list is closed, and it is not.
    documents: dict[str, int]
    chunks: int
    #: gpu | cpu | low-spec. A client showing "reranking disabled" needs to say why.
    hardware: str
    components: ComponentsResponse
    #: `chunks > 0`. Computed here so that every client agrees on what an empty corpus is.
    searchable: bool
