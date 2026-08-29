"""Which space a query is answered in, as one value rather than three arguments.

A stored vector projected with one basis and a query vector projected with another is
**confident nonsense**: the numbers are all in range, the ordering is stable, the plan uses
the index, and the answers are wrong. ADR 0002 already names that failure for mixing
embedding models, and migration 0027 introduces a second way to reach it — two spaces over
the same model, differing only in the basis they are read in.

Three things stand between a query vector and the wrong basis, and this module is the first
of them.

**1. The caller names a space once.** `search.dense` used to take `(embedding, model,
version)` as three independent arguments, and that was the hole: nothing paired the vector
with the space it had been projected into, so the pairing was a convention somebody had to
remember. It now takes one `Space`, and the same value chooses the basis, filters the rows
and decides the width the query vector is cast to. There is no second argument to get wrong.

**2. There is exactly one copy of the basis.** The projection is `zenith_project` in the
database, reading `embedding_space_axes` for the space it is handed. Query vectors and stored
vectors go through the same rows of the same table because there is nowhere else for either
of them to go. Nothing here holds a matrix, and nothing here could hold a stale one.

**3. Postgres refuses the mismatch.** Since 0027 a stored vector is as wide as its space says,
so pgvector's own type check is the last gate: `different halfvec dimensions 1024 and 512`.
`eval/svd-basis.sql` demonstrates all three ways of getting it wrong and all three errors.
That one is not defence in depth — it is the only one of the three that keeps working when
somebody writes a new call site.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ZenithError
from app.features.embeddings.client import DIMENSION, MODEL, VERSION


class NoActiveEmbeddingSpace(ZenithError):
    """No space is serving, so there is nothing to search.

    Reached when a reindex has retired the old space before activating the new one. A 503
    rather than an empty result: "no documents match" and "retrieval is not configured" are
    different answers, and returning the first for the second is how a broken installation
    demonstrates well.
    """

    status_code = 503
    code = "no_active_embedding_space"


@dataclass(frozen=True, slots=True)
class Space:
    """One embedding space, and everything a query needs to know about it.

    Frozen and slotted so it cannot be edited after it is read — a `Space` whose `dimension`
    had been adjusted to make a cast fit would be exactly the bug this type exists to
    prevent.
    """

    model: str
    version: str
    #: The width of a stored vector here, and the width the query vector is cast to.
    dimension: int
    #: The width of the *input* to the projection, or `None` when this space does not
    #: project. `None` is the identity space: the vector the model returned, stored as it is.
    source_dimension: int | None

    @property
    def projects(self) -> bool:
        return self.source_dimension is not None


_ACTIVE = text(
    "SELECT model, version, dimension, source_dimension FROM embedding_spaces "
    "WHERE status = 'active' ORDER BY created_at DESC, version DESC LIMIT 1"
)


async def active(session: AsyncSession) -> Space:
    """The space this installation is currently serving from.

    Read on every search rather than cached in a module constant, and that is deliberate.
    A reindex flips which space is active *while the process is running* — that is what
    `embedding_spaces` is for — and a constant read at import time would keep answering from
    a space whose rows were being deleted underneath it. It is one primary-key read against a
    table with a handful of rows, on a request whose median is 941 ms.

    `embedding_spaces` carries no RLS, so this is the same answer inside any session factory.

    Ordered rather than assumed unique: `status` is a `CHECK`, not a constraint that stops
    two spaces being active at once, and a reindex that half-failed is exactly when this gets
    called. Newest wins, and it is deterministic either way.
    """
    row = (await session.execute(_ACTIVE)).first()
    if row is None:
        raise NoActiveEmbeddingSpace("no embedding space is active")
    return Space(
        model=row.model,
        version=row.version,
        dimension=row.dimension,
        source_dimension=row.source_dimension,
    )


#: The space an installation has before anything reindexes it: the identity, 1,024 wide.
#:
#: For tests and for the measurement harnesses under `eval/`, which build their own corpus and
#: know exactly what is in it. **The product path does not use this** — `service.search` calls
#: `active()`, because which space is serving is a property of the installation and a constant
#: read at import time would keep answering from a space a reindex had retired.
#:
#: Derived from `embeddings.client` rather than repeating its strings, so the two cannot
#: disagree. Migration 0027 is the third copy of the same name — it has to be, because a
#: partial index needs a literal predicate and cannot read a constant — and
#: `test_vector_index.py` holds it to this one.
SHIPPED = Space(model=MODEL, version=VERSION, dimension=DIMENSION, source_dimension=None)
