"""Central model registry.

The only concession to layer-based organisation, and it is plumbing: Alembic needs
to see every table in a single `MetaData` to autogenerate migrations. Each model
lives in its feature; this file only imports them.
"""

from app.core.database import Base
from app.features.auth.model import Permission, Role, RolePermission, User, UserRole
from app.features.documents.model import Chunk, Document, Page
from app.features.embeddings.model import ChunkEmbedding, EmbeddingSpace
from app.features.generation.model import LlmConfig
from app.features.groups.model import Group, GroupLabel, UserGroup
from app.features.labels.model import AccessLabel, DocumentLabel, RoleLabel
from app.features.query.model import Query, QueryCitation
from app.features.tenancy.model import Tenant

__all__ = [
    "AccessLabel",
    "Base",
    "Chunk",
    "ChunkEmbedding",
    "Document",
    "DocumentLabel",
    "EmbeddingSpace",
    "Group",
    "GroupLabel",
    "LlmConfig",
    "Page",
    "Permission",
    "Query",
    "QueryCitation",
    "Role",
    "RoleLabel",
    "RolePermission",
    "Tenant",
    "User",
    "UserGroup",
    "UserRole",
]
